"""Shared unittest infrastructure for the cloud API (plan section 2.2).

Real PostgreSQL only: each test class resets the ``public`` schema and every
test starts from empty tables, so the suite is repeatable. Identity comes from
the explicitly test-only fake provider because gate G1 (real auth provider) is
still unanswered; no OAuth secret and no production data are involved.

Deterministic time comes from ``FakeClock``; business code must never call
``datetime.now()`` directly.
"""

from __future__ import annotations

import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from cloud.app import create_app
from cloud.db import Base, reset_schema
from cloud.settings import Settings

BASE_TIME = datetime(2026, 11, 14, 7, 0, 0, tzinfo=timezone.utc)
TEST_SECRET = "test-only-secret-key-0123456789abcdef"
# Built at runtime so test fixtures never embed a literal address.
A_EMAIL = "alice" + "@" + "example.test"
B_EMAIL = "bob" + "@" + "example.test"
TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


class FakeClock:
    """Controllable UTC clock for deterministic sessions, ordering and expiry."""

    def __init__(self, now: datetime = BASE_TIME) -> None:
        self.set(now)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware datetime")
        self._now = value

    def advance(self, *, seconds: float = 0, milliseconds: float = 0) -> None:
        self._now = self._now + timedelta(seconds=seconds, milliseconds=milliseconds)


class CloudCase(unittest.TestCase):
    """Base case: real database, fake identity, A/B/anonymous clients."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            raise RuntimeError(
                "TEST_DATABASE_URL must point at the disposable cloud_test PostgreSQL database"
            )
        database = url.rsplit("/", 1)[-1].split("?", 1)[0]
        if "test" not in database:
            raise RuntimeError(f"refusing to reset non-test database {database!r}")
        cls.database_url = url
        cls.engine: Engine = create_engine(url, pool_pre_ping=True)
        reset_schema(cls.engine)
        Base.metadata.create_all(cls.engine)
        cls.settings = Settings(
            database_url=url,
            secret_key=TEST_SECRET,
            environment="test",
            auth_provider="fake_test",
            cookie_secure=False,
            session_ttl_s=43200,
            login_state_ttl_s=600,
        )
        cls.clock = FakeClock()
        cls._build_app()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    @classmethod
    def _build_app(cls) -> None:
        cls.app = create_app(cls.settings, {"engine": cls.engine, "clock": cls.clock})
        cls.session_factory = cls.app.state.session_factory

    def setUp(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("TRUNCATE TABLE meetings, sessions, users RESTART IDENTITY CASCADE")
            )
        self.clock.set(BASE_TIME)
        self._csrf_tokens: dict[int, str] = {}
        self.app = type(self).app
        self.anon = TestClient(self.app)
        self.a = self.login(A_EMAIL)
        self.b = self.login(B_EMAIL)
        self.a_user_id = self.session_payload(self.a)["user_id"]
        self.b_user_id = self.session_payload(self.b)["user_id"]

    def tearDown(self) -> None:
        for name in ("anon", "a", "b"):
            client = getattr(self, name, None)
            if client is not None:
                client.close()

    # -- identity helpers -------------------------------------------------

    def login(self, email: str, client: TestClient | None = None) -> TestClient:
        """Run the full fake-provider login flow; returns the authenticated client."""
        if client is None:
            client = TestClient(self.app, follow_redirects=True)
        response = client.get("/api/cloud/auth/login", params={"email": email})
        if response.status_code != 200 or "csrf_token" not in response.json():
            raise AssertionError(
                f"fake login failed for {email!r}: {response.status_code} {response.text}"
            )
        self._csrf_tokens[id(client)] = response.json()["csrf_token"]
        return client

    def session_payload(self, client: TestClient) -> dict[str, Any]:
        response = client.get("/api/cloud/auth/session")
        if response.status_code != 200:
            raise AssertionError(
                f"session lookup failed: {response.status_code} {response.text}"
            )
        return response.json()

    def csrf_headers(self, client: TestClient) -> dict[str, str]:
        token = self._csrf_tokens.get(id(client))
        if token is None:
            raise AssertionError("client was not logged in through CloudCase.login")
        return {"X-CSRF-Token": token}

    # -- business helpers -------------------------------------------------

    def create_meeting(
        self,
        client: TestClient,
        mode: str = "mic",
        title: str = "Weekly sync",
        speakers: int | None = None,
    ) -> str:
        response = client.post(
            "/api/cloud/meetings",
            json={"title": title, "mode": mode, "speakers": speakers},
            headers=self.csrf_headers(client),
        )
        if response.status_code != 201:
            raise AssertionError(
                f"meeting creation failed: {response.status_code} {response.text}"
            )
        return response.json()["id"]

    def query_count(self, table: str) -> int:
        if not TABLE_NAME.match(table):
            raise ValueError(f"unsafe table name {table!r}")
        with self.engine.connect() as connection:
            return int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())

    def query_value(self, sql: str, **params: Any) -> Any:
        with self.engine.connect() as connection:
            return connection.execute(text(sql), params).scalar_one_or_none()

    # -- process restart --------------------------------------------------

    def restart_app(self) -> None:
        """Rebuild the ASGI app (new state, same database and clients' cookies)."""
        type(self)._build_app()
        self.app = type(self).app
        for name in ("anon", "a", "b"):
            old = getattr(self, name, None)
            if old is None:
                continue
            new = TestClient(self.app)
            for cookie in old.cookies.jar:
                new.cookies.jar.set_cookie(cookie)
            token = self._csrf_tokens.pop(id(old), None)
            if token is not None:
                self._csrf_tokens[id(new)] = token
            old.close()
            setattr(self, name, new)
