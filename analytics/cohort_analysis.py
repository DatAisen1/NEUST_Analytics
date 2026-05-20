# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# analytics/cohort_analysis.py
# Cohort survival analysis — tracks how many students from a starting cohort remain
# ==============================================================================

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from database.connection import get_session
from transformation.rules_engine import RiskRules, Thresholds
from utils.config import get_config
from utils.logger import log_step_failure, log_step_start, log_step_success, logger

_config = get_config()


# ==============================================================================
# Result containers
# ==============================================================================

@dataclass
class CohortSemester:
    """Survival data for one semester within a cohort's lifecycle."""
    semester_number:    int       # 1 = first semester after entry
    sort_key:           int
    academic_year:      str
    semester:           int
    enrolled:           int
    dropouts:           int
    graduates:          int
    survival_rate:      float     # enrolled / initial_enrolled * 100
    cumulative_dropout: int       # total dropouts since cohort start


@dataclass
class ProgramCohort:
    """
    A single cohort tracked across all subsequent semesters.

    A cohort is defined as all students enrolled in a specific program
    in their 1st year during a specific academic year.
    """
    program_code:       str
    program_name:       str
    college:            str
    cohort_year:        str       # academic_year of entry (e.g. '2020-2021')
    initial_enrolled:   int
    semesters:          list[CohortSemester] = field(default_factory=list)

    @property
    def final_survival_rate(self) -> float | None:
        if not self.semesters:
            return None
        return self.semesters[-1].survival_rate

    @property
    def is_at_risk(self) -> bool:
        return RiskRules.flag_at_risk_cohort(self.final_survival_rate)


@dataclass
class CohortReport:
    """Full cohort survival analysis output."""
    generated_at:           str = ""
    cohorts:                list[ProgramCohort] = field(default_factory=list)
    institution_survival:   list[dict] = field(default_factory=list)
    at_risk_cohorts:        list[ProgramCohort] = field(default_factory=list)
    avg_survival_sem4:      float | None = None   # Average survival rate at semester 4
    avg_survival_sem8:      float | None = None   # Average survival rate at semester 8
    elapsed_seconds:        float = 0.0
    status:                 str = "pending"
    error_message:          str | None = None


# ==============================================================================
# Cohort analysis engine
# ==============================================================================

class CohortAnalyzer:
    """
    Tracks cohort survival across semesters.

    Cohort definition:
        A cohort = all students enrolled at Year Level 1 in a given program
        in a specific academic year. The cohort is tracked forward through
        all subsequent semesters until no enrollment data remains.

    Survival rate:
        survival_rate(t) = enrolled(t) / initial_enrolled * 100

    At-risk flag:
        Cohorts with survival_rate < 50% (Thresholds.COHORT_RETENTION_THRESHOLD)
        are flagged for intervention.

    Note: Because the NEUST dataset uses aggregated counts (not individual
    student IDs), cohort tracking is approximate — it follows enrollment
    counts at Year Level 1+n over time rather than tracking named students.

    Usage:
        analyzer = CohortAnalyzer()
        report = analyzer.run()
    """

    def __init__(self) -> None:
        self._started = time.monotonic()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> CohortReport:
        """Run full cohort survival analysis."""
        report = CohortReport(generated_at=pd.Timestamp.now().isoformat())
        log_step_start(7, "Cohort survival analysis")

        try:
            df = self._load_enrollment_data()

            if df.empty:
                logger.warning("No enrollment data found for cohort analysis.")
                report.status = "success"
                return report

            logger.info(
                "Enrollment data loaded — {} rows | {} programs | {} academic years",
                len(df), df["program_code"].nunique(), df["academic_year"].nunique(),
            )

            # Build per-program cohorts
            report.cohorts = self._build_cohorts(df)

            # Flag at-risk cohorts
            report.at_risk_cohorts = [c for c in report.cohorts if c.is_at_risk]

            # Institution-wide survival curve
            report.institution_survival = self._institution_survival(report.cohorts)

            # Summary stats
            report.avg_survival_sem4 = self._avg_survival_at_semester(report.cohorts, 4)
            report.avg_survival_sem8 = self._avg_survival_at_semester(report.cohorts, 8)

            report.status = "success"

        except Exception as exc:
            report.status        = "failed"
            report.error_message = str(exc)
            logger.exception("Cohort analysis failed: {}", exc)
            log_step_failure(7, "Cohort survival analysis", exc)

        finally:
            report.elapsed_seconds = time.monotonic() - self._started

        if report.status == "success":
            log_step_success(7, "Cohort survival analysis")
            logger.info(
                "Cohort analysis complete — {} cohorts | {} at-risk | {:.2f}s",
                len(report.cohorts),
                len(report.at_risk_cohorts),
                report.elapsed_seconds,
            )

        return report

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_enrollment_data(self) -> pd.DataFrame:
        """Load enrollment and outcome data grouped by year level per semester."""
        with get_session() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        dt.academic_year,
                        dt.semester,
                        dt.sort_key,
                        dp.program_code,
                        dp.program_name,
                        dp.college,
                        dyl.year_level,
                        SUM(f.total_enrolled)  AS total_enrolled,
                        SUM(f.dropouts)        AS dropouts,
                        SUM(f.graduates)       AS graduates,
                        SUM(f.new_students)    AS new_students
                    FROM gold.fact_enrollment_metrics f
                    JOIN gold.dim_time       dt  ON dt.time_id       = f.time_id
                    JOIN gold.dim_program    dp  ON dp.program_id    = f.program_id
                    JOIN gold.dim_year_level dyl ON dyl.year_level_id = f.year_level_id
                    GROUP BY
                        dt.academic_year, dt.semester, dt.sort_key,
                        dp.program_code, dp.program_name, dp.college,
                        dyl.year_level
                    ORDER BY dp.program_code, dt.sort_key, dyl.year_level
                    """
                )
            ).fetchall()

        if not rows:
            return pd.DataFrame()

        return pd.DataFrame([dict(r._mapping) for r in rows])

    # ------------------------------------------------------------------
    # Cohort building
    # ------------------------------------------------------------------

    def _build_cohorts(self, df: pd.DataFrame) -> list[ProgramCohort]:
        """Build cohort objects for each program × entry year combination."""
        cohorts: list[ProgramCohort] = []

        for program_code, prog_df in df.groupby("program_code"):
            prog_df = prog_df.sort_values("sort_key")
            prog_meta = prog_df.iloc[0]

            # Entry points: semesters where Year Level 1 is observed
            yr1_df = prog_df[prog_df["year_level"] == 1].copy()
            if yr1_df.empty:
                continue

            for _, entry_row in yr1_df.iterrows():
                initial_enrolled = int(entry_row["total_enrolled"])
                if initial_enrolled == 0:
                    continue

                cohort_year = str(entry_row["academic_year"])
                entry_sort  = int(entry_row["sort_key"])

                # Track this cohort across subsequent semesters
                # Each successive semester, the cohort is 1 year level higher
                sem_records: list[CohortSemester] = []
                cumulative_dropout = 0

                for sem_offset in range(Thresholds.COHORT_SURVIVAL_SEMESTERS):
                    expected_sort     = entry_sort + sem_offset
                    expected_yr_level = (sem_offset // 2) + 1   # 2 sems per year level

                    match = prog_df[
                        (prog_df["sort_key"] == expected_sort) &
                        (prog_df["year_level"] == min(expected_yr_level, 6))
                    ]

                    if match.empty:
                        # No data for this semester — stop tracking
                        break

                    row = match.iloc[0]
                    enrolled  = int(row["total_enrolled"])
                    dropouts  = int(row["dropouts"])
                    graduates = int(row["graduates"])

                    cumulative_dropout += dropouts
                    survival_rate = (
                        round((enrolled / initial_enrolled) * 100, 2)
                        if initial_enrolled > 0 else 0.0
                    )

                    sem_records.append(
                        CohortSemester(
                            semester_number=sem_offset + 1,
                            sort_key=int(row["sort_key"]),
                            academic_year=str(row["academic_year"]),
                            semester=int(row["semester"]),
                            enrolled=enrolled,
                            dropouts=dropouts,
                            graduates=graduates,
                            survival_rate=survival_rate,
                            cumulative_dropout=cumulative_dropout,
                        )
                    )

                    # Stop if survival drops to zero
                    if enrolled == 0:
                        break

                if sem_records:
                    cohorts.append(
                        ProgramCohort(
                            program_code=str(program_code),
                            program_name=str(prog_meta["program_name"]),
                            college=str(prog_meta["college"]),
                            cohort_year=cohort_year,
                            initial_enrolled=initial_enrolled,
                            semesters=sem_records,
                        )
                    )

        logger.debug("Built {} program cohorts", len(cohorts))
        return cohorts

    # ------------------------------------------------------------------
    # Institution-wide survival
    # ------------------------------------------------------------------

    def _institution_survival(self, cohorts: list[ProgramCohort]) -> list[dict]:
        """
        Compute an average institution-wide survival curve across all cohorts.

        Returns a list of {semester_number, avg_survival_rate, cohort_count} dicts.
        """
        max_sems = Thresholds.COHORT_SURVIVAL_SEMESTERS
        rows = []

        for sem_num in range(1, max_sems + 1):
            rates = []
            for cohort in cohorts:
                sem = next(
                    (s for s in cohort.semesters if s.semester_number == sem_num),
                    None,
                )
                if sem is not None:
                    rates.append(sem.survival_rate)

            if rates:
                rows.append({
                    "semester_number":    sem_num,
                    "avg_survival_rate":  round(sum(rates) / len(rates), 2),
                    "cohort_count":       len(rates),
                })

        return rows

    # ------------------------------------------------------------------
    # Summary stats
    # ------------------------------------------------------------------

    def _avg_survival_at_semester(
        self, cohorts: list[ProgramCohort], target_sem: int
    ) -> float | None:
        """Average survival rate across all cohorts at a given semester number."""
        rates = []
        for cohort in cohorts:
            sem = next(
                (s for s in cohort.semesters if s.semester_number == target_sem),
                None,
            )
            if sem is not None:
                rates.append(sem.survival_rate)

        if not rates:
            return None
        return round(sum(rates) / len(rates), 2)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_csv(self, report: CohortReport, output_path: Path | None = None) -> Path:
        """Export the full cohort survival data to CSV."""
        output_path = output_path or (
            _config.exports_path
            / f"cohort_survival_{pd.Timestamp.now().strftime('%Y%m%d')}.csv"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        rows = []
        for cohort in report.cohorts:
            for sem in cohort.semesters:
                rows.append({
                    "program_code":       cohort.program_code,
                    "program_name":       cohort.program_name,
                    "college":            cohort.college,
                    "cohort_year":        cohort.cohort_year,
                    "initial_enrolled":   cohort.initial_enrolled,
                    "semester_number":    sem.semester_number,
                    "academic_year":      sem.academic_year,
                    "semester":           sem.semester,
                    "enrolled":           sem.enrolled,
                    "dropouts":           sem.dropouts,
                    "graduates":          sem.graduates,
                    "survival_rate":      sem.survival_rate,
                    "cumulative_dropout": sem.cumulative_dropout,
                    "is_at_risk":         cohort.is_at_risk,
                })

        if rows:
            pd.DataFrame(rows).to_csv(output_path, index=False)
            logger.info("Cohort survival CSV exported → {}", output_path)

        return output_path

    # ------------------------------------------------------------------
    # Console summary
    # ------------------------------------------------------------------

    def print_summary(self, report: CohortReport) -> None:
        sep = "=" * 65
        print(sep)
        print("  NEUST Analytics — Cohort Survival Summary")
        print(sep)
        print(f"  Total Cohorts Tracked : {len(report.cohorts)}")
        print(f"  At-Risk Cohorts       : {len(report.at_risk_cohorts)}")
        s4 = f"{report.avg_survival_sem4:.1f}%" if report.avg_survival_sem4 else "N/A"
        s8 = f"{report.avg_survival_sem8:.1f}%" if report.avg_survival_sem8 else "N/A"
        print(f"  Avg Survival @ Sem 4  : {s4}")
        print(f"  Avg Survival @ Sem 8  : {s8}")

        if report.institution_survival:
            print("\n  Institution Survival Curve:")
            for row in report.institution_survival:
                bar = "█" * int(row["avg_survival_rate"] / 5)
                print(
                    f"    Sem {row['semester_number']:2d} | "
                    f"{row['avg_survival_rate']:5.1f}%  {bar}"
                )

        if report.at_risk_cohorts:
            print(f"\n  ⚠  At-Risk Cohorts (survival < {Thresholds.COHORT_RETENTION_THRESHOLD}%):")
            for c in report.at_risk_cohorts[:5]:
                rate = f"{c.final_survival_rate:.1f}%" if c.final_survival_rate else "N/A"
                print(
                    f"    {c.program_code:10s} AY {c.cohort_year} | "
                    f"Initial={c.initial_enrolled} | Survival={rate}"
                )
        print(sep)


# ==============================================================================
# Module-level runner — called by pipeline.py
# ==============================================================================

def run_cohort_analysis() -> CohortReport:
    """
    Entry point called by pipeline.py.

    Usage:
        from analytics.cohort_analysis import run_cohort_analysis
        report = run_cohort_analysis()
    """
    analyzer = CohortAnalyzer()
    report   = analyzer.run()
    analyzer.print_summary(report)
    analyzer.export_csv(report)
    return report