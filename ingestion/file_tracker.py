# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# ingestion/file_tracker.py
# Tracks which source files have been ingested to prevent duplicate loads
# ==============================================================================

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import text

from database.connection import get_session
from utils.logger import logger


def get_file_hash(file_path: str | Path) -> str:
    """
    Compute the SHA-256 hash of a file's contents.

    Used to detect if the same file has been re-uploaded under a different
    name, or if a file was modified after a previous ingestion attempt.

    Returns a 64-character hex string.
    """
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def is_file_already_ingested(file_path: str | Path) -> bool:
    """
    Check if a file has been successfully ingested before.

    Checks by filename AND file hash — catches both exact duplicates
    and files uploaded under a different name.

    Returns True if the file should be skipped.
    """
    file_path = Path(file_path)
    fname     = file_path.name

    try:
        file_hash = get_file_hash(file_path)
    except Exception as exc:
        logger.error("Cannot compute hash for {}: {}", fname, exc)
        return False

    try:
        with get_session() as session:
            # Check by filename
            by_name = session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM bronze.ingestion_log
                    WHERE source_file = :fname
                      AND status IN ('success', 'partial')
                    """
                ),
                {"fname": fname},
            ).scalar()

            if by_name > 0:
                logger.info(
                    "File '{}' already ingested (matched by filename). Skipping.",
                    fname,
                )
                return True

            # Check by hash (catches renamed duplicates)
            by_hash = session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM bronze.ingestion_log
                    WHERE file_hash = :fhash
                      AND status IN ('success', 'partial')
                    """
                ),
                {"fhash": file_hash},
            ).scalar()

            if by_hash > 0:
                logger.warning(
                    "File '{}' matches a previously ingested file by content hash. "
                    "Skipping to prevent duplicate data.",
                    fname,
                )
                return True

    except Exception as exc:
        # If the file_hash column doesn't exist yet (older schema), fall through
        logger.debug("Hash check skipped (column may not exist): {}", exc)

    return False


def list_ingested_files() -> list[dict]:
    """
    Return a list of all successfully ingested files.

    Used by pipeline.py to print a status summary at startup.
    Returns a list of dicts with keys: source_file, status, rows_inserted, completed_at.
    """
    try:
        with get_session() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        source_file,
                        status,
                        SUM(rows_inserted) AS total_rows,
                        MAX(completed_at)  AS last_ingested
                    FROM bronze.ingestion_log
                    GROUP BY source_file, status
                    ORDER BY MAX(completed_at) DESC
                    """
                )
            ).fetchall()

        return [
            {
                "source_file":    row[0],
                "status":         row[1],
                "rows_inserted":  row[2],
                "last_ingested":  row[3],
            }
            for row in rows
        ]
    except Exception as exc:
        logger.error("Cannot retrieve ingestion file list: {}", exc)
        return []


def print_ingestion_status() -> None:
    """Print a formatted table of all ingested files to the console."""
    files = list_ingested_files()
    if not files:
        logger.info("No files have been ingested yet.")
        return

    logger.info("=" * 70)
    logger.info("  Ingested Files")
    logger.info("=" * 70)
    for f in files:
        logger.info(
            "  {:40s} | {:8s} | {:>6} rows | {}",
            f["source_file"],
            f["status"],
            f["rows_inserted"] or 0,
            f["last_ingested"].strftime("%Y-%m-%d %H:%M") if f["last_ingested"] else "—",
        )
    logger.info("=" * 70)