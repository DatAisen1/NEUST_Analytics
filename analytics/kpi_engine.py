# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# analytics/kpi_engine.py
# Computes all institutional KPIs from the Gold layer and writes to a report
# ==============================================================================

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

from database.connection import get_session
from transformation.rules_engine import KPIRules, Thresholds
from utils.config import get_config
from utils.logger import log_step_failure, log_step_start, log_step_success, logger

_config = get_config()


# ==============================================================================
# KPI result containers
# ==============================================================================

@dataclass
class SemesterKPIs:
    """All KPIs for a single semester snapshot."""

    academic_year:              str
    semester:                   int
    semester_label:             str

    # Enrollment
    total_enrolled:             int   = 0
    total_new_students:         int   = 0
    total_transferees:          int   = 0
    total_returnees:            int   = 0
    total_applicants:           int   = 0
    total_accepted:             int   = 0

    # Outcomes
    total_graduates:            int   = 0
    total_dropouts:             int   = 0
    total_shifters_out:         int   = 0
    total_shifters_in:          int   = 0

    # Rates
    acceptance_rate:            float | None = None
    graduation_rate:            float | None = None
    dropout_rate:               float | None = None
    retention_rate:             float | None = None
    institutional_success_rate: float | None = None

    # Trend deltas vs previous semester
    enrollment_change_pct:      float | None = None
    dropout_change_pct:         float | None = None

    # Derived
    net_shifter_balance:        int   = 0
    super_senior_count:         int   = 0
    program_count:              int   = 0
    college_count:              int   = 0


@dataclass
class ProgramKPIs:
    """KPIs for a single program in a single semester."""

    program_code:       str
    program_name:       str
    college:            str
    academic_year:      str
    semester:           int

    total_enrolled:     int   = 0
    total_graduates:    int   = 0
    total_dropouts:     int   = 0
    total_shifters_out: int   = 0
    total_shifters_in:  int   = 0
    super_senior_count: int   = 0

    acceptance_rate:    float | None = None
    graduation_rate:    float | None = None
    dropout_rate:       float | None = None
    retention_rate:     float | None = None
    net_shifter_balance: int  = 0


@dataclass
class KPIReport:
    """Full KPI report output from a single engine run."""

    generated_at:           datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    current_semester:       SemesterKPIs | None = None
    all_semesters:          list[SemesterKPIs] = field(default_factory=list)
    program_kpis:           list[ProgramKPIs]  = field(default_factory=list)
    top_programs_enrolled:  list[dict]         = field(default_factory=list)
    top_programs_graduates: list[dict]         = field(default_factory=list)
    at_risk_programs:       list[dict]         = field(default_factory=list)
    super_senior_by_program: list[dict]        = field(default_factory=list)
    elapsed_seconds:        float = 0.0
    status:                 str   = "pending"
    error_message:          str | None = None


# ==============================================================================
# KPI Engine
# ==============================================================================

class KPIEngine:
    """
    Reads from the Gold layer and computes all institutional KPIs.

    All queries run against gold.fact_enrollment_metrics and the
    pre-aggregated gold.agg_program_performance table — never Silver.

    KPIs computed:
        Institution-wide:
            - Total enrollment, graduates, dropouts per semester
            - Acceptance rate, graduation rate, dropout rate, retention rate
            - Institutional success rate (composite)
            - Semester-over-semester enrollment and dropout change %
            - Net shifter balance
            - Super senior count
            - Program count, college count

        Per-program:
            - All above rates per program
            - Top 5 programs by enrollment
            - Top 5 programs by graduation rate
            - At-risk programs (dropout rate above threshold)
            - Super senior count per program

    Usage:
        engine = KPIEngine()
        report = engine.run()
        engine.print_summary(report)
    """

    def __init__(self) -> None:
        self._started = time.monotonic()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> KPIReport:
        """Compute all KPIs and return a KPIReport."""
        report = KPIReport()
        log_step_start(4, "KPI computation")

        try:
            # Load raw Gold data into DataFrames for efficient computation
            fact_df   = self._load_fact_table()
            prog_df   = self._load_program_dim()
            time_df   = self._load_time_dim()
            yl_df     = self._load_year_level_dim()

            if fact_df.empty:
                logger.warning(
                    "Gold fact table is empty — run the full pipeline first."
                )
                report.status = "success"
                return report

            # Enrich fact_df with dimension labels
            fact_df = self._enrich_fact(fact_df, prog_df, time_df, yl_df)

            # Compute semester-level KPIs
            report.all_semesters   = self._compute_semester_kpis(fact_df)
            report.current_semester = (
                report.all_semesters[-1] if report.all_semesters else None
            )

            # Compute program-level KPIs
            report.program_kpis = self._compute_program_kpis(fact_df)

            # Leaderboards
            report.top_programs_enrolled  = self._top_programs_by_enrolled(fact_df)
            report.top_programs_graduates = self._top_programs_by_graduates(fact_df)
            report.at_risk_programs       = self._at_risk_programs(fact_df)
            report.super_senior_by_program = self._super_senior_by_program(fact_df, yl_df)

            report.status = "success"

        except Exception as exc:
            report.status        = "failed"
            report.error_message = str(exc)
            logger.exception("KPI engine failed: {}", exc)
            log_step_failure(4, "KPI computation", exc)

        finally:
            report.elapsed_seconds = time.monotonic() - self._started

        if report.status == "success":
            log_step_success(4, "KPI computation")
            logger.info(
                "KPIs computed — {} semesters | {} programs | {:.2f}s",
                len(report.all_semesters),
                len(report.program_kpis),
                report.elapsed_seconds,
            )

        return report

    # ------------------------------------------------------------------
    # Data loaders
    # ------------------------------------------------------------------

    def _load_fact_table(self) -> pd.DataFrame:
        with get_session() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        f.metric_id, f.time_id, f.program_id, f.year_level_id,
                        f.gender,
                        f.applicants, f.accepted_applicants, f.total_enrolled,
                        f.new_students, f.transferees, f.returnees,
                        f.graduates, f.dropouts, f.shifters_out, f.shifters_in,
                        f.acceptance_rate, f.dropout_rate, f.graduation_rate,
                        f.retention_rate, f.net_shifter_balance
                    FROM gold.fact_enrollment_metrics f
                    ORDER BY f.time_id, f.program_id, f.year_level_id
                    """
                )
            ).fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([dict(r._mapping) for r in rows])

    def _load_program_dim(self) -> pd.DataFrame:
        with get_session() as session:
            rows = session.execute(
                text(
                    "SELECT program_id, program_code, program_name, college "
                    "FROM gold.dim_program"
                )
            ).fetchall()
        return pd.DataFrame([dict(r._mapping) for r in rows])

    def _load_time_dim(self) -> pd.DataFrame:
        with get_session() as session:
            rows = session.execute(
                text(
                    "SELECT time_id, academic_year, semester, full_label, sort_key "
                    "FROM gold.dim_time ORDER BY sort_key"
                )
            ).fetchall()
        return pd.DataFrame([dict(r._mapping) for r in rows])

    def _load_year_level_dim(self) -> pd.DataFrame:
        with get_session() as session:
            rows = session.execute(
                text(
                    "SELECT year_level_id, year_level, level_name, is_irregular "
                    "FROM gold.dim_year_level ORDER BY year_level"
                )
            ).fetchall()
        return pd.DataFrame([dict(r._mapping) for r in rows])

    # ------------------------------------------------------------------
    # Enrichment
    # ------------------------------------------------------------------

    def _enrich_fact(
        self,
        fact_df: pd.DataFrame,
        prog_df: pd.DataFrame,
        time_df: pd.DataFrame,
        yl_df:   pd.DataFrame,
    ) -> pd.DataFrame:
        """Join dimension labels onto the fact table."""
        df = fact_df.copy()
        df = df.merge(prog_df, on="program_id", how="left")
        df = df.merge(time_df, on="time_id",    how="left")
        df = df.merge(yl_df,   on="year_level_id", how="left")
        return df

    # ------------------------------------------------------------------
    # Semester-level KPI computation
    # ------------------------------------------------------------------

    def _compute_semester_kpis(self, df: pd.DataFrame) -> list[SemesterKPIs]:
        """Aggregate fact rows by semester and compute institution-wide KPIs."""
        results: list[SemesterKPIs] = []

        # Sort semesters chronologically
        time_groups = df.groupby(["academic_year", "semester", "full_label", "sort_key"])
        time_groups = sorted(time_groups, key=lambda x: x[0][3])   # sort by sort_key

        prev_enrolled = None
        prev_dropouts = None

        for (academic_year, semester, full_label, sort_key), group in time_groups:
            total_enrolled   = int(group["total_enrolled"].sum())
            total_graduates  = int(group["graduates"].sum())
            total_dropouts   = int(group["dropouts"].sum())
            total_applicants = int(group["applicants"].sum())
            total_accepted   = int(group["accepted_applicants"].sum())
            total_shifters_in  = int(group["shifters_in"].sum())
            total_shifters_out = int(group["shifters_out"].sum())
            total_new        = int(group["new_students"].sum())
            total_transferees = int(group["transferees"].sum())
            total_returnees  = int(group["returnees"].sum())

            # Super seniors = year_level >= 5
            super_senior_count = int(
                group[group["year_level"] >= Thresholds.SUPER_SENIOR_YEAR_LEVEL][
                    "total_enrolled"
                ].sum()
            )

            # KPI rates
            acceptance_rate  = KPIRules.acceptance_rate(total_accepted, total_applicants)
            dropout_rate     = KPIRules.dropout_rate(total_dropouts, total_enrolled)
            graduation_rate  = KPIRules.graduation_rate(total_graduates, total_enrolled)
            retention_rate   = KPIRules.retention_rate(total_enrolled, total_dropouts)
            success_rate     = KPIRules.institutional_success_rate(
                graduation_rate, retention_rate, dropout_rate
            )

            # Trend deltas
            enrollment_change_pct = (
                KPIRules.enrollment_change_pct(total_enrolled, prev_enrolled)
                if prev_enrolled is not None else None
            )
            dropout_change_pct = (
                KPIRules.enrollment_change_pct(total_dropouts, prev_dropouts)
                if prev_dropouts is not None else None
            )

            kpi = SemesterKPIs(
                academic_year=academic_year,
                semester=semester,
                semester_label=full_label,
                total_enrolled=total_enrolled,
                total_new_students=total_new,
                total_transferees=total_transferees,
                total_returnees=total_returnees,
                total_applicants=total_applicants,
                total_accepted=total_accepted,
                total_graduates=total_graduates,
                total_dropouts=total_dropouts,
                total_shifters_out=total_shifters_out,
                total_shifters_in=total_shifters_in,
                acceptance_rate=acceptance_rate,
                graduation_rate=graduation_rate,
                dropout_rate=dropout_rate,
                retention_rate=retention_rate,
                institutional_success_rate=success_rate,
                enrollment_change_pct=enrollment_change_pct,
                dropout_change_pct=dropout_change_pct,
                net_shifter_balance=KPIRules.net_shifter_balance(
                    total_shifters_in, total_shifters_out
                ),
                super_senior_count=super_senior_count,
                program_count=int(group["program_id"].nunique()),
                college_count=int(group["college"].nunique()),
            )

            results.append(kpi)
            prev_enrolled = total_enrolled
            prev_dropouts = total_dropouts

        return results

    # ------------------------------------------------------------------
    # Program-level KPI computation
    # ------------------------------------------------------------------

    def _compute_program_kpis(self, df: pd.DataFrame) -> list[ProgramKPIs]:
        """Compute KPIs per program per semester."""
        results: list[ProgramKPIs] = []

        group_cols = [
            "academic_year", "semester", "program_code", "program_name", "college"
        ]
        for keys, group in df.groupby(group_cols):
            academic_year, semester, program_code, program_name, college = keys

            total_enrolled   = int(group["total_enrolled"].sum())
            total_graduates  = int(group["graduates"].sum())
            total_dropouts   = int(group["dropouts"].sum())
            total_shifters_out = int(group["shifters_out"].sum())
            total_shifters_in  = int(group["shifters_in"].sum())

            super_senior_count = int(
                group[group["year_level"] >= Thresholds.SUPER_SENIOR_YEAR_LEVEL][
                    "total_enrolled"
                ].sum()
            )

            results.append(
                ProgramKPIs(
                    program_code=program_code,
                    program_name=program_name,
                    college=college,
                    academic_year=academic_year,
                    semester=semester,
                    total_enrolled=total_enrolled,
                    total_graduates=total_graduates,
                    total_dropouts=total_dropouts,
                    total_shifters_out=total_shifters_out,
                    total_shifters_in=total_shifters_in,
                    super_senior_count=super_senior_count,
                    acceptance_rate=KPIRules.acceptance_rate(
                        int(group["accepted_applicants"].sum()),
                        int(group["applicants"].sum()),
                    ),
                    graduation_rate=KPIRules.graduation_rate(total_graduates, total_enrolled),
                    dropout_rate=KPIRules.dropout_rate(total_dropouts, total_enrolled),
                    retention_rate=KPIRules.retention_rate(total_enrolled, total_dropouts),
                    net_shifter_balance=KPIRules.net_shifter_balance(
                        total_shifters_in, total_shifters_out
                    ),
                )
            )

        return results

    # ------------------------------------------------------------------
    # Leaderboards
    # ------------------------------------------------------------------

    def _top_programs_by_enrolled(self, df: pd.DataFrame, n: int = 5) -> list[dict]:
        """Top N programs by total enrollment (most recent semester)."""
        latest_time_id = df["time_id"].max()
        latest = df[df["time_id"] == latest_time_id]
        grouped = (
            latest.groupby(["program_code", "program_name", "college"])["total_enrolled"]
            .sum()
            .reset_index()
            .sort_values("total_enrolled", ascending=False)
            .head(n)
        )
        return grouped.to_dict("records")

    def _top_programs_by_graduates(self, df: pd.DataFrame, n: int = 5) -> list[dict]:
        """Top N programs by graduation rate (most recent semester)."""
        latest_time_id = df["time_id"].max()
        latest = df[df["time_id"] == latest_time_id]
        grouped = (
            latest.groupby(["program_code", "program_name", "college"])
            .agg(graduates=("graduates", "sum"), enrolled=("total_enrolled", "sum"))
            .reset_index()
        )
        grouped["graduation_rate"] = grouped.apply(
            lambda r: KPIRules.graduation_rate(int(r["graduates"]), int(r["enrolled"])),
            axis=1,
        )
        return (
            grouped.sort_values("graduation_rate", ascending=False)
            .head(n)
            .to_dict("records")
        )

    def _at_risk_programs(
        self, df: pd.DataFrame, dropout_threshold: float = 20.0
    ) -> list[dict]:
        """Programs where dropout_rate exceeds the threshold in the latest semester."""
        latest_time_id = df["time_id"].max()
        latest = df[df["time_id"] == latest_time_id]
        grouped = (
            latest.groupby(["program_code", "program_name", "college"])
            .agg(dropouts=("dropouts", "sum"), enrolled=("total_enrolled", "sum"))
            .reset_index()
        )
        grouped["dropout_rate"] = grouped.apply(
            lambda r: KPIRules.dropout_rate(int(r["dropouts"]), int(r["enrolled"])),
            axis=1,
        )
        at_risk = grouped[grouped["dropout_rate"] >= dropout_threshold]
        return (
            at_risk.sort_values("dropout_rate", ascending=False)
            .to_dict("records")
        )

    def _super_senior_by_program(
        self, df: pd.DataFrame, yl_df: pd.DataFrame
    ) -> list[dict]:
        """Count of super seniors (year_level >= 5) per program."""
        ss = df[df["year_level"] >= Thresholds.SUPER_SENIOR_YEAR_LEVEL]
        if ss.empty:
            return []
        grouped = (
            ss.groupby(["program_code", "program_name", "college"])["total_enrolled"]
            .sum()
            .reset_index()
            .rename(columns={"total_enrolled": "super_senior_count"})
            .sort_values("super_senior_count", ascending=False)
        )
        return grouped.to_dict("records")

    # ------------------------------------------------------------------
    # Console summary printer
    # ------------------------------------------------------------------

    def print_summary(self, report: KPIReport) -> None:
        """Print a formatted KPI summary to the console."""
        if not report.current_semester:
            logger.info("No KPI data available.")
            return

        c = report.current_semester
        sep = "=" * 65

        print(sep)
        print(f"  NEUST Analytics — KPI Summary")
        print(f"  Period : {c.semester_label}")
        print(f"  Generated : {report.generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(sep)
        print(f"  Total Enrolled       : {c.total_enrolled:,}")
        print(f"  New Students         : {c.total_new_students:,}")
        print(f"  Transferees          : {c.total_transferees:,}")
        print(f"  Returnees            : {c.total_returnees:,}")
        print(f"  Super Seniors        : {c.super_senior_count:,}")
        print(sep)
        print(f"  Acceptance Rate      : {_fmt(c.acceptance_rate)}%")
        print(f"  Graduation Rate      : {_fmt(c.graduation_rate)}%")
        print(f"  Dropout Rate         : {_fmt(c.dropout_rate)}%")
        print(f"  Retention Rate       : {_fmt(c.retention_rate)}%")
        print(f"  Institutional Success: {_fmt(c.institutional_success_rate)}%")
        print(sep)
        print(f"  Enrollment Change    : {_fmt(c.enrollment_change_pct)}%")
        print(f"  Dropout Change       : {_fmt(c.dropout_change_pct)}%")
        print(f"  Net Shifter Balance  : {c.net_shifter_balance:+,}")
        print(sep)

        if report.top_programs_enrolled:
            print("  Top Programs by Enrollment:")
            for i, p in enumerate(report.top_programs_enrolled, 1):
                print(f"    {i}. {p['program_code']:10s} — {p['total_enrolled']:,} students")

        if report.at_risk_programs:
            print(f"\n  ⚠  At-Risk Programs (dropout rate ≥ 20%):")
            for p in report.at_risk_programs:
                print(
                    f"    {p['program_code']:10s} — "
                    f"{p['dropout_rate']:.1f}% dropout | {p['enrolled']:,} enrolled"
                )

        print(sep)


def _fmt(value: float | None) -> str:
    """Format a rate value for display."""
    return f"{value:.2f}" if value is not None else "N/A"


# ==============================================================================
# Module-level runner — called by pipeline.py
# ==============================================================================

def run_kpi_engine() -> KPIReport:
    """
    Entry point called by pipeline.py.

    Usage:
        from analytics.kpi_engine import run_kpi_engine
        report = run_kpi_engine()
    """
    engine = KPIEngine()
    report = engine.run()
    engine.print_summary(report)
    return report