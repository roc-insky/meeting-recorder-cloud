"""Database engine, session factory and request-scoped dependency."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import Request
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from cloud.settings import Settings


class Base(DeclarativeBase):
    """Declarative base for every cloud table."""


def create_database_engine(settings: Settings, **kwargs: Any) -> Engine:
    options: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 5,
    }
    options.update(kwargs)
    return create_engine(settings.database_url, **options)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def reset_schema(engine: Engine) -> None:
    """Drop and recreate the public schema (tests only; guarded by the caller)."""
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


def get_db(request: Request) -> Iterator[Session]:
    """Yield a request-scoped session; roll back failures, always close.

    Services own their commit boundaries. The dependency only guarantees that a
    failed request cannot leave an open transaction holding locks.
    """
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise RuntimeError("cloud app state is missing session_factory")
    session = factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
