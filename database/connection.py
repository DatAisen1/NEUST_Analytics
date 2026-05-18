from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Type

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, declarative_base, scoped_session, sessionmaker

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SQLITE_PATH = PROJECT_ROOT / 'data' / 'neust_analytics.db'
DATABASE_URL = os.getenv('DATABASE_URL', f'sqlite:///{DEFAULT_SQLITE_PATH.as_posix()}')

ENGINE_KWARGS = {
    'future': True,
    'echo': False,
}

if DATABASE_URL.startswith('sqlite'):
    ENGINE_KWARGS['connect_args'] = {'check_same_thread': False}

engine = create_engine(DATABASE_URL, **ENGINE_KWARGS)
SessionLocal = scoped_session(
    sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
        future=True,
    )
)
Base = declarative_base()


def get_database_url() -> str:
    """Return the current database URL used by the connector."""
    return DATABASE_URL


@contextmanager
def get_session() -> Iterator[Session]:
    """Yield a database session and manage commit/rollback automatically."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def initialize_database(base = Base) -> None:
    """Create database tables for the registered SQLAlchemy models."""
    try:
        base.metadata.create_all(bind=engine)
    except SQLAlchemyError as exc:
        raise RuntimeError('Failed to initialize database schema') from exc


__all__ = [
    'DATABASE_URL',
    'engine',
    'SessionLocal',
    'Base',
    'get_database_url',
    'get_session',
    'initialize_database',
]
