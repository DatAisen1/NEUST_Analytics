# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# database/models/silver_models.py
# SQLAlchemy ORM models for the Silver (cleaned & standardized) schema
# ==============================================================================

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    BigInteger, Boolean, Column, ForeignKey, Integer,
    Numeric, SmallInteger, Text, TIMESTAMP,
    CheckConstraint, Index, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship, declared_attr


# ------------------------------------------------------------------------------
# Base — Silver uses its own declarative base
# ------------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


class TransformMixin:
    """Adds Bronze lineage tracking to all Silver tables."""

    bronze_batch_id = Column(
        UUID(as_uuid=True),
        nullable=False,
        comment="batch_id of the Bronze run this row was derived from",
    )
    transformed_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="UTC timestamp of the Silver transformation",
    )


# ==============================================================================
# SILVER LOOKUP / DIMENSION TABLES
# ==============================================================================

class SilverAcademicPeriod(Base):
    """
    Standardized academic year + semester combinations.

    Created during Bronze → Silver transformation when new periods are
    encountered. Used as a FK in all Silver fact-like tables.

    Semester values:
        1 = 1st Semester
        2 = 2nd Semester
        3 = Summer
    """

    __tablename__ = "academic_periods"

    @declared_attr
    def __table_args__(cls):
        return (
            UniqueConstraint("academic_year", "semester", name="uq_silver_ap"),
            {"schema": "silver", "comment": "Canonical academic periods reference table"},
        )

    id            = Column(Integer, primary_key=True, autoincrement=True)
    academic_year = Column(Text,     nullable=False, comment="e.g. '2023-2024'")
    semester      = Column(SmallInteger, nullable=False, comment="1, 2, or 3 (summer)")
    year_start    = Column(SmallInteger, nullable=False)
    year_end      = Column(SmallInteger, nullable=False)
    label         = Column(Text,     nullable=False, comment="e.g. 'AY 2023-2024 Sem 1'")
    is_current    = Column(Boolean,  nullable=False, default=False)
    created_at    = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    enrollment_flows = relationship("SilverEnrollmentFlow", back_populates="period")
    student_outcomes = relationship("SilverStudentOutcomes", back_populates="period")

    def __repr__(self) -> str:
        return f"<SilverAcademicPeriod {self.label!r}>"


class SilverProgram(Base):
    """
    Canonical program reference table.

    Bronze program names are standardized against this table during
    Silver transformation (e.g. 'BS CS', 'BSCS', 'BS-CS' → 'BSCS').
    """

    __tablename__ = "programs"

    @declared_attr
    def __table_args__(cls):
        return (
            UniqueConstraint("program_code", name="uq_silver_programs_program_code"),
            Index("idx_silver_programs_college", "college"),
            {
                "schema": "silver",
                "comment": "Canonical programs and colleges reference table",
            },
        )

    id             = Column(Integer, primary_key=True, autoincrement=True)
    program_code   = Column(Text,        nullable=False, unique=True, comment="Canonical code e.g. 'BSCS'")
    program_name   = Column(Text,        nullable=False)
    college        = Column(Text,        nullable=False)
    department     = Column(Text,        nullable=True)
    duration_years = Column(SmallInteger, nullable=False, default=4)
    is_active      = Column(Boolean,     nullable=False, default=True)
    created_at     = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at     = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    enrollment_flows = relationship("SilverEnrollmentFlow", back_populates="program")
    student_outcomes = relationship("SilverStudentOutcomes", back_populates="program")

    def __repr__(self) -> str:
        return f"<SilverProgram {self.program_code!r} — {self.college!r}>"


# ==============================================================================
# SILVER FACT-LIKE TABLES
# ==============================================================================

class SilverEnrollmentFlow(TransformMixin, Base):
    """
    Cleaned and standardized enrollment intake records.

    Differences from Bronze:
    - academic_year and semester are validated and type-cast
    - program names are standardized to a canonical code
    - null metrics are replaced with 0
    - acceptance_rate is a generated/computed column (stored in DB)
    - duplicates are removed via the unique constraint
    """

    __tablename__ = "enrollment_flow"

    @declared_attr
    def __table_args__(cls):
        return (
            UniqueConstraint(
                "academic_year", "semester", "program_code",
                "major", "year_level", "gender",
                name="uq_silver_ef",
            ),
            CheckConstraint("year_level BETWEEN 1 AND 6",   name="ck_silver_ef_year_level"),
            CheckConstraint("applicants >= 0",               name="ck_silver_ef_applicants"),
            CheckConstraint("accepted_applicants >= 0",      name="ck_silver_ef_accepted"),
            CheckConstraint("total_enrolled >= 0",           name="ck_silver_ef_enrolled"),
            CheckConstraint("new_students >= 0",             name="ck_silver_ef_new"),
            CheckConstraint("transferees >= 0",              name="ck_silver_ef_transferees"),
            CheckConstraint("returnees >= 0",                name="ck_silver_ef_returnees"),
            CheckConstraint(
                "gender IN ('Male', 'Female', 'Other', 'Not Specified')",
                name="ck_silver_ef_gender",
            ),
            Index("idx_silver_ef_period",   "period_id"),
            Index("idx_silver_ef_program",  "program_id"),
            Index("idx_silver_ef_year_sem", "academic_year", "semester"),
            Index("idx_silver_ef_college",  "college"),
            {
                "schema": "silver",
                "comment": "Cleaned enrollment intake — Bronze→Silver output",
            },
        )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Foreign keys
    period_id  = Column(Integer, ForeignKey("silver.academic_periods.id"), nullable=False)
    program_id = Column(Integer, ForeignKey("silver.programs.id"),         nullable=False)

    # Standardized fields
    academic_year  = Column(Text,        nullable=False)
    semester       = Column(SmallInteger, nullable=False)
    college        = Column(Text,        nullable=False)
    program_code   = Column(Text,        nullable=False)
    major          = Column(Text,        nullable=True)
    year_level     = Column(SmallInteger, nullable=False)
    gender         = Column(Text,        nullable=True)

    # Enrollment metrics (0 when source is null)
    applicants          = Column(Integer, nullable=False, default=0)
    accepted_applicants = Column(Integer, nullable=False, default=0)
    total_enrolled      = Column(Integer, nullable=False, default=0)
    new_students        = Column(Integer, nullable=False, default=0)
    transferees         = Column(Integer, nullable=False, default=0)
    returnees           = Column(Integer, nullable=False, default=0)

    # Computed rate (Python-side; the DB generated column handles persistence)
    acceptance_rate = Column(
        Numeric(5, 2),
        nullable=True,
        comment="accepted_applicants / applicants * 100 — populated by transformation",
    )

    # Relationships
    period  = relationship("SilverAcademicPeriod", back_populates="enrollment_flows")
    program = relationship("SilverProgram",         back_populates="enrollment_flows")

    def __repr__(self) -> str:
        return (
            f"<SilverEnrollmentFlow id={self.id} "
            f"ay={self.academic_year!r} sem={self.semester} "
            f"prog={self.program_code!r} yl={self.year_level}>"
        )


class SilverStudentOutcomes(TransformMixin, Base):
    """
    Cleaned and standardized student outcome records.

    Deduplication key: (academic_year, semester, program_code, major, year_level, gender).
    Shifter balance is computed during aggregation in Gold, not stored here.
    """

    __tablename__ = "student_outcomes"

    @declared_attr
    def __table_args__(cls):
        return (
            UniqueConstraint(
                "academic_year", "semester", "program_code",
                "major", "year_level", "gender",
                name="uq_silver_so",
            ),
            CheckConstraint("year_level BETWEEN 1 AND 6", name="ck_silver_so_year_level"),
            CheckConstraint("graduates >= 0",              name="ck_silver_so_graduates"),
            CheckConstraint("dropouts >= 0",               name="ck_silver_so_dropouts"),
            CheckConstraint("shifters_out >= 0",           name="ck_silver_so_shifters_out"),
            CheckConstraint("shifters_in >= 0",            name="ck_silver_so_shifters_in"),
            CheckConstraint(
                "gender IN ('Male', 'Female', 'Other', 'Not Specified')",
                name="ck_silver_so_gender",
            ),
            Index("idx_silver_so_period",   "period_id"),
            Index("idx_silver_so_program",  "program_id"),
            Index("idx_silver_so_year_sem", "academic_year", "semester"),
            {
                "schema": "silver",
                "comment": "Cleaned student outcomes — Bronze→Silver output",
            },
        )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Foreign keys
    period_id  = Column(Integer, ForeignKey("silver.academic_periods.id"), nullable=False)
    program_id = Column(Integer, ForeignKey("silver.programs.id"),         nullable=False)

    # Standardized fields
    academic_year = Column(Text,        nullable=False)
    semester      = Column(SmallInteger, nullable=False)
    college       = Column(Text,        nullable=False)
    program_code  = Column(Text,        nullable=False)
    major         = Column(Text,        nullable=True)
    year_level    = Column(SmallInteger, nullable=False)
    gender        = Column(Text,        nullable=True)

    # Outcome metrics
    graduates    = Column(Integer, nullable=False, default=0)
    dropouts     = Column(Integer, nullable=False, default=0)
    shifters_out = Column(Integer, nullable=False, default=0)
    shifters_in  = Column(Integer, nullable=False, default=0)

    # Relationships
    period  = relationship("SilverAcademicPeriod", back_populates="student_outcomes")
    program = relationship("SilverProgram",         back_populates="student_outcomes")

    def __repr__(self) -> str:
        return (
            f"<SilverStudentOutcomes id={self.id} "
            f"ay={self.academic_year!r} sem={self.semester} "
            f"prog={self.program_code!r} yl={self.year_level}>"
        )


class SilverTransformationLog(Base):
    """
    Audit log for Bronze → Silver transformation runs.

    One row per (bronze_batch_id, target_table). Used to detect
    whether a batch has already been transformed and to diagnose failures.
    """

    __tablename__ = "transformation_log"

    @declared_attr
    def __table_args__(cls):
        return (
            CheckConstraint(
                "status IN ('pending', 'success', 'failed', 'partial')",
                name="ck_silver_log_status",
            ),
            {"schema": "silver", "comment": "Audit log for Silver transformation runs"},
        )

    id               = Column(BigInteger, primary_key=True, autoincrement=True)
    bronze_batch_id  = Column(UUID(as_uuid=True), nullable=False)
    target_table     = Column(Text,    nullable=False)
    rows_processed   = Column(Integer, nullable=False, default=0)
    rows_inserted    = Column(Integer, nullable=False, default=0)
    rows_updated     = Column(Integer, nullable=False, default=0)
    rows_skipped     = Column(Integer, nullable=False, default=0)
    status           = Column(Text,    nullable=False, default="pending")
    error_message    = Column(Text,    nullable=True)
    started_at       = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at     = Column(TIMESTAMP(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<SilverTransformationLog id={self.id} "
            f"table={self.target_table!r} status={self.status!r}>"
        )