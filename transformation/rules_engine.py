# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# transformation/rules_engine.py
# All institutional business rules applied during Bronze → Silver transformation
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from utils.logger import logger


# ==============================================================================
# Enumerations — canonical status values used across the entire system
# ==============================================================================

class StudentStatus(str, Enum):
    """
    Enrollment status classification per semester.

    ACTIVE      — currently enrolled, no special flag
    GRADUATE    — completed the program this semester
    DROPOUT     — discontinued enrollment (no re-enrollment in next semester)
    SHIFTER_IN  — transferred INTO this program from another
    SHIFTER_OUT — transferred OUT of this program to another
    RETURNEE    — re-enrolled after a gap semester
    SUPER_SENIOR — enrolled beyond the standard program duration
    IRREGULAR   — year level does not match expected progression
    """
    ACTIVE       = "Active"
    GRADUATE     = "Graduate"
    DROPOUT      = "Dropout"
    SHIFTER_IN   = "Shifter In"
    SHIFTER_OUT  = "Shifter Out"
    RETURNEE     = "Returnee"
    SUPER_SENIOR = "Super Senior"
    IRREGULAR    = "Irregular"


class RiskLevel(str, Enum):
    """At-risk classification for dropout prediction."""
    LOW      = "Low"
    MODERATE = "Moderate"
    HIGH     = "High"
    CRITICAL = "Critical"


# ==============================================================================
# Thresholds — single place to change all business rule cutoffs
# ==============================================================================

class Thresholds:
    """
    All numeric thresholds used in business rules.
    Modify here only — never hardcode in transformation scripts.
    """

    # Year level
    STANDARD_PROGRAM_YEARS      = 4     # default duration for most NEUST programs
    MAX_PROGRAM_YEARS           = 6     # beyond this = super senior (irregular)
    SUPER_SENIOR_YEAR_LEVEL     = 5     # year_level >= this = super senior flag
    YEAR_LEVEL_GAP_WARNING      = 1     # gap >= 1 = irregular flag
    YEAR_LEVEL_GAP_HIGH_RISK    = 2     # gap >= 2 = high dropout risk

    # Dropout risk scoring (weights sum to 1.0)
    DROPOUT_RATE_WEIGHT         = 0.40
    YEAR_LEVEL_GAP_WEIGHT       = 0.35
    SHIFTER_HISTORY_WEIGHT      = 0.25

    # Risk thresholds (composite score 0.0–1.0)
    RISK_LOW_MAX                = 0.25
    RISK_MODERATE_MAX           = 0.50
    RISK_HIGH_MAX               = 0.75
    # Above 0.75 = CRITICAL

    # Enrollment data quality
    MIN_ENROLLED_FOR_RATES      = 1     # avoid division by zero
    MAX_ACCEPTANCE_RATE         = 100.0 # flag if > 100 (data error)
    MAX_DROPOUT_RATE            = 100.0 # flag if > 100 (data error)

    # Cohort analysis
    COHORT_RETENTION_THRESHOLD  = 50.0  # below this % = at-risk cohort
    COHORT_SURVIVAL_SEMESTERS   = 8     # track cohorts for 8 semesters (4 years)


# ==============================================================================
# KPI computation rules
# ==============================================================================

class KPIRules:
    """
    Stateless functions that compute all institutional KPIs.

    All functions return None when inputs are insufficient to avoid
    producing misleading zero or NaN values in the dashboard.
    """

    @staticmethod
    def acceptance_rate(accepted: int, applicants: int) -> float | None:
        """
        Acceptance Rate = (Accepted Applicants / Applicants) × 100

        Scope: per program, per semester.
        """
        if applicants < Thresholds.MIN_ENROLLED_FOR_RATES:
            return None
        rate = (accepted / applicants) * 100
        if rate > Thresholds.MAX_ACCEPTANCE_RATE:
            logger.warning(
                "Acceptance rate {:.1f}% exceeds 100% — data quality issue "
                "(accepted={}, applicants={})",
                rate, accepted, applicants,
            )
        return round(rate, 2)

    @staticmethod
    def dropout_rate(dropouts: int, total_enrolled: int) -> float | None:
        """
        Dropout Rate = (Dropouts / Total Enrolled) × 100

        Scope: per program, per year level, per semester.
        """
        if total_enrolled < Thresholds.MIN_ENROLLED_FOR_RATES:
            return None
        rate = (dropouts / total_enrolled) * 100
        return round(min(rate, 100.0), 2)

    @staticmethod
    def graduation_rate(graduates: int, total_enrolled: int) -> float | None:
        """
        Graduation Rate = (Graduates / Total Enrolled) × 100

        Scope: per program, per cohort year.
        Note: This is a simplified rate. True cohort graduation rate
        requires tracking a specific entry cohort to completion.
        """
        if total_enrolled < Thresholds.MIN_ENROLLED_FOR_RATES:
            return None
        rate = (graduates / total_enrolled) * 100
        return round(min(rate, 100.0), 2)

    @staticmethod
    def retention_rate(total_enrolled: int, dropouts: int) -> float | None:
        """
        Retention Rate = ((Total Enrolled − Dropouts) / Total Enrolled) × 100

        Scope: per program, per semester.
        """
        if total_enrolled < Thresholds.MIN_ENROLLED_FOR_RATES:
            return None
        retained = max(total_enrolled - dropouts, 0)
        rate = (retained / total_enrolled) * 100
        return round(rate, 2)

    @staticmethod
    def net_shifter_balance(shifters_in: int, shifters_out: int) -> int:
        """
        Net Shifter Balance = Shifters In − Shifters Out

        Positive = more students entering the program than leaving.
        Negative = program is losing students to other programs.
        """
        return shifters_in - shifters_out

    @staticmethod
    def institutional_success_rate(
        graduation_rate: float | None,
        retention_rate: float | None,
        dropout_rate: float | None,
    ) -> float | None:
        """
        Institutional Success Rate = (Graduation Rate + Retention Rate − Dropout Rate) / 2

        Composite KPI as defined in the NEUST development plan.
        Returns None if any component is unavailable.
        """
        if any(v is None for v in [graduation_rate, retention_rate, dropout_rate]):
            return None
        score = (graduation_rate + retention_rate - dropout_rate) / 2
        return round(max(score, 0.0), 2)

    @staticmethod
    def enrollment_change_pct(
        current_enrolled: int,
        previous_enrolled: int,
    ) -> float | None:
        """
        Semester-over-semester enrollment change.

        Positive = growth, Negative = decline.
        Returns None if previous semester had no enrollment.
        """
        if previous_enrolled < Thresholds.MIN_ENROLLED_FOR_RATES:
            return None
        change = ((current_enrolled - previous_enrolled) / previous_enrolled) * 100
        return round(change, 2)


# ==============================================================================
# Student status classification rules
# ==============================================================================

class StatusRules:
    """
    Rules for classifying student/group records into status categories.

    These rules operate on aggregated row-level data (per program/year_level/gender
    per semester) rather than individual student records, since the NEUST dataset
    does not include individual student IDs.
    """

    @staticmethod
    def classify_row(
        total_enrolled: int,
        graduates:      int,
        dropouts:       int,
        shifters_out:   int,
        shifters_in:    int,
        returnees:      int,
        year_level:     int,
    ) -> list[StudentStatus]:
        """
        Return a list of applicable status flags for an aggregated row.

        A single row can have multiple statuses (e.g., a program group
        can have both GRADUATE and DROPOUT records in the same semester).
        """
        statuses: list[StudentStatus] = []

        if graduates > 0:
            statuses.append(StudentStatus.GRADUATE)

        if dropouts > 0:
            statuses.append(StudentStatus.DROPOUT)

        if shifters_out > 0:
            statuses.append(StudentStatus.SHIFTER_OUT)

        if shifters_in > 0:
            statuses.append(StudentStatus.SHIFTER_IN)

        if returnees > 0:
            statuses.append(StudentStatus.RETURNEE)

        if year_level >= Thresholds.SUPER_SENIOR_YEAR_LEVEL:
            statuses.append(StudentStatus.SUPER_SENIOR)

        # Default active if no other status applies and enrollment exists
        if not statuses and total_enrolled > 0:
            statuses.append(StudentStatus.ACTIVE)

        return statuses

    @staticmethod
    def is_super_senior(year_level: int, program_duration: int = 4) -> bool:
        """
        A student group is flagged Super Senior if their year level
        exceeds the standard program duration.
        """
        return year_level > program_duration

    @staticmethod
    def is_irregular(year_level: int, years_since_start: int) -> bool:
        """
        A student is irregular if their actual year level does not match
        the expected year level based on years since enrollment start.

        year_level_gap = year_level − years_since_start
        Positive gap = behind expected level (delayed progression).
        """
        gap = year_level - years_since_start
        return abs(gap) >= Thresholds.YEAR_LEVEL_GAP_WARNING


# ==============================================================================
# Year level progression rules
# ==============================================================================

class ProgressionRules:
    """
    Rules governing year level advancement and super senior detection.
    """

    @staticmethod
    def year_level_gap(
        actual_year_level: int,
        years_since_admission: int,
    ) -> int:
        """
        Year Level Gap = Actual Year Level − Expected Year Level
        Expected Year Level = years_since_admission (capped at program duration)

        Positive = student is behind (at risk).
        Negative = student is ahead (unusual, possible double major or acceleration).
        Zero     = on track.
        """
        expected = min(years_since_admission, Thresholds.STANDARD_PROGRAM_YEARS)
        return actual_year_level - expected

    @staticmethod
    def expected_year_level(years_since_admission: int) -> int:
        """
        Compute the expected year level based on years since first enrollment.
        Capped at standard program duration.
        """
        return min(
            max(years_since_admission, 1),
            Thresholds.STANDARD_PROGRAM_YEARS,
        )

    @staticmethod
    def semesters_remaining(
        current_year_level: int,
        program_duration_years: int = 4,
        current_semester: int = 1,
    ) -> int:
        """
        Estimate remaining semesters to graduation.
        Each year = 2 semesters. Does not include summer terms.
        """
        total_semesters    = program_duration_years * 2
        completed_semesters = (current_year_level - 1) * 2 + (current_semester - 1)
        return max(total_semesters - completed_semesters, 0)


# ==============================================================================
# Risk scoring rules
# ==============================================================================

@dataclass
class RiskScore:
    """Result of a risk assessment for a program/year_level group."""
    composite_score:    float
    risk_level:         RiskLevel
    dropout_component:  float
    gap_component:      float
    shifter_component:  float


class RiskRules:
    """
    Dropout risk scoring for aggregated program/year_level groups.

    Composite score is a weighted sum of:
        - Dropout rate component (historical dropout rate for this group)
        - Year level gap component (proportion of super seniors in group)
        - Shifter outflow component (proportion of students leaving the program)

    Score range: 0.0 (no risk) to 1.0 (maximum risk).
    """

    @staticmethod
    def compute_risk_score(
        dropout_rate:       float,   # 0.0–100.0
        year_level:         int,
        total_enrolled:     int,
        shifters_out:       int,
    ) -> RiskScore:
        """
        Compute a composite dropout risk score for an aggregated group.

        Args:
            dropout_rate    — current semester dropout rate (0–100)
            year_level      — year level of the group (1–6)
            total_enrolled  — number of students in this group
            shifters_out    — number of students leaving this program

        Returns a RiskScore with composite score and risk level label.
        """
        # Normalize each component to 0.0–1.0
        dropout_component = min(dropout_rate / 100.0, 1.0)

        # Year level gap component: year_level / MAX gives a 0–1 score
        # Super seniors (year 5+) score higher
        gap_component = min(
            (year_level - 1) / (Thresholds.MAX_PROGRAM_YEARS - 1),
            1.0,
        )

        # Shifter outflow component
        if total_enrolled > 0:
            shifter_component = min(shifters_out / total_enrolled, 1.0)
        else:
            shifter_component = 0.0

        # Weighted composite
        composite = (
            dropout_component  * Thresholds.DROPOUT_RATE_WEIGHT
            + gap_component    * Thresholds.YEAR_LEVEL_GAP_WEIGHT
            + shifter_component * Thresholds.SHIFTER_HISTORY_WEIGHT
        )
        composite = round(min(composite, 1.0), 4)

        # Classify risk level
        if composite <= Thresholds.RISK_LOW_MAX:
            risk_level = RiskLevel.LOW
        elif composite <= Thresholds.RISK_MODERATE_MAX:
            risk_level = RiskLevel.MODERATE
        elif composite <= Thresholds.RISK_HIGH_MAX:
            risk_level = RiskLevel.HIGH
        else:
            risk_level = RiskLevel.CRITICAL

        return RiskScore(
            composite_score    = composite,
            risk_level         = risk_level,
            dropout_component  = round(dropout_component, 4),
            gap_component      = round(gap_component, 4),
            shifter_component  = round(shifter_component, 4),
        )

    @staticmethod
    def flag_at_risk_cohort(retention_rate: float | None) -> bool:
        """
        Flag a cohort as at-risk if retention drops below threshold.
        Used in cohort survival analysis.
        """
        if retention_rate is None:
            return False
        return retention_rate < Thresholds.COHORT_RETENTION_THRESHOLD


# ==============================================================================
# Data quality rules
# ==============================================================================

class DataQualityRules:
    """
    Flags data quality issues before Silver insertion.
    Returns warning strings — does not block ingestion.
    """

    @staticmethod
    def check_enrollment_row(
        academic_year:      str | None,
        semester:           int | None,
        program_code:       str | None,
        year_level:         int | None,
        total_enrolled:     int | None,
    ) -> list[str]:
        """Return a list of data quality warnings for an enrollment row."""
        warnings: list[str] = []

        if not academic_year:
            warnings.append("Missing academic_year")
        if semester not in (1, 2, 3):
            warnings.append(f"Invalid semester: {semester!r}")
        if not program_code:
            warnings.append("Missing program_code")
        if year_level is None or not (1 <= year_level <= 6):
            warnings.append(f"Invalid year_level: {year_level!r}")
        if total_enrolled is not None and total_enrolled < 0:
            warnings.append(f"Negative total_enrolled: {total_enrolled}")
        if total_enrolled == 0:
            warnings.append("total_enrolled is zero — row may be empty")

        return warnings

    @staticmethod
    def check_outcome_row(
        graduates:   int | None,
        dropouts:    int | None,
        total_enrolled: int | None,
    ) -> list[str]:
        """Return data quality warnings for a student outcome row."""
        warnings: list[str] = []

        if total_enrolled and graduates and graduates > total_enrolled:
            warnings.append(
                f"Graduates ({graduates}) exceeds total_enrolled ({total_enrolled})"
            )
        if total_enrolled and dropouts and dropouts > total_enrolled:
            warnings.append(
                f"Dropouts ({dropouts}) exceeds total_enrolled ({total_enrolled})"
            )
        if (
            total_enrolled
            and graduates is not None
            and dropouts is not None
            and (graduates + dropouts) > total_enrolled
        ):
            warnings.append(
                f"Graduates + Dropouts ({graduates + dropouts}) "
                f"exceeds total_enrolled ({total_enrolled})"
            )

        return warnings