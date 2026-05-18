# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# database/models/bronze_models.py
# SQLAlchemy ORM models for the Bronze (raw ingestion) schema
# ==============================================================================

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    BigInteger, Boolean, Column, Integer, SmallInteger,
    Text, TIMESTAMP, CheckConstraint, Index
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, declared_attr


# ------------------------------------------------------------------------------
# Base class shared across all models
# ------------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


class TimestampMixin:
    """Adds ingested_at auto-timestamp to Bronze tables."""

    ingested_at: Column = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="UTC timestamp when this row was loaded into Bronze",
    )


class SoftDeleteMixin:
    """Adds soft-delete support — rows are never physically removed."""

    is_deleted: Column = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Logical delete flag — never hard-delete Bronze records",
    )
    deleted_at: Column = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="UTC timestamp when this row was soft-deleted",
    )


# ==============================================================================
# BRONZE TABLES
# ==============================================================================

class BronzeEnrollmentFlow(TimestampMixin, SoftDeleteMixin, Base):
    """
    Raw enrollment intake records — maps 1:1 to the Enrollment_Flow sheet.

    All source columns stored as TEXT to preserve exact values from the
    Excel export. Numeric fields are INTEGER but nullable to handle blanks.
    No business logic is applied at this layer.
    """

    __tablename__ = "enrollment_flow"

    @declared_attr
    def __table_args__(cls):
        return (
            Index("idx_bronze_ef_batch_id",    "batch_id"),
            Index("idx_bronze_ef_ingested_at", "ingested_at"),
            Index("idx_bronze_ef_acad_year",   "academic_year"),
            {"schema": "bronze", "comment": "Raw enrollment intake — Enrollment_Flow sheet"},
        )

    # Primary key
    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Source tracking
    source_file = Column(
        Text,
        nullable=False,
        comment="Original filename (e.g. enrollment_flow_AY2024.xlsx)",
    )
    batch_id = Column(
        UUID(as_uuid=True),
        nullable=False,
        default=uuid4,
        comment="Groups all rows from one ingestion run",
    )

    # Raw source columns (TEXT — no casting at Bronze)
    academic_year       = Column(Text, nullable=True, comment="e.g. '2023-2024'")
    semester            = Column(Text, nullable=True, comment="e.g. '1st Semester'")
    college_department  = Column(Text, nullable=True)
    program_course      = Column(Text, nullable=True)
    major               = Column(Text, nullable=True)
    year_level          = Column(Text, nullable=True)
    gender              = Column(Text, nullable=True)

    # Numeric metrics (nullable — source may contain blank cells)
    applicants          = Column(Integer, nullable=True)
    accepted_applicants = Column(Integer, nullable=True)
    total_enrolled      = Column(Integer, nullable=True)
    new_students        = Column(Integer, nullable=True)
    transferees         = Column(Integer, nullable=True)
    returnees           = Column(Integer, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<BronzeEnrollmentFlow id={self.id} "
            f"ay={self.academic_year!r} sem={self.semester!r} "
            f"program={self.program_course!r}>"
        )


class BronzeStudentOutcomes(TimestampMixin, SoftDeleteMixin, Base):
    """
    Raw student outcome records — maps 1:1 to the Student_Outcomes sheet.

    Stores graduates, dropouts, and shifter counts exactly as exported.
    No business rules or status classifications applied here.
    """

    __tablename__ = "student_outcomes"

    @declared_attr
    def __table_args__(cls):
        return (
            Index("idx_bronze_so_batch_id",    "batch_id"),
            Index("idx_bronze_so_ingested_at", "ingested_at"),
            Index("idx_bronze_so_acad_year",   "academic_year"),
            {"schema": "bronze", "comment": "Raw student outcomes — Student_Outcomes sheet"},
        )

    # Primary key
    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Source tracking
    source_file = Column(Text, nullable=False)
    batch_id    = Column(UUID(as_uuid=True), nullable=False, default=uuid4)

    # Raw source columns
    academic_year       = Column(Text, nullable=True)
    semester            = Column(Text, nullable=True)
    college_department  = Column(Text, nullable=True)
    program_course      = Column(Text, nullable=True)
    major               = Column(Text, nullable=True)
    year_level          = Column(Text, nullable=True)
    gender              = Column(Text, nullable=True)

    # Outcome metrics
    graduates    = Column(Integer, nullable=True)
    dropouts     = Column(Integer, nullable=True)
    shifters_out = Column(Integer, nullable=True)
    shifters_in  = Column(Integer, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<BronzeStudentOutcomes id={self.id} "
            f"ay={self.academic_year!r} sem={self.semester!r} "
            f"program={self.program_course!r}>"
        )


class BronzeIngestionLog(Base):
    """
    Audit trail for every file loaded into Bronze.

    One row per (batch_id, target_table) pair. Used by the pipeline
    to detect re-ingestion of the same file and to track failures.
    """

    __tablename__ = "ingestion_log"

    @declared_attr
    def __table_args__(cls):
        return (
            CheckConstraint(
                "status IN ('pending', 'success', 'failed', 'partial')",
                name="ck_bronze_log_status",
            ),
            Index("idx_bronze_log_batch_id", "batch_id"),
            Index("idx_bronze_log_status",   "status"),
            {"schema": "bronze", "comment": "Audit log for every Bronze ingestion run"},
        )

    id            = Column(BigInteger, primary_key=True, autoincrement=True)
    batch_id      = Column(UUID(as_uuid=True), nullable=False, default=uuid4)
    source_file   = Column(Text, nullable=False)
    target_table  = Column(Text, nullable=False)
    rows_inserted = Column(Integer, nullable=False, default=0)
    rows_rejected = Column(Integer, nullable=False, default=0)
    status        = Column(Text, nullable=False, default="pending")
    error_message = Column(Text, nullable=True)
    started_at    = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at  = Column(TIMESTAMP(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<BronzeIngestionLog id={self.id} "
            f"file={self.source_file!r} status={self.status!r}>"
        )