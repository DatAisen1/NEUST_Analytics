# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# transformation/gold_aggregate.py
# Silver → Gold aggregation: builds the star schema and pre-computes all KPIs
# ==============================================================================

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from database.connection import get_session
from database.models.gold_models import (
    GoldAggCollegeSummary,
    GoldAggProgramPerformance,
    GoldDimProgram,
    GoldDimTime,
    GoldDimYearLevel,
    GoldFactEnrollmentMetrics,
)
from transformation.rules_engine import KPIRules
from utils.date_helpers import semester_to_label
from utils.logger import log_step_failure, log_step_start, log_step_success, logger


# ==============================================================================
# Result dataclass
# ==============================================================================

@dataclass
class AggregationResult:
    """Summary of a Silver → Gold aggregation run."""

    fact_rows_written:          int = 0
    agg_program_rows_written:   int = 0
    agg_college_rows_written:   int = 0
    dim_time_rows_written:      int = 0
    dim_program_rows_written:   int = 0
    status:                     str = "pending"
    error_message:              str | None = None
    elapsed_seconds:            float = 0.0
    warnings:                   list[str] = field(default_factory=list)

    @property
    def total_rows_written(self) -> int:
        return (
            self.fact_rows_written
            + self.agg_program_rows_written
            + self.agg_college_rows_written
        )


# ==============================================================================
# Gold aggregator
# ==============================================================================

class GoldAggregator:
    """
    Aggregates Silver data into the Gold star schema.

    Steps:
        1. Sync dim_time      — one row per unique (academic_year, semester)
        2. Sync dim_program   — one row per unique program_code
        3. Build fact table   — join enrollment_flow + student_outcomes, compute KPIs
        4. Build agg_program  — program-level rollup with trend deltas
        5. Build agg_college  — college-level rollup

    Fully idempotent — all inserts use ON CONFLICT DO UPDATE.
    Gold is rebuilt from Silver on every run; no Gold state is ever trusted.

    Usage:
        aggregator = GoldAggregator()
        result = aggregator.run()
    """

    def __init__(self) -> None:
        self._started = time.monotonic()
        self._time_id_cache:    dict[tuple, int] = {}   # (academic_year, sem) → time_id
        self._program_id_cache: dict[str, int]   = {}   # program_code → program_id
        self._year_level_cache: dict[int, int]   = {}   # year_level → year_level_id

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> AggregationResult:
        """Execute the full Silver → Gold aggregation pipeline."""
        result = AggregationResult()
        log_step_start(3, "Gold aggregation")

        try:
            # Step 1 — sync dimensions
            logger.info("Step 1/5 — Syncing dim_time ...")
            result.dim_time_rows_written    = self._sync_dim_time()

            logger.info("Step 2/5 — Syncing dim_program ...")
            result.dim_program_rows_written = self._sync_dim_program()

            logger.info("Step 3/5 — Building fact_enrollment_metrics ...")
            result.fact_rows_written        = self._build_fact_table()

            logger.info("Step 4/5 — Building agg_program_performance ...")
            result.agg_program_rows_written = self._build_agg_program()

            logger.info("Step 5/5 — Building agg_college_summary ...")
            result.agg_college_rows_written = self._build_agg_college()

            result.status = "success"

        except Exception as exc:
            result.status        = "failed"
            result.error_message = str(exc)
            logger.exception("Gold aggregation failed: {}", exc)
            log_step_failure(3, "Gold aggregation", exc)

        finally:
            result.elapsed_seconds = time.monotonic() - self._started

        if result.status == "success":
            log_step_success(3, "Gold aggregation", rows=result.total_rows_written)
            logger.info(
                "Gold complete — fact={} | agg_program={} | agg_college={} | {:.2f}s",
                result.fact_rows_written,
                result.agg_program_rows_written,
                result.agg_college_rows_written,
                result.elapsed_seconds,
            )

        return result

    # ------------------------------------------------------------------
    # Step 1 — dim_time
    # ------------------------------------------------------------------

    def _sync_dim_time(self) -> int:
        """Upsert one dim_time row per unique (academic_year, semester) in Silver."""
        with get_session() as session:
            periods = session.execute(
                text(
                    """
                    SELECT DISTINCT academic_year, semester, year_start, year_end
                    FROM silver.academic_periods
                    ORDER BY academic_year, semester
                    """
                )
            ).fetchall()

        count = 0
        for row in periods:
            academic_year = row[0]
            semester      = row[1]
            year_start    = row[2]
            year_end      = row[3]
            sort_key      = year_start * 10 + semester
            sem_label     = semester_to_label(semester)
            full_label    = f"AY {academic_year} {sem_label}"

            with get_session() as session:
                stmt = pg_insert(GoldDimTime).values(
                    academic_year=academic_year,
                    semester=semester,
                    year_start=year_start,
                    year_end=year_end,
                    semester_label=sem_label,
                    full_label=full_label,
                    sort_key=sort_key,
                    is_current=False,
                )
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_gold_dim_time",
                    set_={
                        "full_label":     stmt.excluded.full_label,
                        "semester_label": stmt.excluded.semester_label,
                        "sort_key":       stmt.excluded.sort_key,
                    },
                )
                stmt = stmt.returning(GoldDimTime.time_id)
                time_id = session.execute(stmt).scalar_one()

            self._time_id_cache[(academic_year, semester)] = time_id
            count += 1

        logger.debug("dim_time synced — {} rows", count)
        return count

    # ------------------------------------------------------------------
    # Step 2 — dim_program
    # ------------------------------------------------------------------

    def _sync_dim_program(self) -> int:
        """Upsert one dim_program row per unique program_code in Silver."""
        with get_session() as session:
            programs = session.execute(
                text(
                    """
                    SELECT DISTINCT ON (program_code)
                        program_code, program_name, college, department, duration_years, is_active
                    FROM silver.programs
                    ORDER BY program_code, id DESC
                    """
                )
            ).fetchall()

        count = 0
        for row in programs:
            program_code   = row[0]
            program_name   = row[1]
            college        = row[2]
            department     = row[3]
            duration_years = row[4]
            is_active      = row[5]

            with get_session() as session:
                stmt = pg_insert(GoldDimProgram).values(
                    program_code=program_code,
                    program_name=program_name,
                    college=college,
                    department=department,
                    duration_years=duration_years,
                    is_active=is_active,
                )
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_gold_dim_program_program_code",
                    set_={
                        "program_name":   stmt.excluded.program_name,
                        "college":        stmt.excluded.college,
                        "is_active":      stmt.excluded.is_active,
                    },
                )
                stmt = stmt.returning(GoldDimProgram.program_id)
                program_id = session.execute(stmt).scalar_one()

            self._program_id_cache[program_code] = program_id
            count += 1

        logger.debug("dim_program synced — {} rows", count)
        return count

    # ------------------------------------------------------------------
    # Step 3 — fact_enrollment_metrics
    # ------------------------------------------------------------------

    def _build_fact_table(self) -> int:
        """
        Build fact_enrollment_metrics by joining Silver enrollment + outcomes.

        Join key: (academic_year, semester, program_code, major, year_level, gender)
        All KPI rates are computed here and stored pre-computed.
        """
        with get_session() as session:
            # Full outer join to capture rows that exist in one sheet but not the other
            rows = session.execute(
                text(
                    """
                    SELECT
                        COALESCE(ef.academic_year,  so.academic_year)  AS academic_year,
                        COALESCE(ef.semester,       so.semester)       AS semester,
                        COALESCE(ef.program_code,   so.program_code)   AS program_code,
                        COALESCE(ef.major,          so.major)          AS major,
                        COALESCE(ef.year_level,     so.year_level)     AS year_level,
                        COALESCE(ef.gender,         so.gender)         AS gender,

                        COALESCE(ef.applicants,          0) AS applicants,
                        COALESCE(ef.accepted_applicants, 0) AS accepted_applicants,
                        COALESCE(ef.total_enrolled,      0) AS total_enrolled,
                        COALESCE(ef.new_students,        0) AS new_students,
                        COALESCE(ef.transferees,         0) AS transferees,
                        COALESCE(ef.returnees,           0) AS returnees,

                        COALESCE(so.graduates,    0) AS graduates,
                        COALESCE(so.dropouts,     0) AS dropouts,
                        COALESCE(so.shifters_out, 0) AS shifters_out,
                        COALESCE(so.shifters_in,  0) AS shifters_in

                    FROM silver.enrollment_flow ef
                    FULL OUTER JOIN silver.student_outcomes so
                        ON  ef.academic_year = so.academic_year
                        AND ef.semester      = so.semester
                        AND ef.program_code  = so.program_code
                        AND COALESCE(ef.major, '')      = COALESCE(so.major, '')
                        AND ef.year_level    = so.year_level
                        AND ef.gender        = so.gender
                    ORDER BY academic_year, semester, program_code, year_level
                    """
                )
            ).fetchall()

        count = 0
        for row in rows:
            row = dict(row._mapping)

            time_id       = self._resolve_time_id(row["academic_year"], row["semester"])
            program_id    = self._resolve_program_id(row["program_code"])
            year_level_id = self._resolve_year_level_id(row["year_level"])

            if None in (time_id, program_id, year_level_id):
                logger.warning(
                    "Skipping fact row — unresolved dimension: "
                    "time_id={} program_id={} year_level_id={}",
                    time_id, program_id, year_level_id,
                )
                continue

            total_enrolled = row["total_enrolled"]
            dropouts       = row["dropouts"]
            graduates      = row["graduates"]
            applicants     = row["applicants"]
            accepted       = row["accepted_applicants"]
            shifters_in    = row["shifters_in"]
            shifters_out   = row["shifters_out"]

            # Pre-compute all KPI rates
            acceptance_rate     = KPIRules.acceptance_rate(accepted, applicants)
            dropout_rate        = KPIRules.dropout_rate(dropouts, total_enrolled)
            graduation_rate     = KPIRules.graduation_rate(graduates, total_enrolled)
            retention_rate      = KPIRules.retention_rate(total_enrolled, dropouts)
            net_shifter_balance = KPIRules.net_shifter_balance(shifters_in, shifters_out)

            with get_session() as session:
                stmt = pg_insert(GoldFactEnrollmentMetrics).values(
                    time_id=time_id,
                    program_id=program_id,
                    year_level_id=year_level_id,
                    gender=row["gender"] or "Not Specified",
                    applicants=applicants,
                    accepted_applicants=accepted,
                    total_enrolled=total_enrolled,
                    new_students=row["new_students"],
                    transferees=row["transferees"],
                    returnees=row["returnees"],
                    graduates=graduates,
                    dropouts=dropouts,
                    shifters_out=shifters_out,
                    shifters_in=shifters_in,
                    acceptance_rate=acceptance_rate,
                    dropout_rate=dropout_rate,
                    graduation_rate=graduation_rate,
                    retention_rate=retention_rate,
                    net_shifter_balance=net_shifter_balance,
                    refreshed_at=datetime.now(timezone.utc),
                )
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_gold_fact",
                    set_={
                        "applicants":           stmt.excluded.applicants,
                        "accepted_applicants":  stmt.excluded.accepted_applicants,
                        "total_enrolled":       stmt.excluded.total_enrolled,
                        "new_students":         stmt.excluded.new_students,
                        "transferees":          stmt.excluded.transferees,
                        "returnees":            stmt.excluded.returnees,
                        "graduates":            stmt.excluded.graduates,
                        "dropouts":             stmt.excluded.dropouts,
                        "shifters_out":         stmt.excluded.shifters_out,
                        "shifters_in":          stmt.excluded.shifters_in,
                        "acceptance_rate":      stmt.excluded.acceptance_rate,
                        "dropout_rate":         stmt.excluded.dropout_rate,
                        "graduation_rate":      stmt.excluded.graduation_rate,
                        "retention_rate":       stmt.excluded.retention_rate,
                        "net_shifter_balance":  stmt.excluded.net_shifter_balance,
                        "refreshed_at":         stmt.excluded.refreshed_at,
                    },
                )
                session.execute(stmt)

            count += 1

        logger.debug("fact_enrollment_metrics built — {} rows", count)
        return count

    # ------------------------------------------------------------------
    # Step 4 — agg_program_performance
    # ------------------------------------------------------------------

    def _build_agg_program(self) -> int:
        """
        Build agg_program_performance by rolling up fact rows per (program, time).
        Includes semester-over-semester trend deltas.
        """
        with get_session() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        program_id,
                        time_id,
                        SUM(applicants)             AS total_applicants,
                        SUM(accepted_applicants)    AS total_accepted,
                        SUM(total_enrolled)         AS total_enrolled,
                        SUM(graduates)              AS total_graduates,
                        SUM(dropouts)               AS total_dropouts,
                        SUM(shifters_out)           AS total_shifters_out,
                        SUM(shifters_in)            AS total_shifters_in,
                        AVG(acceptance_rate)        AS avg_acceptance_rate,
                        AVG(graduation_rate)        AS avg_graduation_rate,
                        AVG(dropout_rate)           AS avg_dropout_rate,
                        AVG(retention_rate)         AS avg_retention_rate
                    FROM gold.fact_enrollment_metrics
                    GROUP BY program_id, time_id
                    ORDER BY program_id, time_id
                    """
                )
            ).fetchall()

        # Build a lookup for previous semester totals (for trend deltas)
        prev_lookup: dict[tuple, dict] = {}
        agg_rows = [dict(r._mapping) for r in rows]

        for row in agg_rows:
            prev_lookup[(row["program_id"], row["time_id"])] = row

        count = 0
        for row in agg_rows:
            program_id     = row["program_id"]
            time_id        = row["time_id"]
            total_enrolled = row["total_enrolled"] or 0
            total_dropouts = row["total_dropouts"] or 0

            # Compute trend delta vs previous time_id
            prev_time_id = self._previous_time_id(time_id)
            prev_row     = prev_lookup.get((program_id, prev_time_id))

            enrollment_change_pct = None
            dropout_change_pct    = None

            if prev_row:
                enrollment_change_pct = KPIRules.enrollment_change_pct(
                    total_enrolled, prev_row["total_enrolled"] or 0
                )
                dropout_change_pct = KPIRules.enrollment_change_pct(
                    total_dropouts, prev_row["total_dropouts"] or 0
                )

            with get_session() as session:
                stmt = pg_insert(GoldAggProgramPerformance).values(
                    program_id=program_id,
                    time_id=time_id,
                    total_applicants=row["total_applicants"] or 0,
                    total_accepted=row["total_accepted"] or 0,
                    total_enrolled=total_enrolled,
                    total_graduates=row["total_graduates"] or 0,
                    total_dropouts=total_dropouts,
                    total_shifters_out=row["total_shifters_out"] or 0,
                    total_shifters_in=row["total_shifters_in"] or 0,
                    avg_acceptance_rate=row["avg_acceptance_rate"],
                    avg_graduation_rate=row["avg_graduation_rate"],
                    avg_dropout_rate=row["avg_dropout_rate"],
                    avg_retention_rate=row["avg_retention_rate"],
                    enrollment_change_pct=enrollment_change_pct,
                    dropout_change_pct=dropout_change_pct,
                    refreshed_at=datetime.now(timezone.utc),
                )
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_gold_agg_prog",
                    set_={
                        "total_enrolled":       stmt.excluded.total_enrolled,
                        "total_graduates":      stmt.excluded.total_graduates,
                        "total_dropouts":       stmt.excluded.total_dropouts,
                        "avg_dropout_rate":     stmt.excluded.avg_dropout_rate,
                        "avg_graduation_rate":  stmt.excluded.avg_graduation_rate,
                        "avg_retention_rate":   stmt.excluded.avg_retention_rate,
                        "enrollment_change_pct": stmt.excluded.enrollment_change_pct,
                        "dropout_change_pct":   stmt.excluded.dropout_change_pct,
                        "refreshed_at":         stmt.excluded.refreshed_at,
                    },
                )
                session.execute(stmt)

            count += 1

        logger.debug("agg_program_performance built — {} rows", count)
        return count

    # ------------------------------------------------------------------
    # Step 5 — agg_college_summary
    # ------------------------------------------------------------------

    def _build_agg_college(self) -> int:
        """Build college-level summary by rolling up agg_program rows."""
        with get_session() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        dp.college,
                        agg.time_id,
                        SUM(agg.total_enrolled)         AS total_enrolled,
                        SUM(agg.total_graduates)        AS total_graduates,
                        SUM(agg.total_dropouts)         AS total_dropouts,
                        COUNT(DISTINCT agg.program_id)  AS program_count,
                        AVG(agg.avg_dropout_rate)       AS avg_dropout_rate,
                        AVG(agg.avg_graduation_rate)    AS avg_graduation_rate
                    FROM gold.agg_program_performance agg
                    JOIN gold.dim_program dp ON dp.program_id = agg.program_id
                    GROUP BY dp.college, agg.time_id
                    ORDER BY dp.college, agg.time_id
                    """
                )
            ).fetchall()

        count = 0
        for row in rows:
            row = dict(row._mapping)
            with get_session() as session:
                stmt = pg_insert(GoldAggCollegeSummary).values(
                    college=row["college"],
                    time_id=row["time_id"],
                    total_enrolled=row["total_enrolled"] or 0,
                    total_graduates=row["total_graduates"] or 0,
                    total_dropouts=row["total_dropouts"] or 0,
                    program_count=row["program_count"] or 0,
                    avg_dropout_rate=row["avg_dropout_rate"],
                    avg_graduation_rate=row["avg_graduation_rate"],
                    refreshed_at=datetime.now(timezone.utc),
                )
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_gold_agg_college",
                    set_={
                        "total_enrolled":    stmt.excluded.total_enrolled,
                        "total_graduates":   stmt.excluded.total_graduates,
                        "total_dropouts":    stmt.excluded.total_dropouts,
                        "program_count":     stmt.excluded.program_count,
                        "avg_dropout_rate":  stmt.excluded.avg_dropout_rate,
                        "avg_graduation_rate": stmt.excluded.avg_graduation_rate,
                        "refreshed_at":      stmt.excluded.refreshed_at,
                    },
                )
                session.execute(stmt)
            count += 1

        logger.debug("agg_college_summary built — {} rows", count)
        return count

    # ------------------------------------------------------------------
    # Dimension resolution helpers
    # ------------------------------------------------------------------

    def _resolve_time_id(self, academic_year: str, semester: int) -> int | None:
        key = (academic_year, semester)
        if key in self._time_id_cache:
            return self._time_id_cache[key]
        with get_session() as session:
            row = session.execute(
                text(
                    "SELECT time_id FROM gold.dim_time "
                    "WHERE academic_year = :ay AND semester = :sem"
                ),
                {"ay": academic_year, "sem": semester},
            ).fetchone()
        if row:
            self._time_id_cache[key] = row[0]
            return row[0]
        return None

    def _resolve_program_id(self, program_code: str) -> int | None:
        if program_code in self._program_id_cache:
            return self._program_id_cache[program_code]
        with get_session() as session:
            row = session.execute(
                text(
                    "SELECT program_id FROM gold.dim_program "
                    "WHERE program_code = :code"
                ),
                {"code": program_code},
            ).fetchone()
        if row:
            self._program_id_cache[program_code] = row[0]
            return row[0]
        return None

    def _resolve_year_level_id(self, year_level: int) -> int | None:
        if year_level in self._year_level_cache:
            return self._year_level_cache[year_level]
        with get_session() as session:
            row = session.execute(
                text(
                    "SELECT year_level_id FROM gold.dim_year_level "
                    "WHERE year_level = :yl"
                ),
                {"yl": year_level},
            ).fetchone()
        if row:
            self._year_level_cache[year_level] = row[0]
            return row[0]
        return None

    def _previous_time_id(self, time_id: int) -> int | None:
        """Return the time_id of the immediately preceding semester."""
        with get_session() as session:
            row = session.execute(
                text(
                    """
                    SELECT time_id FROM gold.dim_time
                    WHERE sort_key < (
                        SELECT sort_key FROM gold.dim_time WHERE time_id = :tid
                    )
                    ORDER BY sort_key DESC
                    LIMIT 1
                    """
                ),
                {"tid": time_id},
            ).fetchone()
        return row[0] if row else None


# ==============================================================================
# Module-level runner — called by pipeline.py
# ==============================================================================

def run_gold() -> AggregationResult:
    """
    Entry point called by pipeline.py.

    Usage in pipeline.py:
        from transformation.gold_aggregate import run_gold
        result = run_gold()
    """
    aggregator = GoldAggregator()
    return aggregator.run()