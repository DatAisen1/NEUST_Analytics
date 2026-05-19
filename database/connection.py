# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# database/connection.py
# SQLAlchemy engine, session factory, and connection health check
# ==============================================================================

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from utils.config import get_config
from utils.logger import logger

# ------------------------------------------------------------------------------
# Engine (module-level singleton — created once on first import)
# ------------------------------------------------------------------------------

_config = get_config()

_engine: Engine | None = None
_SessionFactory: sessionmaker | None = None


def _build_engine() -> Engine:
    """
    Create and configure the SQLAlchemy engine.

    Pool settings are tuned for a single-user manual pipeline:
        pool_size=5       — up to 5 persistent connections
        max_overflow=10   — up to 10 extra connections under load
        pool_timeout=30   — wait up to 30s before raising PoolTimeout
        pool_recycle=1800 — recycle connections every 30 min (avoids stale)
        pool_pre_ping=True — test connection before use (handles DB restarts)
    """
    url = (
        f"postgresql+psycopg2://"
        f"{_config.db_user}:{_config.db_password}"
        f"@{_config.db_host}:{_config.db_port}"
        f"/{_config.db_name}"
    )

    engine = create_engine(
        url,
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        pool_recycle=1800,
        pool_pre_ping=True,
        # Echo SQL only when DEBUG mode is on — never in production runs
        echo=_config.debug,
        echo_pool=False,
        future=True,
    )

    # Log every new physical connection (not checkout) for audit purposes
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, connection_record):
        logger.debug("New DB connection opened — pool record: {}", id(connection_record))

    @event.listens_for(engine, "checkout")
    def _on_checkout(dbapi_conn, connection_record, connection_proxy):
        logger.debug("DB connection checked out from pool")

    logger.info(
        "Database engine created — {}@{}:{}/{}",
        _config.db_user,
        _config.db_host,
        _config.db_port,
        _config.db_name,
    )
    return engine


def get_engine() -> Engine:
    """Return the module-level engine singleton, creating it if needed."""
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> sessionmaker:
    """Return the module-level session factory singleton."""
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            bind=get_engine(),
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,   # prevent lazy-load errors after commit
        )
    return _SessionFactory


# ------------------------------------------------------------------------------
# Session context manager (preferred usage)
# ------------------------------------------------------------------------------

@contextmanager
def get_session() -> Generator[Session, None, None]:
    """
    Provide a transactional database session.

    Usage:
        with get_session() as session:
            session.add(record)
            # commits automatically on exit; rolls back on exception

    The session is always closed after the block, even on error.
    Nested sessions are NOT supported — open one session per pipeline step.
    """
    factory = get_session_factory()
    session: Session = factory()
    try:
        yield session
        session.commit()
        logger.debug("Session committed successfully")
    except SQLAlchemyError as exc:
        session.rollback()
        logger.error("Session rolled back due to SQLAlchemy error: {}", exc)
        raise
    except Exception as exc:
        session.rollback()
        logger.error("Session rolled back due to unexpected error: {}", exc)
        raise
    finally:
        session.close()
        logger.debug("Session closed")


# ------------------------------------------------------------------------------
# Health check
# ------------------------------------------------------------------------------

def check_connection() -> bool:
    """
    Verify that the database is reachable and all three schemas exist.

    Returns True on success, False on any failure.
    Called by pipeline.py at startup before any work begins.
    """
    try:
        with get_engine().connect() as conn:
            # Basic connectivity
            conn.execute(text("SELECT 1"))

            # Verify all three schemas exist
            result = conn.execute(
                text(
                    """
                    SELECT schema_name
                    FROM information_schema.schemata
                    WHERE schema_name IN ('bronze', 'silver', 'gold')
                    """
                )
            )
            found = {row[0] for row in result}
            required = {"bronze", "silver", "gold"}

            if not required.issubset(found):
                missing = required - found
                logger.error(
                    "Database health check failed — missing schemas: {}. "
                    "Run database/migrations/001_initial_schema.sql first.",
                    missing,
                )
                return False

        logger.info("Database health check passed — bronze, silver, gold schemas verified")
        return True

    except OperationalError as exc:
        logger.error(
            "Database health check failed — cannot connect to {}:{}/{} — {}",
            _config.db_host,
            _config.db_port,
            _config.db_name,
            exc,
        )
        return False


# ------------------------------------------------------------------------------
# Teardown (call at end of pipeline.py to cleanly dispose the pool)
# ------------------------------------------------------------------------------

def dispose_engine() -> None:
    """
    Close all pooled connections and dispose the engine.

    Call this at the very end of pipeline.py to cleanly release
    all DB connections before the process exits.
    """
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
        _engine = None
        _SessionFactory = None
        logger.info("Database engine disposed — all connections released")