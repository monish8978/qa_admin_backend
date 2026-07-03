"""SQLAlchemy engine + session factory for the master database.

The schema is currently owned by the Nest API's Prisma `prisma-master`
migrations. This module connects to the same DB; we do NOT call
``Base.metadata.create_all``. New schema changes should be made via Alembic
once the Nest API is retired.
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()


def _normalize_pg_url(url: str) -> str:
    """Ensure a SQLAlchemy URL uses the psycopg (v3) driver.

    Nest's config writes plain ``postgresql://...`` URLs that SQLAlchemy maps
    to psycopg2 by default. We standardize on psycopg v3 in this port.
    """
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    return url


engine = create_engine(
    _normalize_pg_url(_settings.MASTER_DATABASE_URL),
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=10,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
