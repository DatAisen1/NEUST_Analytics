# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# ingestion/load_bronze.py
# Reads Excel source files and loads raw data into Bronze PostgreSQL tables
#
# FIXES APPLIED:
#   Fix 1 — force parameter bypasses idempotency check entirely
#   Fix 2 — _already_ingested() respects force flag
#   Fix 3 — skipped status is distinct from success (zero rows loaded ≠ success)
#   Fix 6 — batch_id on skipped runs returns None so Silver can detect it
#   Fix 8 — reprocess mode clears ingestion log entry before re-ingesting
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
from utils.logger import (
    log_stage_failure,
    log_stage_start,
    log_stage_success,
    log_warning,
    logger,
)

_config = get_config()


# ==============================================================================
# Result dataclass
# ==============================================================================

@dataclass
class IngestionResult:
    """
    Summary of a single ingestion run.

    status values:
        success   — rows were loaded successfully
        partial   — some rows loaded, some rejected
        skipped   — file already ingested, no force flag (FIX 3)
        failed    — error during ingestion, no rows loaded
    """

    batch_id:         UUID | None       # FIX 6: None when skipped
    source_file:      str
    ef_rows_loaded:   int  = 0
    so_rows_loaded:   int  = 0
    ef_rows_rejected: int  = 0
    so_rows_rejected: int  = 0
    status:           str  = "pending"
    error_message:    str | None = None
    elapsed_seconds:  float = 0.0
    was_skipped:      bool = False      # FIX 3: explicit skipped flag

    @property
    def total_rows_loaded(self) -> int:
        return self.ef_rows_loaded + self.so_rows_loaded

    @property
    def total_rows_rejected(self) -> int:
        return self.ef_rows_rejected + self.so_rows_rejected

    @property
    def has_data(self) -> bool:
        """FIX 3: True only when rows were actually loaded — not just 'no crash'."""
        return self.total_rows_loaded > 0


# ==============================================================================
# Main ingestion class
# ==============================================================================

class BronzeLoader:
    """
    Loads raw NEUST enrollment Excel data into the Bronze PostgreSQL schema.

    Parameters:
        file_path   — path to the Excel file to ingest
        force       — FIX 1: if True, bypasses idempotency check entirely
        reprocess   — FIX 8: if True, clears the previous ingestion log
                      entry for this file before re-ingesting
    """

    def __init__(
        self,
        file_path:  str | Path,
        force:      bool = False,   # FIX 1
        reprocess:  bool = False,   # FIX 8
    ) -> None:
        self.file_path  = Path(file_path)
        self.force      = force
        self.reprocess  = reprocess
        self.batch_id   = uuid4()
        self._started   = time.monotonic()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> IngestionResult:
        """Execute the full ingestion pipeline for one Excel file."""
        result = IngestionResult(
            batch_id=self.batch_id,
            source_file=self.file_path.name,
        )

        log_stage_start("BRONZE", 1, f"Bronze ingestion — {self.file_path.name}")

        # FIX 1 + FIX 8 — log active mode clearly
        if self.force:
            log_warning(
                "FORCE mode ON — idempotency check bypassed.",
                stage="BRONZE",
            )
        if self.reprocess:
            log_warning(
                "REPROCESS mode ON — clearing previous ingestion log.",
                stage="BRONZE",
            )
            self._clear_ingestion_log()

        try:
            # Step 1 — validate schema
            logger.info("Step 1/5 — Validating file schema ...")
            validate_file(self.file_path)

            # Step 2 — idempotency check (FIX 1: skipped when force=True)
            logger.info("Step 2/5 — Checking for duplicate ingestion ...")
            if not self.force and self._already_ingested():
                logger.warning(
                    "File '{}' has already been ingested. Skipping. "
                    "Use --force to re-ingest or --reprocess to clear and re-run.",
                    self.file_path.name,
                )
                # FIX 3: use 'skipped' status — not 'success'
                result.status      = "skipped"
                result.was_skipped = True
                result.batch_id    = None   # FIX 6: signal to Silver that no new batch exists
                result.elapsed_seconds = time.monotonic() - self._started
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

            # FIX 3: status reflects actual data loaded
            if result.total_rows_loaded == 0:
                result.status = "failed"
                result.error_message = (
                    "Zero rows were loaded despite successful file read. "
                    "Check that the Excel file contains data rows."
                )
            elif result.total_rows_rejected == 0:
                result.status = "success"
            else:
                result.status = "partial"

        except ValueError as exc:
            result.status = "failed"
            result.error_message = str(exc)
            log_stage_failure("BRONZE", 1, "Bronze ingestion", exc)

        except Exception as exc:
            result.status = "failed"
            result.error_message = str(exc)
            logger.exception("Unexpected error during Bronze ingestion: {}", exc)
            log_stage_failure("BRONZE", 1, "Bronze ingestion", exc)

        finally:
            result.elapsed_seconds = time.monotonic() - self._started
            # Do not write log for skipped runs — log already exists
            if not result.was_skipped:
                self._write_ingestion_log(result)

        if result.status in ("success", "partial"):
            log_stage_success("BRONZE", 1, "Bronze ingestion", rows=result.total_rows_loaded)
            logger.info(
                f"[BRONZE] Ingestion complete — batch_id={result.batch_id} | "
                f"loaded={result.total_rows_loaded} | rejected={result.total_rows_rejected} | "
                f"{result.elapsed_seconds:.2f}s"
            )
        elif result.status == "failed":
            logger.error(
                f"[BRONZE] Ingestion FAILED — {self.file_path.name} | {result.error_message}"
            )

        return result

    # ------------------------------------------------------------------
    # FIX 2 — idempotency check (unchanged logic, force handled upstream)
    # ------------------------------------------------------------------

    def _already_ingested(self) -> bool:
        """
        Check if this filename has already been successfully ingested.
        Only called when force=False.
        """
        with get_session() as session:
            count = session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM bronze.ingestion_log
                    WHERE source_file = :fname
                      AND status IN ('success', 'partial')
                    """
                ),
                {"fname": self.file_path.name},
            ).scalar()
        return count > 0

    # ------------------------------------------------------------------
    # FIX 8 — reprocess: clear previous log entry for this file
    # ------------------------------------------------------------------

    def _clear_ingestion_log(self) -> None:
        """
        Delete ingestion log entries for this filename so the file
        can be re-ingested cleanly without touching actual data tables.

        This is safer than manually deleting Bronze table rows.
        The existing Bronze rows remain — new rows get a fresh batch_id.
        """
        try:
            with get_session() as session:
                deleted = session.execute(
                    text(
                        """
                        DELETE FROM bronze.ingestion_log
                        WHERE source_file = :fname
                        RETURNING id
                        """
                    ),
                    {"fname": self.file_path.name},
                ).rowcount
            logger.info(
                "Reprocess: cleared {} ingestion log entries for '{}'.",
                deleted, self.file_path.name,
            )
        except Exception as exc:
            logger.error("Failed to clear ingestion log for reprocess: {}", exc)

    # ------------------------------------------------------------------
    # Sheet reading
    # ------------------------------------------------------------------

    def _read_sheets(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        xl    = pd.ExcelFile(self.file_path, engine="openpyxl")
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
        df.columns = df.columns.str.strip()
        df = df.dropna(how="all").reset_index(drop=True)
        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].str.strip()
        return df

    # ------------------------------------------------------------------
    # Enrollment flow loader
    # ------------------------------------------------------------------

    def _load_enrollment_flow(self, df: pd.DataFrame) -> tuple[int, int]:
        rows_rejected = 0
        records       = []

        for idx, row in df.iterrows():
            try:
                records.append(BronzeEnrollmentFlow(
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
                ))
            except Exception as exc:
                rows_rejected += 1
                logger.warning("Row {} rejected from Enrollment_Flow: {}", idx, exc)

        rows_inserted  = self._batch_insert(records, "bronze.enrollment_flow")
        rows_rejected += len(records) - rows_inserted
        return rows_inserted, rows_rejected

    # ------------------------------------------------------------------
    # Student outcomes loader
    # ------------------------------------------------------------------

    def _load_student_outcomes(self, df: pd.DataFrame) -> tuple[int, int]:
        rows_rejected = 0
        records       = []

        for idx, row in df.iterrows():
            try:
                records.append(BronzeStudentOutcomes(
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
                ))
            except Exception as exc:
                rows_rejected += 1
                logger.warning("Row {} rejected from Student_Outcomes: {}", idx, exc)

        rows_inserted  = self._batch_insert(records, "bronze.student_outcomes")
        rows_rejected += len(records) - rows_inserted
        return rows_inserted, rows_rejected

    # ------------------------------------------------------------------
    # Batch insert
    # ------------------------------------------------------------------

    def _batch_insert(self, records: list, table_label: str) -> int:
        if not records:
            return 0

        batch_size    = _config.batch_size
        total_batches = (len(records) + batch_size - 1) // batch_size
        rows_inserted = 0

        for batch_num in range(total_batches):
            batch = records[batch_num * batch_size : (batch_num + 1) * batch_size]
            try:
                with get_session() as session:
                    session.bulk_save_objects(batch)
                rows_inserted += len(batch)
                logger.debug(
                    "Batch {}/{} inserted — {} rows into {}",
                    batch_num + 1, total_batches, len(batch), table_label,
                )
            except Exception as exc:
                logger.warning(
                    "Batch {}/{} failed for {} — retrying row by row. Error: {}",
                    batch_num + 1, total_batches, table_label, exc,
                )
                rows_inserted += self._row_by_row_insert(batch, table_label)

        return rows_inserted

    def _row_by_row_insert(self, records: list, table_label: str) -> int:
        inserted = 0
        for record in records:
            try:
                with get_session() as session:
                    session.add(record)
                inserted += 1
            except Exception as exc:
                logger.error("Row insert failed for {} — skipping. Error: {}", table_label, exc)
        return inserted

    # ------------------------------------------------------------------
    # Audit log writer
    # ------------------------------------------------------------------

    def _write_ingestion_log(self, result: IngestionResult) -> None:
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
            logger.error("Failed to write ingestion log: {}", exc)

    # ------------------------------------------------------------------
    # Type-safe helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_str(value) -> str | None:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        s = str(value).strip()
        return s if s else None

    @staticmethod
    def _safe_int(value) -> int | None:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        try:
            return int(float(str(value).replace(",", "").strip()))
        except (ValueError, TypeError):
            return None


# ==============================================================================
# Module-level runner — called by pipeline.py
# ==============================================================================

def run_bronze(
    file_path:  str | Path | None = None,
    force:      bool = False,   # FIX 1
    reprocess:  bool = False,   # FIX 8
) -> IngestionResult:
    """
    Entry point called by pipeline.py.

    Args:
        file_path  — specific Excel file to ingest (default: all in data/raw/)
        force      — bypass idempotency check, always re-ingest
        reprocess  — clear previous log entry then re-ingest (safer than force)
    """
    if file_path:
        loader = BronzeLoader(file_path, force=force, reprocess=reprocess)
        return loader.run()

    raw_path   = _config.raw_data_path
    xlsx_files = sorted(raw_path.glob("*.xlsx"))

    if not xlsx_files:
        logger.warning("No .xlsx files found in: {}", raw_path)
        return IngestionResult(batch_id=None, source_file="none", status="skipped", was_skipped=True)

    logger.info("Found {} Excel file(s) in {}", len(xlsx_files), raw_path)

    last_result = None
    for xlsx_file in xlsx_files:
        loader      = BronzeLoader(xlsx_file, force=force, reprocess=reprocess)
        last_result = loader.run()

    return last_result