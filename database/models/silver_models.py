# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# database/models/gold_models.py
# SQLAlchemy ORM models for the Gold (analytics-ready star schema) layer
# ==============================================================================

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, Column, ForeignKey, Integer,
    Numeric, SmallInteger, Text, TIMESTAMP,
    CheckConstraint, Index, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship, declared_attr


# ------------------------------------------------------------------------------
# Base
# ------------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


class RefreshMixin:
    """Tracks when a Gold row was last rebuilt from Silver."""

    refreshed_at: Column = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        comment="UTC timestamp of the last Gold aggregation that wrote this row",
    )


# ==============================================================================
# DIMENSION TABLES
# ==============================================================================

class GoldDimTime(Base):
    """
    Time dimension — one row per academic year + semester.

    sort_key = year_start * 10 + semester  (e.g. 2023*10+1 = 20231)
    This allows ORDER BY sort_key to produce correct chronological order
    without string parsing in every query.
    """

    __tablename__ = "dim_time"

    @declared_attr
    def __table_args__(cls):
        return (
            UniqueConstraint("academic_year", "semester", name="uq_gold_dim_time"),
            Index("idx_gold_dim_time_sort", "sort_key"),
            {"schema": "gold", "comment": "Time dimension — academic year and semester"},
        )

    time_id        = Column(Integer,     primary_key=True, autoincrement=True)
    academic_year  = Column(Text,        nullable=False)
    semester       = Column(SmallInteger, nullable=False)
    year_start     = Column(SmallInteger, nullable=False)
    year_end       = Column(SmallInteger, nullable=False)
    semester_label = Column(Text,        nullable=False, comment="'1st Semester' | '2nd Semester' | 'Summer'")
    full_label     = Column(Text,        nullable=False, comment="'AY 2023-2024 1st Semester'")
    sort_key       = Column(Integer,     nullable=False, comment="year_start * 10 + semester — use for ORDER BY")
    is_current     = Column(Boolean,     nullable=False, default=False)

    # Relationships
    fact_metrics        = relationship("GoldFactEnrollmentMetrics", back_populates="time")
    agg_program_perf    = relationship("GoldAggProgramPerformance", back_populates="time")
    agg_college_summary = relationship("GoldAggCollegeSummary",     back_populates="time")

    def __repr__(self) -> str:
        return f"<GoldDimTime {self.full_label!r}>"


class GoldDimProgram(Base):
    """
    Program dimension — one row per canonical program code.

    Populated from silver.programs during the Gold aggregation step.
    Kept separate so Metabase can filter/group by college without
    joining fact tables.
    """

    __tablename__ = "dim_program"

    @declared_attr
    def __table_args__(cls):
        return (
            Index("idx_gold_dim_program_college", "college"),
            {"schema": "gold", "comment": "Program dimension — canonical programs and colleges"},
        )

    program_id     = Column(Integer,     primary_key=True, autoincrement=True)
    program_code   = Column(Text,        nullable=False, unique=True)
    program_name   = Column(Text,        nullable=False)
    college        = Column(Text,        nullable=False)
    department     = Column(Text,        nullable=True)
    duration_years = Column(SmallInteger, nullable=False, default=4)
    is_active      = Column(Boolean,     nullable=False, default=True)

    # Relationships
    fact_metrics     = relationship("GoldFactEnrollmentMetrics", back_populates="program")
    agg_program_perf = relationship("GoldAggProgramPerformance", back_populates="program")

    def __repr__(self) -> str:
        return f"<GoldDimProgram {self.program_code!r}>"


class GoldDimYearLevel(Base):
    """
    Year level dimension with descriptive labels.

    Pre-populated by the migration. Values 5 and 6 represent
    super seniors and extended students — used in at-risk detection.
    """

    __tablename__ = "dim_year_level"

    @declared_attr
    def __table_args__(cls):
        return (
            {"schema": "gold", "comment": "Year level dimension with readable labels"},
        )

    year_level_id = Column(Integer,     primary_key=True, autoincrement=True)
    year_level    = Column(SmallInteger, nullable=False, unique=True)
    level_name    = Column(Text,        nullable=False, comment="'Freshman' | 'Sophomore' | etc.")
    is_irregular  = Column(Boolean,     nullable=False, default=False)

    # Relationship
    fact_metrics = relationship("GoldFactEnrollmentMetrics", back_populates="year_level_dim")

    def __repr__(self) -> str:
        return f"<GoldDimYearLevel {self.year_level} — {self.level_name!r}>"


# ==============================================================================
# FACT TABLE
# ==============================================================================

class GoldFactEnrollmentMetrics(RefreshMixin, Base):
    """
    Central fact table — one row per (time, program, year_level, gender).

    Joins both enrollment flow and student outcomes into a single row
    so dashboards can compute any KPI with a single table scan.

    Pre-computed KPI columns (dropout_rate, graduation_rate, etc.) are
    populated during the Gold aggregation step to avoid repeated division
    in every Metabase chart query.

    Unique key: (time_id, program_id, year_level_id, gender)
    Use INSERT ... ON CONFLICT DO UPDATE on pipeline refreshes.
    """

    __tablename__ = "fact_enrollment_metrics"

    @declared_attr
    def __table_args__(cls):
        return (
            UniqueConstraint(
                "time_id", "program_id", "year_level_id", "gender",
                name="uq_gold_fact",
            ),
            CheckConstraint(
                "gender IN ('Male', 'Female', 'Other', 'Not Specified', 'All')",
                name="ck_gold_fact_gender",
            ),
            Index("idx_gold_fact_time",         "time_id"),
            Index("idx_gold_fact_program",       "program_id"),
            Index("idx_gold_fact_year_level",    "year_level_id"),
            Index("idx_gold_fact_time_program",  "time_id", "program_id"),
            {
                "schema": "gold",
                "comment": "Central fact table — enrollment and outcome metrics",
            },
        )

    metric_id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Dimension foreign keys
    time_id       = Column(Integer, ForeignKey("gold.dim_time.time_id"),           nullable=False)
    program_id    = Column(Integer, ForeignKey("gold.dim_program.program_id"),     nullable=False)
    year_level_id = Column(Integer, ForeignKey("gold.dim_year_level.year_level_id"), nullable=False)
    gender        = Column(Text,    nullable=True)

    # ── Enrollment metrics ──────────────────────────────────────────────────
    applicants          = Column(Integer, nullable=False, default=0)
    accepted_applicants = Column(Integer, nullable=False, default=0)
    total_enrolled      = Column(Integer, nullable=False, default=0)
    new_students        = Column(Integer, nullable=False, default=0)
    transferees         = Column(Integer, nullable=False, default=0)
    returnees           = Column(Integer, nullable=False, default=0)

    # ── Outcome metrics ─────────────────────────────────────────────────────
    graduates    = Column(Integer, nullable=False, default=0)
    dropouts     = Column(Integer, nullable=False, default=0)
    shifters_out = Column(Integer, nullable=False, default=0)
    shifters_in  = Column(Integer, nullable=False, default=0)

    # ── Pre-computed KPI rates ───────────────────────────────────────────────
    acceptance_rate     = Column(Numeric(5, 2), nullable=True,
                                 comment="accepted_applicants / applicants * 100")
    dropout_rate        = Column(Numeric(5, 2), nullable=True,
                                 comment="dropouts / total_enrolled * 100")
    graduation_rate     = Column(Numeric(5, 2), nullable=True,
                                 comment="graduates / total_enrolled * 100")
    retention_rate      = Column(Numeric(5, 2), nullable=True,
                                 comment="(total_enrolled - dropouts) / total_enrolled * 100")
    net_shifter_balance = Column(Integer, nullable=True,
                                 comment="shifters_in - shifters_out")

    # Relationships
    time          = relationship("GoldDimTime",      back_populates="fact_metrics")
    program       = relationship("GoldDimProgram",   back_populates="fact_metrics")
    year_level_dim = relationship("GoldDimYearLevel", back_populates="fact_metrics")

    def __repr__(self) -> str:
        return (
            f"<GoldFactEnrollmentMetrics metric_id={self.metric_id} "
            f"time_id={self.time_id} prog_id={self.program_id} "
            f"yl_id={self.year_level_id} gender={self.gender!r}>"
        )


# ==============================================================================
# AGGREGATE TABLES
# ==============================================================================

class GoldAggProgramPerformance(RefreshMixin, Base):
    """
    Pre-aggregated program-level KPIs per semester.

    Rebuilt from fact_enrollment_metrics on every pipeline run.
    Powers the program comparison dashboard in Metabase.

    enrollment_change_pct and dropout_change_pct are computed by
    comparing the current semester against the immediately preceding one.
    """

    __tablename__ = "agg_program_performance"

    @declared_attr
    def __table_args__(cls):
        return (
            UniqueConstraint("program_id", "time_id", name="uq_gold_agg_prog"),
            Index("idx_gold_agg_prog_time",    "time_id"),
            Index("idx_gold_agg_prog_program", "program_id"),
            {
                "schema": "gold",
                "comment": "Program KPI rollup per semester — feeds program comparison charts",
            },
        )

    agg_id     = Column(BigInteger, primary_key=True, autoincrement=True)
    program_id = Column(Integer, ForeignKey("gold.dim_program.program_id"), nullable=False)
    time_id    = Column(Integer, ForeignKey("gold.dim_time.time_id"),       nullable=False)

    # Totals
    total_applicants  = Column(Integer, nullable=False, default=0)
    total_accepted    = Column(Integer, nullable=False, default=0)
    total_enrolled    = Column(Integer, nullable=False, default=0)
    total_graduates   = Column(Integer, nullable=False, default=0)
    total_dropouts    = Column(Integer, nullable=False, default=0)
    total_shifters_out = Column(Integer, nullable=False, default=0)
    total_shifters_in  = Column(Integer, nullable=False, default=0)

    # KPIs
    avg_acceptance_rate = Column(Numeric(5, 2), nullable=True)
    avg_graduation_rate = Column(Numeric(5, 2), nullable=True)
    avg_dropout_rate    = Column(Numeric(5, 2), nullable=True)
    avg_retention_rate  = Column(Numeric(5, 2), nullable=True)

    # Trend vs previous semester
    enrollment_change_pct = Column(Numeric(6, 2), nullable=True,
                                   comment="% change in total_enrolled vs prior semester")
    dropout_change_pct    = Column(Numeric(6, 2), nullable=True,
                                   comment="% change in total_dropouts vs prior semester")

    # Relationships
    program = relationship("GoldDimProgram", back_populates="agg_program_perf")
    time    = relationship("GoldDimTime",    back_populates="agg_program_perf")

    def __repr__(self) -> str:
        return (
            f"<GoldAggProgramPerformance prog_id={self.program_id} "
            f"time_id={self.time_id} enrolled={self.total_enrolled}>"
        )


class GoldAggCollegeSummary(RefreshMixin, Base):
    """
    College-level rollup — one row per college per semester.

    The highest-level aggregation in the Gold layer. Used for the
    institution-wide summary dashboard and executive reports.
    """

    __tablename__ = "agg_college_summary"

    @declared_attr
    def __table_args__(cls):
        return (
            UniqueConstraint("college", "time_id", name="uq_gold_agg_college"),
            Index("idx_gold_agg_college_time", "time_id"),
            {
                "schema": "gold",
                "comment": "College-level enrollment and outcome rollup per semester",
            },
        )

    agg_id          = Column(BigInteger, primary_key=True, autoincrement=True)
    college         = Column(Text,    nullable=False)
    time_id         = Column(Integer, ForeignKey("gold.dim_time.time_id"), nullable=False)

    total_enrolled      = Column(Integer,     nullable=False, default=0)
    total_graduates     = Column(Integer,     nullable=False, default=0)
    total_dropouts      = Column(Integer,     nullable=False, default=0)
    program_count       = Column(Integer,     nullable=False, default=0)
    avg_dropout_rate    = Column(Numeric(5,2), nullable=True)
    avg_graduation_rate = Column(Numeric(5,2), nullable=True)

    # Relationship
    time = relationship("GoldDimTime", back_populates="agg_college_summary")

    def __repr__(self) -> str:
        return (
            f"<GoldAggCollegeSummary college={self.college!r} "
            f"time_id={self.time_id} enrolled={self.total_enrolled}>"
        )


class GoldPipelineRunLog(Base):
    """
    Top-level audit log for every pipeline.py execution.

    One row per run. Captures row counts at each layer and the final
    status. Used for monitoring and debugging pipeline failures.
    """

    __tablename__ = "pipeline_run_log"

    @declared_attr
    def __table_args__(cls):
        return (
            CheckConstraint(
                "status IN ('running', 'success', 'failed', 'partial')",
                name="ck_gold_run_status",
            ),
            {"schema": "gold", "comment": "Top-level audit log for every pipeline.py run"},
        )

    run_id        = Column(BigInteger, primary_key=True, autoincrement=True)
    run_label     = Column(Text,    nullable=True,  comment="Optional label e.g. 'SEM1_2024_manual'")
    started_at    = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at  = Column(TIMESTAMP(timezone=True), nullable=True)
    status        = Column(Text,    nullable=False, default="running")
    bronze_rows   = Column(Integer, nullable=True,  comment="Total rows in Bronze after this run")
    silver_rows   = Column(Integer, nullable=True,  comment="Total rows in Silver after this run")
    gold_rows     = Column(Integer, nullable=True,  comment="Total rows in Gold after this run")
    error_message = Column(Text,    nullable=True)
    triggered_by  = Column(Text,    nullable=False, default="manual")

    def __repr__(self) -> str:
        return (
            f"<GoldPipelineRunLog run_id={self.run_id} "
            f"status={self.status!r} label={self.run_label!r}>"
        )