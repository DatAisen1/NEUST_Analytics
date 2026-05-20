# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# pipeline.py — Manual run entry point
# Executes all pipeline stages in order: Bronze → Silver → Gold → Analytics
#
# Usage:
#   python pipeline.py                          # full pipeline, latest file
#   python pipeline.py --file data/raw/x.xlsx  # specific file
#   python pipeline.py --skip-forecast         # skip forecasting (faster)
#   python pipeline.py --skip-ml               # skip ML models
#   python pipeline.py --kpi-only              # KPIs only (no ingestion)
# ==============================================================================

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from database.connection import check_connection, dispose_engine, get_session
from database.models.gold_models import GoldPipelineRunLog
from utils.config import get_config, print_config_summary
from utils.logger import (
    log_pipeline_end,
    log_pipeline_start,
    log_step_failure,
    logger,
)

_config = get_config()


# ==============================================================================
# CLI argument parser
# ==============================================================================

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="NEUST Academic Analytics Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pipeline.py
  python pipeline.py --file data/raw/enrollment_AY2024.xlsx
  python pipeline.py --skip-forecast
  python pipeline.py --kpi-only
  python pipeline.py --label SEM1_2024
        """,
    )
    parser.add_argument(
        "--file", "-f",
        type=str, default=None,
        help="Path to a specific Excel file to ingest (default: all files in data/raw/)",
    )
    parser.add_argument(
        "--label", "-l",
        type=str, default=f"manual_{datetime.now().strftime('%Y%m%d_%H%M')}",
        help="Label for this pipeline run (shown in logs and audit table)",
    )
    parser.add_argument(
        "--skip-forecast", action="store_true", default=False,
        help="Skip Prophet forecasting (use when data is too sparse)",
    )
    parser.add_argument(
        "--skip-ml", action="store_true", default=False,
        help="Skip ML models (at-risk + forecasting)",
    )
    parser.add_argument(
        "--kpi-only", action="store_true", default=False,
        help="Only compute KPIs from existing Gold data — skip all ingestion",
    )
    parser.add_argument(
        "--semesters-ahead", type=int, default=4,
        help="Number of semesters to forecast ahead (default: 4)",
    )
    return parser.parse_args()


# ==============================================================================
# Stage runners
# ==============================================================================

def stage_bronze(file_path: str | None) -> tuple[bool, int]:
    """Stage 1 — Ingest Excel → Bronze."""
    from ingestion.load_bronze import run_bronze
    result = run_bronze(file_path)
    success = result.status in ("success", "partial")
    return success, result.total_rows_loaded


def stage_silver(bronze_batch_id=None) -> tuple[bool, int]:
    """Stage 2 — Bronze → Silver transformation."""
    from transformation.silver_transform import run_silver
    result = run_silver(bronze_batch_id)
    success = result.status == "success"
    return success, result.total_inserted


def stage_gold() -> tuple[bool, int]:
    """Stage 3 — Silver → Gold aggregation."""
    from transformation.gold_aggregate import run_gold
    result = run_gold()
    success = result.status == "success"
    return success, result.total_rows_written


def stage_kpis() -> bool:
    """Stage 4 — KPI computation from Gold."""
    from analytics.kpi_engine import run_kpi_engine
    report = run_kpi_engine()
    return report.status == "success"


def stage_forecast(semesters_ahead: int) -> bool:
    """Stage 5 — Enrollment forecasting."""
    from analytics.forecasting import run_forecast
    report = run_forecast(semesters_ahead=semesters_ahead)
    return report.status in ("success",)


def stage_at_risk() -> bool:
    """Stage 6 — At-risk dropout model."""
    from analytics.at_risk_model import run_at_risk_model
    report = run_at_risk_model()
    return report.status == "success"


def stage_cohort() -> bool:
    """Stage 7 — Cohort survival analysis."""
    from analytics.cohort_analysis import run_cohort_analysis
    report = run_cohort_analysis()
    return report.status == "success"


# ==============================================================================
# Pipeline run logger
# ==============================================================================

def _start_run_log(label: str) -> int | None:
    """Insert a pipeline_run_log record and return run_id."""
    try:
        run = GoldPipelineRunLog(
            run_label=label,
            status="running",
            triggered_by="manual",
        )
        with get_session() as session:
            session.add(run)
            session.flush()
            run_id = run.run_id
        return run_id
    except Exception as exc:
        logger.warning("Could not create run log: {}", exc)
        return None


def _finish_run_log(
    run_id: int | None,
    status: str,
    bronze_rows: int,
    silver_rows: int,
    gold_rows: int,
    error: str | None = None,
) -> None:
    if run_id is None:
        return
    try:
        from sqlalchemy import text
        with get_session() as session:
            session.execute(
                text(
                    """
                    UPDATE gold.pipeline_run_log SET
                        status        = :status,
                        completed_at  = NOW(),
                        bronze_rows   = :bronze,
                        silver_rows   = :silver,
                        gold_rows     = :gold,
                        error_message = :err
                    WHERE run_id = :rid
                    """
                ),
                dict(
                    status=status,
                    bronze=bronze_rows,
                    silver=silver_rows,
                    gold=gold_rows,
                    err=error,
                    rid=run_id,
                ),
            )
    except Exception as exc:
        logger.warning("Could not update run log: {}", exc)


# ==============================================================================
# Main pipeline
# ==============================================================================

def main() -> int:
    """
    Main pipeline entry point.
    Returns exit code: 0 = success, 1 = failure.
    """
    args      = _parse_args()
    started   = time.monotonic()
    run_label = args.label

    # ── Startup ───────────────────────────────────────────────────────
    print_config_summary()
    log_pipeline_start(run_label)

    # ── Health check ──────────────────────────────────────────────────
    logger.info("Checking database connection ...")
    if not check_connection():
        logger.error(
            "Database connection failed. "
            "Check your .env file and ensure PostgreSQL is running. "
            "If this is a fresh install, run: "
            "psql -U postgres -d neust_analytics -f database/migrations/001_initial_schema.sql"
        )
        return 1

    bronze_rows = silver_rows = gold_rows = 0
    overall_status = "success"
    run_id = _start_run_log(run_label)

    try:
        # ── KPI-only mode ─────────────────────────────────────────────
        if args.kpi_only:
            logger.info("KPI-only mode — skipping ingestion and transformation.")
            ok = stage_kpis()
            if not ok:
                overall_status = "partial"
            log_pipeline_end(run_label, overall_status, time.monotonic() - started)
            return 0 if overall_status == "success" else 1

        # ── Stage 1 — Bronze ──────────────────────────────────────────
        ok, bronze_rows = stage_bronze(args.file)
        if not ok:
            logger.error("Bronze ingestion failed — aborting pipeline.")
            overall_status = "failed"
            _finish_run_log(run_id, "failed", 0, 0, 0)
            return 1

        # ── Stage 2 — Silver ──────────────────────────────────────────
        ok, silver_rows = stage_silver()
        if not ok:
            logger.error("Silver transformation failed — aborting pipeline.")
            overall_status = "failed"
            _finish_run_log(run_id, "failed", bronze_rows, 0, 0)
            return 1

        # ── Stage 3 — Gold ────────────────────────────────────────────
        ok, gold_rows = stage_gold()
        if not ok:
            logger.error("Gold aggregation failed — aborting pipeline.")
            overall_status = "failed"
            _finish_run_log(run_id, "failed", bronze_rows, silver_rows, 0)
            return 1

        # ── Stage 4 — KPIs ────────────────────────────────────────────
        ok = stage_kpis()
        if not ok:
            logger.warning("KPI computation failed — continuing.")
            overall_status = "partial"

        # ── Stage 5 — Forecasting ─────────────────────────────────────
        if not args.skip_ml and not args.skip_forecast:
            ok = stage_forecast(args.semesters_ahead)
            if not ok:
                logger.warning("Forecasting failed or skipped — continuing.")
                overall_status = "partial"
        else:
            logger.info("Forecasting skipped (--skip-ml or --skip-forecast).")

        # ── Stage 6 — At-risk model ───────────────────────────────────
        if not args.skip_ml:
            ok = stage_at_risk()
            if not ok:
                logger.warning("At-risk model failed or skipped — continuing.")
                overall_status = "partial"
        else:
            logger.info("At-risk model skipped (--skip-ml).")

        # ── Stage 7 — Cohort analysis ─────────────────────────────────
        ok = stage_cohort()
        if not ok:
            logger.warning("Cohort analysis failed — continuing.")
            overall_status = "partial"

    except KeyboardInterrupt:
        logger.warning("Pipeline interrupted by user.")
        overall_status = "failed"
        _finish_run_log(run_id, "failed", bronze_rows, silver_rows, gold_rows)
        return 1

    except Exception as exc:
        logger.exception("Unexpected pipeline error: {}", exc)
        overall_status = "failed"
        _finish_run_log(run_id, "failed", bronze_rows, silver_rows, gold_rows, str(exc))
        return 1

    finally:
        elapsed = time.monotonic() - started
        log_pipeline_end(run_label, overall_status, elapsed)
        _finish_run_log(run_id, overall_status, bronze_rows, silver_rows, gold_rows)
        dispose_engine()

    return 0 if overall_status == "success" else 1


# ==============================================================================
# Entry point
# ==============================================================================

if __name__ == "__main__":
    sys.exit(main())