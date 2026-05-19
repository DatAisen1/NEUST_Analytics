# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# utils/logger.py
# Loguru-based logger with console + rotating file output
# ==============================================================================

import sys
from pathlib import Path

from loguru import logger as _logger

# ------------------------------------------------------------------------------
# Import config — but guard against circular imports during early startup
# ------------------------------------------------------------------------------
try:
    from utils.config import get_config
    _config = get_config()
    _logs_path: Path = _config.logs_path
    _debug: bool = _config.debug
except Exception:
    # Fallback if config is not yet available (e.g. during testing)
    _logs_path = Path("logs")
    _debug = False

# ------------------------------------------------------------------------------
# Ensure logs directory exists
# ------------------------------------------------------------------------------
_logs_path.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------------------
# Log format
# ------------------------------------------------------------------------------
_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "<level>{message}</level>"
)

_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
    "{level: <8} | "
    "{name}:{function}:{line} | "
    "{message}"
)

# ------------------------------------------------------------------------------
# Configure logger
# ------------------------------------------------------------------------------

# Remove the default loguru handler
_logger.remove()

# ── Console handler ──────────────────────────────────────────────────────────
_logger.add(
    sys.stdout,
    format=_CONSOLE_FORMAT,
    level="DEBUG" if _debug else "INFO",
    colorize=True,
    backtrace=True,
    diagnose=_debug,    # show variable values in tracebacks only in debug mode
)

# ── Daily rotating file handler (one file per day) ───────────────────────────
_logger.add(
    str(_logs_path / "pipeline_{time:YYYYMMDD}.log"),
    format=_FILE_FORMAT,
    level="DEBUG",          # always capture DEBUG to file for full audit trail
    rotation="00:00",       # new file at midnight
    retention="30 days",    # keep 30 days of logs
    compression="zip",      # compress old logs to save disk space
    backtrace=True,
    diagnose=False,         # never write variable values to file (may contain PII)
    encoding="utf-8",
    enqueue=True,           # non-blocking writes — pipeline speed unaffected
)

# ── Error-only file handler (quick reference for failures) ───────────────────
_logger.add(
    str(_logs_path / "errors_{time:YYYYMMDD}.log"),
    format=_FILE_FORMAT,
    level="ERROR",
    rotation="00:00",
    retention="60 days",    # keep errors longer than regular logs
    compression="zip",
    backtrace=True,
    diagnose=False,
    encoding="utf-8",
    enqueue=True,
)

# ------------------------------------------------------------------------------
# Public logger instance
# All modules import this:
#   from utils.logger import logger
# ------------------------------------------------------------------------------
logger = _logger

# ------------------------------------------------------------------------------
# Pipeline step helpers
# Used by pipeline.py to print clean step banners in the console
# ------------------------------------------------------------------------------

def log_step_start(step: int, name: str) -> None:
    """Log a clean banner when a pipeline step begins."""
    logger.info("=" * 60)
    logger.info("  STEP {}  |  {}", step, name.upper())
    logger.info("=" * 60)


def log_step_success(step: int, name: str, rows: int | None = None) -> None:
    """Log a success banner when a pipeline step completes."""
    suffix = f" — {rows:,} rows processed" if rows is not None else ""
    logger.success("  STEP {} COMPLETE  |  {}{}", step, name.upper(), suffix)


def log_step_failure(step: int, name: str, error: Exception) -> None:
    """Log a failure banner when a pipeline step raises an exception."""
    logger.error("  STEP {} FAILED  |  {}  |  {}", step, name.upper(), error)


def log_pipeline_start(label: str) -> None:
    """Log the pipeline start banner."""
    logger.info("*" * 60)
    logger.info("  NEUST ANALYTICS PIPELINE STARTED")
    logger.info("  Label : {}", label)
    logger.info("*" * 60)


def log_pipeline_end(label: str, status: str, elapsed_seconds: float) -> None:
    """Log the pipeline completion banner."""
    logger.info("*" * 60)
    logger.info("  PIPELINE {} — {}", status.upper(), label)
    logger.info("  Elapsed : {:.2f}s", elapsed_seconds)
    logger.info("*" * 60)