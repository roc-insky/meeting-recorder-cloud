"""Server-side sessions and the test-only identity provider (gate G1).

Security properties implemented here:

* the browser only ever sees an opaque random session token; the database key
  is its SHA-256, so a database leak alone cannot be replayed as a cookie;
* login is a two-step flow with a ``state`` value bound to the pending session
  row, an expiry, and single use (``consumed_at``), so callback replay fails;
* the session token is rotated on successful login;
* mutations require a CSRF token that is present as a non-HttpOnly cookie AND
  an ``X-CSRF-Token`` header AND stored on the session row; cross-origin
  requests are refused;
* login failures are generic (``login_failed``), never revealing whether an
  account exists.

The fake provider exists only so the stack is testable while G1 is unanswered.
``Settings.validate_for_startup()`` refuses it in production.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from cloud.contracts import (
    AuthContext,
    ForbiddenError,
    InvalidRequestError,
    NotFoundError,
    SessionView,
    UnauthorizedError,
    b64url_decode,
    b64url_encode,
    iso_utc,
)
from cloud.db import get_db
from cloud.schema import AuthSession, User
from cloud.settings import Settings

router = APIRouter(prefix="/api/cloud/auth", tags=["auth"])

_MAX_COOKIE_TOKEN_LENGTH = 512


# -- identity provider seam ------------------------------------------------


class IdentityError(Exception):
    """Raised when a provider callback cannot be trusted."""


class Identity:
    __slots__ = ("subject", "email")

    def __init__(self, subject: str, email: str) -> None:
        self.subject = subject
        self.email = email


class IdentityProvider(Protocol):
    def authorize_url(
        self, *, state: str, redirect_uri: str, login_hint: str | None
    ) -> str: ...

    def identity_from_callback(
        self, *, code: str, state: str, now: datetime
    ) -> Identity: ...


class FakeIdentityProvider:
    """Test-only provider: self-issued HMAC-signed codes, no network, no OAuth.

    It deliberately has no password and no registration UI: the login hint is
    the identity. This is why production startup must refuse it.
    """

    def __init__(self, secret_key: str, authorize_path: str = "/api/cloud/auth/fake/authorize") -> None:
        self._key = secret_key.encode("utf-8")
        self._authorize_path = authorize_path

    def authorize_url(
        self, *, state: str, redirect_uri: str, login_hint: str | None
    ) -> str:
        query = {"state": state, "redirect_uri": redirect_uri}
        if login_hint:
            query["email"] = login_hint
        return f"{self._authorize_path}?{urlencode(query)}"

    def issue_code(self, *, email: str, now: datetime) -> str:
        payload = json.dumps(
            {"email": email.strip().lower(), "issued_at": int(now.timestamp()), "nonce": secrets.token_urlsafe(12)},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        body = b64url_encode(payload)
        signature = b64url_encode(hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest())
        return f"{body}.{signature}"

    def identity_from_callback(self, *, code: str, state: str, now: datetime) -> Identity:
        try:
            body, signature = code.split(".", 1)
            expected = b64url_encode(
                hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest()
            )
            if not secrets.compare_digest(signature, expected):
                raise IdentityError("bad signature")
            payload = json.loads(b64url_decode(body))
            email = str(payload["email"]).strip().lower()
            issued_at = int(payload["issued_at"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise IdentityError("malformed code") from exc
        if not email or "@" not in email or len(email) > 320:
            raise IdentityError("invalid identity")
        if issued_at > int(now.timestamp()) + 60:
            raise IdentityError("code issued in the future")
        return Identity(subject=f"email:{email}", email=email)


def build_identity_provider(settings: Settings) -> IdentityProvider:
    if settings.auth_provider == "fake_test":
        return FakeIdentityProvider(settings.secret_key)
    raise RuntimeError(
        f"identity provider {settings.auth_provider!r} is not implemented "
        "(gate G1: real provider not selected)"
    )


# -- helpers ---------------------------------------------------------------


def hash_session_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _now(request: Request) -> datetime:
    return request.app.state.clock.now()


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _provider(request: Request) -> IdentityProvider:
    return request.app.state.identity_provider


def callback_url(request: Request) -> str:
    settings = _settings(request)
    base = settings.public_base_url or f"{request.url.scheme}://{request.url.netloc}"
    return f"{base.rstrip('/')}/api/cloud/auth/callback"


def _set_session_cookie(response: Response, settings: Settings, token: str, max_age: int) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _set_csrf_cookie(response: Response, settings: Settings, token: str, max_age: int) -> None:
    # Readable by same-origin JavaScript on purpose: the double-submit value.
    response.set_cookie(
        settings.csrf_cookie_name,
        token,
        max_age=max_age,
        httponly=False,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _clear_auth_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )
    response.delete_cookie(
        settings.csrf_cookie_name,
        path="/",
        httponly=False,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def _active_session(request: Request, db: Session) -> AuthSession:
    settings = _settings(request)
    token = request.cookies.get(settings.session_cookie_name)
    if not token or len(token) > _MAX_COOKIE_TOKEN_LENGTH:
        raise UnauthorizedError()
    row = db.get(AuthSession, hash_session_token(token))
    now = _now(request)
    if row is None or row.user_id is None:
        raise UnauthorizedError()
    user = db.get(User, row.user_id)
    if user is None or user.deleted_at is not None:
        # A revoked account must never keep a usable session alive.
        raise UnauthorizedError()
    if row.consumed_at is not None or row.revoked_at is not None:
        raise UnauthorizedError()
    if row.expires_at <= now:
        raise UnauthorizedError("session_expired")
    return row


# -- dependencies ----------------------------------------------------------


def require_user(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    """Resolve the caller from the server-side session, or raise 401."""
    row = _active_session(request, db)
    return AuthContext(
        user_id=row.user_id,
        session_id=row.id,
        db=db,
        clock=request.app.state.clock,
        settings=request.app.state.settings,
        objects=getattr(request.app.state, "object_store", None),
        audit=getattr(request.app.state, "audit_store", None),
    )


def require_csrf(
    request: Request, ctx: AuthContext = Depends(require_user)
) -> AuthContext:
    """Require session + double-submit CSRF token + same-origin mutation."""
    settings = ctx.settings
    header = request.headers.get(settings.csrf_header_name)
    cookie = request.cookies.get(settings.csrf_cookie_name)
    if not header or not cookie or not secrets.compare_digest(header, cookie):
        raise ForbiddenError("csrf_failed")
    row = ctx.require_db().get(AuthSession, ctx.session_id)
    if row is None or not secrets.compare_digest(row.csrf_token, header):
        raise ForbiddenError("csrf_failed")
    _require_same_origin(request, settings)
    return ctx


def _require_same_origin(request: Request, settings: Settings) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        # Non-browser clients are still covered by the header/cookie/DB check.
        return
    expected = settings.public_base_url or f"{request.url.scheme}://{request.url.netloc}"
    if origin == "null" or origin.rstrip("/") != expected.rstrip("/"):
        raise ForbiddenError("csrf_failed")


# -- routes ----------------------------------------------------------------


@router.get("/login")
def start_login(
    request: Request,
    email: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    settings = _settings(request)
    now = _now(request)
    raw_token = secrets.token_urlsafe(32)
    state = secrets.token_urlsafe(32)
    pending = AuthSession(
        id=hash_session_token(raw_token),
        user_id=None,
        csrf_token=secrets.token_urlsafe(32),
        state=state,
        state_expires_at=now + timedelta(seconds=settings.login_state_ttl_s),
        expires_at=now + timedelta(seconds=settings.login_state_ttl_s),
        created_at=now,
    )
    db.add(pending)
    db.commit()

    url = _provider(request).authorize_url(
        state=state, redirect_uri=callback_url(request), login_hint=email
    )
    response = RedirectResponse(url, status_code=303)
    _set_session_cookie(response, settings, raw_token, settings.login_state_ttl_s)
    return response


@router.get("/fake/authorize")
def fake_authorize(
    request: Request,
    state: str,
    redirect_uri: str | None = None,
    email: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """The test-only stand-in for the provider's consent screen.

    It refuses to issue a code unless the caller holds a live pending session
    whose state matches, which keeps login CSRF and state fixation testable
    without a real provider.
    """
    provider = _provider(request)
    if not isinstance(provider, FakeIdentityProvider):
        raise NotFoundError()

    settings = _settings(request)
    token = request.cookies.get(settings.session_cookie_name)
    row = db.get(AuthSession, hash_session_token(token)) if token else None
    now = _now(request)
    if (
        row is None
        or row.user_id is not None
        or row.consumed_at is not None
        or row.state is None
        or row.expires_at <= now
        or not secrets.compare_digest(row.state, state)
    ):
        raise InvalidRequestError("invalid_login_state")
    if not email:
        raise InvalidRequestError("email_required")

    expected_redirect = callback_url(request)
    if redirect_uri is not None and redirect_uri != expected_redirect:
        raise InvalidRequestError("invalid_redirect_uri")

    code = provider.issue_code(email=email, now=now)
    location = f"{expected_redirect}?{urlencode({'code': code, 'state': state})}"
    return RedirectResponse(location, status_code=303)


@router.get("/callback")
def login_callback(
    request: Request,
    code: str,
    state: str,
    db: Session = Depends(get_db),
) -> Response:
    settings = _settings(request)
    now = _now(request)
    token = request.cookies.get(settings.session_cookie_name)
    if not token or len(token) > _MAX_COOKIE_TOKEN_LENGTH:
        raise InvalidRequestError("invalid_login_state")
    pending = db.get(AuthSession, hash_session_token(token))
    if pending is None or pending.user_id is not None or pending.state is None:
        raise InvalidRequestError("invalid_login_state")
    if pending.consumed_at is not None:
        raise InvalidRequestError("login_state_used")
    if pending.expires_at <= now or (
        pending.state_expires_at is not None and pending.state_expires_at <= now
    ):
        raise InvalidRequestError("login_state_expired")
    if not secrets.compare_digest(pending.state, state):
        raise InvalidRequestError("invalid_login_state")

    try:
        identity = _provider(request).identity_from_callback(code=code, state=state, now=now)
    except IdentityError:
        # Generic on purpose: no account-existence signal.
        raise UnauthorizedError("login_failed")

    email = identity.email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(id=str(uuid4()), email=email, created_at=now)
        db.add(user)
        db.flush()
    elif user.deleted_at is not None:
        # Never resurrect or authenticate a revoked account; the failure stays
        # generic so it reveals nothing about account existence.
        db.rollback()
        raise UnauthorizedError("login_failed")

    pending.consumed_at = now

    raw_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    session = AuthSession(
        id=hash_session_token(raw_token),
        user_id=user.id,
        csrf_token=csrf_token,
        state=None,
        state_expires_at=None,
        expires_at=now + timedelta(seconds=settings.session_ttl_s),
        created_at=now,
        authenticated_at=now,
    )
    db.add(session)
    db.commit()

    body = SessionView(
        user_id=user.id, csrf_token=csrf_token, expires_at=iso_utc(session.expires_at)
    )
    response = JSONResponse(body.model_dump(mode="json"))
    _set_session_cookie(response, settings, raw_token, settings.session_ttl_s)
    _set_csrf_cookie(response, settings, csrf_token, settings.session_ttl_s)
    return response


@router.get("/session")
def read_session(ctx: AuthContext = Depends(require_user)) -> Response:
    row = ctx.require_db().get(AuthSession, ctx.session_id)
    if row is None:
        raise UnauthorizedError()
    body = SessionView(
        user_id=ctx.user_id, csrf_token=row.csrf_token, expires_at=iso_utc(row.expires_at)
    )
    return JSONResponse(body.model_dump(mode="json"))


@router.post("/logout", status_code=204)
def logout(request: Request, ctx: AuthContext = Depends(require_csrf)) -> Response:
    row = ctx.require_db().get(AuthSession, ctx.session_id)
    if row is not None:
        row.revoked_at = ctx.clock.now()
        ctx.require_db().commit()
    response = Response(status_code=204)
    _clear_auth_cookies(response, _settings(request))
    return response
