from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def stable_lock_id(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@asynccontextmanager
async def postgres_advisory_lock(session: AsyncSession, lock_name: str) -> AsyncIterator[None]:
    """Acquire a Postgres advisory lock for a singleton worker section.

    SQLite/test databases do not support advisory locks; they are treated as a
    no-op so unit tests and local ephemeral stores remain lightweight.
    """

    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect != "postgresql":
        yield
        return

    lock_id = stable_lock_id(lock_name)
    acquired = bool(await session.scalar(text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": lock_id}))
    if not acquired:
        raise RuntimeError(f"Another instance already holds lock {lock_name!r}")
    try:
        yield
    finally:
        await session.scalar(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id})
