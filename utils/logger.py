# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# utils/logger.py
# Clean Human-Friendly Logger for Data Pipelines
# ==============================================================================

import sys
from pathlib import Path
from loguru import logger as _logger

# ------------------------------------------------------------------------------
# Load Config
# ------------------------------------------------------------------------------
try:
    from utils.config import get_config

    _config = get_config()
    _logs_path: Path = _config.logs_path
    _debug: bool = _config.debug

except Exception:
    _logs_path = Path("logs")
    _debug = False

# ------------------------------------------------------------------------------
# Create Logs Directory
# ------------------------------------------------------------------------------
_logs_path.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------------------
# Remove Default Logger
# ------------------------------------------------------------------------------
_logger.remove()

# ==============================================================================
# CLEAN CONSOLE FORMAT
# ==============================================================================

_CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss}</green> | "
    "<level>{level: <7}</level> | "
    "<level>{message}</level>"
)

# ==============================================================================
# DETAILED FILE FORMAT
# ==============================================================================

_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
    "{level:<8} | "
    "{name}:{function}:{line} | "
    "{message}"
)

# ==============================================================================
# CONSOLE LOGGER
# ==============================================================================

_logger.add(
    sys.stdout,
    format=_CONSOLE_FORMAT,
    level="DEBUG" if _debug else "INFO",
    colorize=True,
    backtrace=False,
    diagnose=False,
)

# ==============================================================================
# MAIN PIPELINE LOG FILE
# ==============================================================================

_logger.add(
    str(_logs_path / "pipeline_{time:YYYYMMDD}.log"),
    format=_FILE_FORMAT,
    level="DEBUG",
    rotation="1 day",
    retention="30 days",
    compression="zip",
    encoding="utf-8",
    enqueue=True,
)

# ==============================================================================
# ERROR LOG FILE
# ==============================================================================

_logger.add(
    str(_logs_path / "errors_{time:YYYYMMDD}.log"),
    format=_FILE_FORMAT,
    level="ERROR",
    rotation="1 day",
    retention="60 days",
    compression="zip",
    encoding="utf-8",
    enqueue=True,
)

# ==============================================================================
# PUBLIC LOGGER
# ==============================================================================

logger = _logger

# ==============================================================================
# PIPELINE UI HELPERS
# ==============================================================================

def _format(prefix: str, message: str) -> str:
    return f"[{prefix}] {message}"


def _print_table(prefix: str, rows: list[tuple[str, str]]) -> None:
    for label, value in rows:
        logger.info(_format(prefix, f"{label:<22}: {value}"))


def line(char: str = "─", width: int = 70) -> None:
    logger.info(char * width)


def big_line(width: int = 70) -> None:
    logger.info("═" * width)


# ------------------------------------------------------------------------------
# PIPELINE START / END
# ------------------------------------------------------------------------------

def log_pipeline_start(label: str) -> None:
    big_line()
    logger.info(_format("PIPELINE", "🚀 PIPELINE STARTED"))
    logger.info(_format("PIPELINE", f"Job: {label}"))
    big_line()


def log_pipeline_end(label: str, status: str, elapsed_seconds: float) -> None:
    big_line()

    if status.lower() == "success":
        logger.success(_format("PIPELINE", f"✅ PIPELINE SUCCESS: {label}"))
    else:
        logger.error(_format("PIPELINE", f"❌ PIPELINE FAILED: {label}"))

    logger.info(_format("PIPELINE", f"Runtime: {elapsed_seconds:.2f} seconds"))
    big_line()


# ------------------------------------------------------------------------------
# STEP LOGGING
# ------------------------------------------------------------------------------

def log_stage_start(stage: str, step: int, name: str) -> None:
    line()
    logger.info(_format(stage, f"🔄 START STEP {step} — {name}"))
    line()


def log_stage_success(stage: str, step: int, name: str, rows: int | None = None) -> None:
    if rows is not None:
        logger.success(_format(stage, f"✅ COMPLETE STEP {step} — {name} | {rows:,} rows processed"))
    else:
        logger.success(_format(stage, f"✅ COMPLETE STEP {step} — {name}"))


def log_stage_failure(stage: str, step: int, name: str, error: Exception) -> None:
    logger.error(_format(stage, f"❌ FAILED STEP {step} — {name}"))
    logger.error(_format("ERROR", f"{error}"))


def log_step_start(step: int, name: str) -> None:
    log_stage_start("PIPELINE", step, name)


def log_step_success(step: int, name: str, rows: int | None = None) -> None:
    log_stage_success("PIPELINE", step, name, rows)


def log_step_failure(step: int, name: str, error: Exception) -> None:
    log_stage_failure("PIPELINE", step, name, error)


# ------------------------------------------------------------------------------
# SUMMARY AND DASHBOARD HELPERS
# ------------------------------------------------------------------------------

def log_warning(message: str, stage: str = "WARNING") -> None:
    logger.warning(_format(stage, message))


def log_error(message: str, stage: str = "ERROR") -> None:
    logger.error(_format(stage, message))


def print_kpi_summary(
    title: str,
    period: str,
    generated_at: str,
    overview: list[tuple[str, str]],
    rates: list[tuple[str, str]],
    changes: list[tuple[str, str]],
    top_programs: list[tuple[str, str]] | None = None,
    at_risk_programs: list[tuple[str, str]] | None = None,
) -> None:
    big_line()
    logger.success(_format("KPI", title))
    logger.info(_format("KPI", f"Period       : {period}"))
    logger.info(_format("KPI", f"Generated    : {generated_at}"))
    big_line()
    _print_table("KPI", overview)
    line()
    logger.info(_format("KPI", "Rates:"))
    _print_table("KPI", rates)
    line()
    logger.info(_format("KPI", "Changes:"))
    _print_table("KPI", changes)

    if top_programs:
        line()
        logger.info(_format("KPI", "Top Programs by Enrollment:"))
        for program_code, enrolled in top_programs:
            logger.info(_format("KPI", f"{program_code:<10} — {enrolled}"))

    if at_risk_programs:
        line()
        logger.info(_format("KPI", "At-Risk Programs (dropout ≥ 20%):"))
        for program_code, detail in at_risk_programs:
            logger.info(_format("KPI", f"{program_code:<10} — {detail}"))

    big_line()


def print_ml_summary(
    title: str,
    performance: list[tuple[str, str]],
    risk_summary: list[tuple[str, str]],
    feature_importances: list[tuple[str, str]] | None = None,
) -> None:
    big_line()
    logger.success(_format("ML", title))
    big_line()
    logger.info(_format("ML", "Model Performance:"))
    _print_table("ML", performance)
    line()
    logger.info(_format("ML", "Risk Summary:"))
    _print_table("ML", risk_summary)

    if feature_importances:
        line()
        logger.info(_format("ML", "Feature Importance:"))
        for feature, importance in feature_importances:
            logger.info(_format("ML", f"{feature:<25} {importance}"))

    big_line()


# ------------------------------------------------------------------------------
# OPTIONAL SHORTCUT HELPERS
# ------------------------------------------------------------------------------

def info(message: str) -> None:
    logger.info(f"ℹ️ {message}")


def success(message: str) -> None:
    logger.success(f"✅ {message}")


def warning(message: str) -> None:
    logger.warning(f"⚠️ {message}")


def error(message: str) -> None:
    logger.error(f"❌ {message}")