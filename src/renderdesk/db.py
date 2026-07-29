from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from renderdesk.config import settings


class Base(DeclarativeBase):
    pass


def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    # Every ForeignKey in models.py is otherwise decorative at runtime —
    # SQLite doesn't enforce them unless told to on each connection.
    cursor.execute("PRAGMA foreign_keys=ON")
    # WAL + a busy timeout independently reduce the "database is locked"
    # errors app.py's _run_migrations already retries around (seen during a
    # deploy's brief old/new-container overlap).
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def _make_engine():
    db_path = Path(settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listens_for(engine.sync_engine, "connect")(_set_sqlite_pragmas)
    return engine


engine = _make_engine()
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        yield session
