# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# utils/config.py
# Loads and validates all environment variables from .env
# ==============================================================================

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# ------------------------------------------------------------------------------
# Load .env from project root (two levels up from utils/)
# ------------------------------------------------------------------------------
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=False)


# ------------------------------------------------------------------------------
# Config dataclass — single source of truth for all settings
# ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    """
    Immutable configuration object populated from environment variables.

    All fields have defaults so the system stays runnable in a minimal
    environment. Required fields (DB credentials) raise ValueError if missing.

    Frozen=True means this object cannot be mutated after creation —
    safe to share across modules without accidental overwrites.
    """

    # ── Database ────────────────────────────────────────────────────────────
    db_host:     str = field(default="localhost")
    db_port:     int = field(default=5432)
    db_name:     str = field(default="neust_analytics")
    db_user:     str = field(default="postgres")
    db_password: str = field(default="")

    # ── Data paths ───────────────────────────────────────────────────────────
    raw_data_path:       Path = field(default=Path("data/raw"))
    processed_data_path: Path = field(default=Path("data/processed"))
    exports_path:        Path = field(default=Path("data/exports"))
    models_path:         Path = field(default=Path("data/models"))
    logs_path:           Path = field(default=Path("logs"))

    # ── Pipeline settings ────────────────────────────────────────────────────
    pipeline_label:       str  = field(default="manual")
    batch_size:           int  = field(default=1000)   # rows per DB insert batch
    debug:                bool = field(default=False)

    # ── Academic settings ────────────────────────────────────────────────────
    # Semester codes exactly as they appear in the Excel source file
    semester_map: dict = field(default_factory=lambda: {
        "1st semester": 1,
        "first semester": 1,
        "sem 1": 1,
        "semester 1": 1,
        "1": 1,
        "2nd semester": 2,
        "second semester": 2,
        "sem 2": 2,
        "semester 2": 2,
        "2": 2,
        "summer": 3,
        "summer term": 3,
        "midyear": 3,
        "3": 3,
    })

    # Year level codes as they appear in Excel → canonical integer
    year_level_map: dict = field(default_factory=lambda: {
        "1st year":  1, "first year":  1, "year 1": 1, "freshman":  1, "1": 1,
        "2nd year":  2, "second year": 2, "year 2": 2, "sophomore": 2, "2": 2,
        "3rd year":  3, "third year":  3, "year 3": 3, "junior":    3, "3": 3,
        "4th year":  4, "fourth year": 4, "year 4": 4, "senior":    4, "4": 4,
        "5th year":  5, "fifth year":  5, "year 5": 5, "5": 5,
        "6th year":  6, "sixth year":  6, "year 6": 6, "6": 6,
    })

    # Gender codes as they appear in Excel → canonical string
    gender_map: dict = field(default_factory=lambda: {
        "male":   "Male",
        "m":      "Male",
        "female": "Female",
        "f":      "Female",
        "other":  "Other",
        "n/a":    "Not Specified",
        "na":     "Not Specified",
        "":       "Not Specified",
    })


def _resolve_path(key: str, default: str) -> Path:
    """Read a path from env, resolve relative to project root."""
    raw = os.getenv(key, default)
    p = Path(raw)
    if not p.is_absolute():
        # Resolve relative paths from project root
        p = Path(__file__).resolve().parent.parent / p
    return p


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("true", "1", "yes")


def _build_config() -> Config:
    """Read all env vars and return a validated Config instance."""

    db_password = os.getenv("DB_PASSWORD", "")
    if not db_password:
        import warnings
        warnings.warn(
            "DB_PASSWORD is not set in .env — using empty string. "
            "Set a password for any non-local environment.",
            stacklevel=3,
        )

    return Config(
        # Database
        db_host=os.getenv("DB_HOST", "localhost"),
        db_port=int(os.getenv("DB_PORT", "5432")),
        db_name=os.getenv("DB_NAME", "neust_analytics"),
        db_user=os.getenv("DB_USER", "postgres"),
        db_password=db_password,

        # Paths
        raw_data_path=       _resolve_path("RAW_DATA_PATH",       "data/raw"),
        processed_data_path= _resolve_path("PROCESSED_DATA_PATH", "data/processed"),
        exports_path=        _resolve_path("EXPORTS_PATH",         "data/exports"),
        models_path=         _resolve_path("MODELS_PATH",          "data/models"),
        logs_path=           _resolve_path("LOGS_PATH",            "logs"),

        # Pipeline
        pipeline_label=os.getenv("PIPELINE_LABEL", "manual"),
        batch_size=int(os.getenv("BATCH_SIZE", "1000")),
        debug=_parse_bool(os.getenv("DEBUG", "false")),
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    """
    Return the singleton Config instance.

    Cached after first call — safe to import anywhere with zero overhead.

    Usage:
        from utils.config import get_config
        config = get_config()
        print(config.db_host)
    """
    config = _build_config()
    return config


def print_config_summary() -> None:
    """
    Print a safe summary of the loaded config (no passwords).
    Called by pipeline.py at startup for operator confirmation.
    """
    c = get_config()
    print("=" * 60)
    print("  NEUST Analytics — Configuration Summary")
    print("=" * 60)
    print(f"  Database   : {c.db_user}@{c.db_host}:{c.db_port}/{c.db_name}")
    print(f"  Raw data   : {c.raw_data_path}")
    print(f"  Processed  : {c.processed_data_path}")
    print(f"  Exports    : {c.exports_path}")
    print(f"  Models     : {c.models_path}")
    print(f"  Logs       : {c.logs_path}")
    print(f"  Batch size : {c.batch_size}")
    print(f"  Debug mode : {c.debug}")
    print(f"  Label      : {c.pipeline_label}")
    print("=" * 60)