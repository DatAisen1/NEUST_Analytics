# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# ingestion/load_bronze.py
# Reads Excel source files and loads raw data into Bronze PostgreSQL tables
# ==============================================================================

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pandas as pd
from sqlalchemy import text

from database.connection import get_session
from database.models.bronze_models import (
    BronzeEnrollmentFlow,
    BronzeIngestionLog,
    BronzeStudentOutcomes,
)
from ingestion.schema_validator import validate_file
from utils.config import get_config
from utils.logger import log_step_failure, log_step_start, log_step_success, logger

_config = get_config()


# ==============================================================================
# Result dataclass
# ==============================================================================

@dataclass
class IngestionResult:
    """Summary of a single ingestion run."""

    batch_id:        UUID
    source_file:     str
    ef_rows_loaded:  int = 0
    so_rows_loaded:  int = 0
    ef_rows_rejected: int = 0
    so_rows_rejected: int = 0
    status:          str = "pending"   # pending | success | failed | partial
    error_message:   str | None = None
    elapsed_seconds: float = 0.0

    @property
    def total_rows_loaded(self) -> int:
        return self.ef_rows_loaded + self.so_rows_loaded

    @property
    def total_rows_rejected(self) -> int:
        return self.ef_rows_rejected + self.so_rows_rejected


# ==============================================================================
# Main ingestion class
# ==============================================================================

class BronzeLoader:
    """
    Loads raw NEUST enrollment Excel data into the Bronze PostgreSQL schema.

    Pipeline:
        1. Validate the file schema (columns, types, required fields)
        2. Check the file has not already been ingested (idempotency)
        3. Read both sheets into DataFrames
        4. Clean column names and strip whitespace
        5. Batch-insert rows into bronze.enrollment_flow
        6. Batch-insert rows into bronze.student_outcomes
        7. Write an audit record to bronze.ingestion_log

    All columns are stored as-is (no transformation). The Bronze layer
    is append-only — existing rows are never modified or deleted.

    Usage:
        loader = BronzeLoader("data/raw/enrollment_AY2024.xlsx")
        result = loader.run()
        print(result.total_rows_loaded)
    """

    def __init__(self, file_path: str | Path) -> None:
        self.file_path  = Path(file_path)
        self.batch_id   = uuid4()
        self._started   = time.monotonic()
        self._skip_audit_log = False

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> IngestionResult:
        """Execute the full ingestion pipeline for one Excel file."""
        result = IngestionResult(
            batch_id=self.batch_id,
            source_file=self.file_path.name,
        )

        log_step_start(1, f"Bronze ingestion — {self.file_path.name}")

        try:
            # Step 1 — validate schema
            logger.info("Step 1/5 — Validating file schema ...")
            validate_file(self.file_path)

            # Step 2 — idempotency check
            logger.info("Step 2/5 — Checking for duplicate ingestion ...")
            existing_batch_id = self._already_ingested()
            if existing_batch_id is not None:
                logger.warning(
                    "File '{}' has already been ingested. "
                    "Skipping to prevent duplicate data. "
                    "Rename the file or delete the Bronze record to re-ingest.",
                    self.file_path.name,
                )
                result.batch_id = existing_batch_id
                result.status = "success"
                result.elapsed_seconds = time.monotonic() - self._started
                self._skip_audit_log = True
                return result

            # Step 3 — read sheets
            logger.info("Step 3/5 — Reading Excel sheets ...")
            ef_df, so_df = self._read_sheets()

            # Step 4 — load enrollment flow
            logger.info("Step 4/5 — Loading Enrollment_Flow ({} rows) ...", len(ef_df))
            ef_loaded, ef_rejected = self._load_enrollment_flow(ef_df)
            result.ef_rows_loaded   = ef_loaded
            result.ef_rows_rejected = ef_rejected

            # Step 5 — load student outcomes
            logger.info("Step 5/5 — Loading Student_Outcomes ({} rows) ...", len(so_df))
            so_loaded, so_rejected = self._load_student_outcomes(so_df)
            result.so_rows_loaded   = so_loaded
            result.so_rows_rejected = so_rejected

            # Determine final status
            if result.total_rows_rejected == 0:
                result.status = "success"
            else:
                result.status = "partial"

        except ValueError as exc:
            # Validation failure — do not write any rows
            result.status = "failed"
            result.error_message = str(exc)
            log_step_failure(1, "Bronze ingestion", exc)

        except Exception as exc:
            result.status = "failed"
            result.error_message = str(exc)
            logger.exception("Unexpected error during Bronze ingestion: {}", exc)
            log_step_failure(1, "Bronze ingestion", exc)

        finally:
            result.elapsed_seconds = time.monotonic() - self._started
            if not self._skip_audit_log:
                self._write_ingestion_log(result)

        if result.status in ("success", "partial"):
            log_step_success(
                1,
                "Bronze ingestion",
                rows=result.total_rows_loaded,
            )
            logger.info(
                "Ingestion complete — batch_id={} | loaded={} | rejected={} | {:.2f}s",
                result.batch_id,
                result.total_rows_loaded,
                result.total_rows_rejected,
                result.elapsed_seconds,
            )

        return result

    # ------------------------------------------------------------------
    # Idempotency check
    # ------------------------------------------------------------------

    def _already_ingested(self) -> UUID | None:
        """
        Check if this filename has already been successfully ingested.

        Returns the original batch_id when the file has already been loaded,
        otherwise returns None.
        """
        with get_session() as session:
            result = session.execute(
                text(
                    """
                    SELECT batch_id FROM bronze.ingestion_log
                    WHERE source_file = :fname
                      AND status IN ('success', 'partial')
                    ORDER BY completed_at DESC
                    LIMIT 1
                    """
                ),
                {"fname": self.file_path.name},
            )
            row = result.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # Sheet reading
    # ------------------------------------------------------------------

    def _read_sheets(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Read both Excel sheets and clean column names."""
        xl = pd.ExcelFile(self.file_path, engine="openpyxl")

        ef_df = xl.parse("Enrollment_Flow",  dtype=str)
        so_df = xl.parse("Student_Outcomes", dtype=str)

        ef_df = self._clean_dataframe(ef_df)
        so_df = self._clean_dataframe(so_df)

        logger.debug(
            "Sheets loaded — Enrollment_Flow: {} rows | Student_Outcomes: {} rows",
            len(ef_df), len(so_df),
        )
        return ef_df, so_df

    def _clean_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """Strip whitespace from column names and string values. Drop fully empty rows."""
        df.columns = df.columns.str.strip()
        df = df.dropna(how="all").reset_index(drop=True)
        # Strip leading/trailing whitespace from all string cells
        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].str.strip()
        return df

    # ------------------------------------------------------------------
    # Enrollment flow loader
    # ------------------------------------------------------------------

    def _load_enrollment_flow(
        self, df: pd.DataFrame
    ) -> tuple[int, int]:
        """
        Insert Enrollment_Flow rows into bronze.enrollment_flow.

        Returns (rows_inserted, rows_rejected).
        Uses batch inserts of size config.batch_size for performance.
        """
        rows_inserted = 0
        rows_rejected = 0
        records       = []

        for idx, row in df.iterrows():
            try:
                record = BronzeEnrollmentFlow(
                    source_file         = self.file_path.name,
                    batch_id            = self.batch_id,
                    academic_year       = self._safe_str(row.get("Academic Year")),
                    semester            = self._safe_str(row.get("Semester")),
                    college_department  = self._safe_str(row.get("College/Department")),
                    program_course      = self._safe_str(row.get("Program/Course")),
                    major               = self._safe_str(row.get("Major")),
                    year_level          = self._safe_str(row.get("Year Level")),
                    gender              = self._safe_str(row.get("Gender")),
                    applicants          = self._safe_int(row.get("Applicants")),
                    accepted_applicants = self._safe_int(row.get("Accepted Applicants")),
                    total_enrolled      = self._safe_int(row.get("Total Enrolled")),
                    new_students        = self._safe_int(row.get("New Students")),
                    transferees         = self._safe_int(row.get("Transferees")),
                    returnees           = self._safe_int(row.get("Returnees")),
                )
                records.append(record)
            except Exception as exc:
                rows_rejected += 1
                logger.warning("Row {} rejected from Enrollment_Flow: {}", idx, exc)

        rows_inserted = self._batch_insert(records, "bronze.enrollment_flow")
        rows_rejected += len(records) - rows_inserted

        return rows_inserted, rows_rejected

    # ------------------------------------------------------------------
    # Student outcomes loader
    # ------------------------------------------------------------------

    def _load_student_outcomes(
        self, df: pd.DataFrame
    ) -> tuple[int, int]:
        """
        Insert Student_Outcomes rows into bronze.student_outcomes.

        Returns (rows_inserted, rows_rejected).
        """
        rows_rejected = 0
        records       = []

        for idx, row in df.iterrows():
            try:
                record = BronzeStudentOutcomes(
                    source_file        = self.file_path.name,
                    batch_id           = self.batch_id,
                    academic_year      = self._safe_str(row.get("Academic Year")),
                    semester           = self._safe_str(row.get("Semester")),
                    college_department = self._safe_str(row.get("College/Department")),
                    program_course     = self._safe_str(row.get("Program/Course")),
                    major              = self._safe_str(row.get("Major")),
                    year_level         = self._safe_str(row.get("Year Level")),
                    gender             = self._safe_str(row.get("Gender")),
                    graduates          = self._safe_int(row.get("Graduates")),
                    dropouts           = self._safe_int(row.get("Dropouts")),
                    shifters_out       = self._safe_int(row.get("Shifters Out")),
                    shifters_in        = self._safe_int(row.get("Shifters In")),
                )
                records.append(record)
            except Exception as exc:
                rows_rejected += 1
                logger.warning("Row {} rejected from Student_Outcomes: {}", idx, exc)

        rows_inserted = self._batch_insert(records, "bronze.student_outcomes")
        rows_rejected += len(records) - rows_inserted

        return rows_inserted, rows_rejected

    # ------------------------------------------------------------------
    # Batch insert (performance-critical)
    # ------------------------------------------------------------------

    def _batch_insert(self, records: list, table_label: str) -> int:
        """
        Insert records in batches of config.batch_size.

        Each batch is a single transaction. If a batch fails, it is
        retried row-by-row so one bad row doesn't discard the entire batch.

        Returns the number of rows successfully inserted.
        """
        if not records:
            return 0

        batch_size    = _config.batch_size
        total_batches = (len(records) + batch_size - 1) // batch_size
        rows_inserted = 0

        for batch_num in range(total_batches):
            start  = batch_num * batch_size
            end    = start + batch_size
            batch  = records[start:end]

            try:
                with get_session() as session:
                    session.bulk_save_objects(batch)
                rows_inserted += len(batch)
                logger.debug(
                    "Batch {}/{} inserted — {} rows into {}",
                    batch_num + 1, total_batches, len(batch), table_label,
                )
            except Exception as exc:
                # Batch failed — retry row by row
                logger.warning(
                    "Batch {}/{} failed for {} — retrying row by row. Error: {}",
                    batch_num + 1, total_batches, table_label, exc,
                )
                rows_inserted += self._row_by_row_insert(batch, table_label)

        return rows_inserted

    def _row_by_row_insert(self, records: list, table_label: str) -> int:
        """Fallback: insert records one at a time. Used when a batch fails."""
        inserted = 0
        for record in records:
            try:
                with get_session() as session:
                    session.add(record)
                inserted += 1
            except Exception as exc:
                logger.error(
                    "Row insert failed for {} — skipping. Error: {}",
                    table_label, exc,
                )
        return inserted

    # ------------------------------------------------------------------
    # Audit log writer
    # ------------------------------------------------------------------

    def _write_ingestion_log(self, result: IngestionResult) -> None:
        """Write two audit records to bronze.ingestion_log (one per table)."""
        completed = datetime.now(timezone.utc)
        logs = [
            BronzeIngestionLog(
                batch_id      = result.batch_id,
                source_file   = result.source_file,
                target_table  = "bronze.enrollment_flow",
                rows_inserted = result.ef_rows_loaded,
                rows_rejected = result.ef_rows_rejected,
                status        = result.status,
                error_message = result.error_message,
                completed_at  = completed,
            ),
            BronzeIngestionLog(
                batch_id      = result.batch_id,
                source_file   = result.source_file,
                target_table  = "bronze.student_outcomes",
                rows_inserted = result.so_rows_loaded,
                rows_rejected = result.so_rows_rejected,
                status        = result.status,
                error_message = result.error_message,
                completed_at  = completed,
            ),
        ]
        try:
            with get_session() as session:
                session.bulk_save_objects(logs)
            logger.debug("Ingestion audit log written — batch_id={}", result.batch_id)
        except Exception as exc:
            # Never let a log write failure crash the pipeline
            logger.error("Failed to write ingestion log: {}", exc)

    # ------------------------------------------------------------------
    # Type-safe helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_str(value) -> str | None:
        """Convert to string or None if blank/null."""
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        s = str(value).strip()
        return s if s else None

    @staticmethod
    def _safe_int(value) -> int | None:
        """Convert to int or None if blank/null/non-numeric."""
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        try:
            return int(float(str(value).replace(",", "").strip()))
        except (ValueError, TypeError):
            return None


# ==============================================================================
# Module-level runner — called by pipeline.py
# ==============================================================================

def run_bronze(file_path: str | Path | None = None) -> IngestionResult:
    """
    Entry point called by pipeline.py.

    If file_path is not provided, scans config.raw_data_path for
    all .xlsx files and ingests each one that has not yet been loaded.

    Usage in pipeline.py:
        from ingestion.load_bronze import run_bronze
        result = run_bronze()
    """
    if file_path:
        loader = BronzeLoader(file_path)
        return loader.run()

    # Auto-discover all Excel files in the raw data folder
    raw_path = _config.raw_data_path
    xlsx_files = sorted(raw_path.glob("*.xlsx"))

    if not xlsx_files:
        logger.warning("No .xlsx files found in: {}", raw_path)
        return IngestionResult(batch_id=uuid4(), source_file="none", status="success")

    logger.info("Found {} Excel file(s) in {}", len(xlsx_files), raw_path)

    last_result = None
    for xlsx_file in xlsx_files:
        loader = BronzeLoader(xlsx_file)
        last_result = loader.run()

    return last_result