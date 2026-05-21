# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# pipeline.py — Manual run entry point
#
# FIXES APPLIED:
#   Fix 1 — --force flag bypasses Bronze idempotency check
#   Fix 3 — 'skipped' status is distinct from 'success'
#   Fix 5 — pipeline only reports SUCCESS when rows were actually processed
#   Fix 7 — each stage validates row counts before proceeding downstream
#   Fix 8 -- --reprocess clears ingestion log without touching data tables
#
# Usage:
#   python pipeline.py                           # full pipeline
#   python pipeline.py --file data/raw/x.xlsx   # specific file
#   python pipeline.py --force                  # bypass idempotency check
#   python pipeline.py --reprocess              # clear log + re-ingest
#   python pipeline.py --skip-forecast          # skip forecasting
#   python pipeline.py --skip-ml                # skip all ML
#   python pipeline.py --kpi-only               # KPIs from existing Gold only
#   python pipeline.py --label SEM1_2024        # tag this run
# ==============================================================================

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from database.connection import check_connection, dispose_engine, get_session
from database.models.gold_models import GoldPipelineRunLog
from utils.config import get_config, print_config_summary
from utils.logger import log_pipeline_end, log_pipeline_start, logger

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
  python pipeline.py --force
  python pipeline.py --reprocess
  python pipeline.py --skip-ml
  python pipeline.py --kpi-only
  python pipeline.py --label SEM1_2024
        """,
    )
    parser.add_argument("--file",    "-f", type=str, default=None,
        help="Path to specific Excel file (default: all files in data/raw/)")
    parser.add_argument("--label",   "-l", type=str,
        default=f"manual_{datetime.now().strftime('%Y%m%d_%H%M')}",
        help="Label for this pipeline run")

    # FIX 1 + FIX 8
    parser.add_argument("--force", "-F", action="store_true", default=False,
        help="Bypass idempotency check — re-ingest even if file was already loaded")
    parser.add_argument("--reprocess", "-R", action="store_true", default=False,
        help="Clear previous ingestion log entry then re-ingest (safer than --force)")

    parser.add_argument("--skip-forecast", action="store_true", default=False,
        help="Skip Prophet forecasting")
    parser.add_argument("--skip-ml",       action="store_true", default=False,
        help="Skip all ML models (forecasting + at-risk)")
    parser.add_argument("--kpi-only",      action="store_true", default=False,
        help="Only compute KPIs from existing Gold data — skip all ingestion")
    parser.add_argument("--semesters-ahead", type=int, default=4,
        help="Semesters to forecast ahead (default: 4)")
    return parser.parse_args()


# ==============================================================================
# Stage runners  — each returns (ok: bool, row_count: int)
# ==============================================================================

def stage_bronze(file_path, force: bool, reprocess: bool) -> tuple[bool, int]:
    from ingestion.load_bronze import run_bronze
    result = run_bronze(file_path, force=force, reprocess=reprocess)

    # FIX 3 + FIX 5: 'skipped' is not success when we need new data
    if result.was_skipped:
        logger.warning(
            "Bronze was SKIPPED — '{}' already ingested. "
            "Use --force or --reprocess to re-run ingestion.",
            result.source_file,
        )
        return False, 0

    ok = result.status in ("success", "partial") and result.has_data
    return ok, result.total_rows_loaded


def stage_silver(bronze_batch_id=None) -> tuple[bool, int]:
    from transformation.silver_transform import run_silver
    result = run_silver(bronze_batch_id)

    # FIX 7: treat 'skipped' as a hard stop
    if result.status == "skipped":
        logger.error(
            "Silver was SKIPPED — Bronze batch is empty. "
            "Check Bronze ingestion logs above."
        )
        return False, 0

    ok = result.status == "success" and result.has_data
    return ok, result.total_inserted


def stage_gold() -> tuple[bool, int]:
    from transformation.gold_aggregate import run_gold
    result = run_gold()
    ok = result.status == "success" and result.total_rows_written > 0
    return ok, result.total_rows_written


def stage_kpis() -> bool:
    from analytics.kpi_engine import run_kpi_engine
    report = run_kpi_engine()
    return report.status == "success"


def stage_forecast(semesters_ahead: int) -> bool:
    from analytics.forecasting import run_forecast
    report = run_forecast(semesters_ahead=semesters_ahead)
    return report.status == "success"


def stage_at_risk() -> bool:
    from analytics.at_risk_model import run_at_risk_model
    report = run_at_risk_model()
    return report.status == "success"


def stage_cohort() -> bool:
    from analytics.cohort_analysis import run_cohort_analysis
    report = run_cohort_analysis()
    return report.status == "success"


# ==============================================================================
# Pipeline run logger
# ==============================================================================

def _start_run_log(label: str) -> int | None:
    try:
        run = GoldPipelineRunLog(run_label=label, status="running", triggered_by="manual")
        with get_session() as session:
            session.add(run)
            session.flush()
            return run.run_id
    except Exception as exc:
        logger.warning("Could not create run log: {}", exc)
        return None


def _finish_run_log(run_id, status, bronze_rows, silver_rows, gold_rows, error=None):
    if run_id is None:
        return
    try:
        from sqlalchemy import text
        with get_session() as session:
            session.execute(
                text(
                    """
                    UPDATE gold.pipeline_run_log SET
                        status=:s, completed_at=NOW(),
                        bronze_rows=:b, silver_rows=:si,
                        gold_rows=:g, error_message=:e
                    WHERE run_id=:rid
                    """
                ),
                dict(s=status, b=bronze_rows, si=silver_rows, g=gold_rows, e=error, rid=run_id),
            )
    except Exception as exc:
        logger.warning("Could not update run log: {}", exc)


# ==============================================================================
# Main pipeline
# ==============================================================================

def main() -> int:
    args    = _parse_args()
    started = time.monotonic()
    label   = args.label

    print_config_summary()
    log_pipeline_start(label)

    # ── Health check ──────────────────────────────────────────────────
    if not check_connection():
        logger.error(
            "Database connection failed. Check .env and ensure PostgreSQL is running. "
            "Fresh install? Run: psql -U postgres -d neust_analytics "
            "-f database/migrations/001_initial_schema.sql"
        )
        return 1

    bronze_rows = silver_rows = gold_rows = 0
    overall_status = "success"
    run_id = _start_run_log(label)

    try:
        # ── KPI-only mode ─────────────────────────────────────────────
        if args.kpi_only:
            logger.info("KPI-only mode — skipping ingestion and transformation.")
            ok = stage_kpis()
            # FIX 5: kpi-only always has existing Gold data — success if no crash
            overall_status = "success" if ok else "partial"
            log_pipeline_end(label, overall_status, time.monotonic() - started)
            _finish_run_log(run_id, overall_status, 0, 0, 0)
            return 0 if ok else 1

        # ── Stage 1 — Bronze ──────────────────────────────────────────
        ok, bronze_rows = stage_bronze(args.file, force=args.force, reprocess=args.reprocess)
        if not ok:
            # FIX 7: hard-stop — no point running Silver/Gold on empty Bronze
            logger.error(
                "Pipeline ABORTED at Bronze stage. "
                "No rows were loaded. Downstream stages will not run. "
                "Tip: use --force to re-ingest an already-loaded file, "
                "or --reprocess to clear the log and start fresh."
            )
            overall_status = "failed"
            _finish_run_log(run_id, "failed", 0, 0, 0, "Bronze stage produced 0 rows")
            log_pipeline_end(label, "failed", time.monotonic() - started)
            return 1

        # ── Stage 2 — Silver ──────────────────────────────────────────
        ok, silver_rows = stage_silver()
        if not ok:
            logger.error(
                "Pipeline ABORTED at Silver stage. "
                "0 rows written to Silver. Gold and Analytics will not run."
            )
            overall_status = "failed"
            _finish_run_log(run_id, "failed", bronze_rows, 0, 0, "Silver stage produced 0 rows")
            log_pipeline_end(label, "failed", time.monotonic() - started)
            return 1

        # ── Stage 3 — Gold ────────────────────────────────────────────
        ok, gold_rows = stage_gold()
        if not ok:
            logger.error(
                "Pipeline ABORTED at Gold stage. "
                "0 rows written to Gold. Analytics will not run."
            )
            overall_status = "failed"
            _finish_run_log(run_id, "failed", bronze_rows, silver_rows, 0, "Gold stage produced 0 rows")
            log_pipeline_end(label, "failed", time.monotonic() - started)
            return 1

        # ── Stage 4 — KPIs ────────────────────────────────────────────
        if not stage_kpis():
            logger.warning("KPI computation failed — continuing with remaining stages.")
            overall_status = "partial"

        # ── Stage 5 — Forecasting ─────────────────────────────────────
        if not args.skip_ml and not args.skip_forecast:
            if not stage_forecast(args.semesters_ahead):
                logger.warning("Forecasting failed — continuing.")
                overall_status = "partial"
        else:
            logger.info("Forecasting skipped (--skip-ml or --skip-forecast).")

        # ── Stage 6 — At-risk model ───────────────────────────────────
        if not args.skip_ml:
            if not stage_at_risk():
                logger.warning("At-risk model failed — continuing.")
                overall_status = "partial"
        else:
            logger.info("At-risk model skipped (--skip-ml).")

        # ── Stage 7 — Cohort analysis ─────────────────────────────────
        if not stage_cohort():
            logger.warning("Cohort analysis failed — continuing.")
            overall_status = "partial"

        # FIX 5: Only print SUCCESS when rows were actually processed
        if overall_status == "success":
            logger.info(
                "Pipeline SUCCESS — bronze={:,} | silver={:,} | gold={:,} rows",
                bronze_rows, silver_rows, gold_rows,
            )
        else:
            logger.warning(
                "Pipeline completed with status={} — some stages had issues. "
                "Check logs above for details.",
                overall_status.upper(),
            )

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
        log_pipeline_end(label, overall_status, elapsed)
        _finish_run_log(run_id, overall_status, bronze_rows, silver_rows, gold_rows)
        dispose_engine()

    return 0 if overall_status in ("success", "partial") else 1


# ==============================================================================
# Entry point
# ==============================================================================

if __name__ == "__main__":
    sys.exit(main())