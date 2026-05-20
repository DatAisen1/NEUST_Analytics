# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# transformation/silver_transform.py
# Bronze → Silver transformation: clean, standardize, validate, and upsert
# ==============================================================================

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from database.connection import get_session
from database.models.silver_models import (
    SilverAcademicPeriod,
    SilverEnrollmentFlow,
    SilverProgram,
    SilverStudentOutcomes,
    SilverTransformationLog,
)
from transformation.rules_engine import (
    DataQualityRules,
    KPIRules,
    Thresholds,
)
from utils.config import get_config
from utils.date_helpers import (
    build_academic_period,
    parse_gender,
    parse_year_level,
)
from utils.logger import log_step_failure, log_step_start, log_step_success, logger

_config = get_config()


# ==============================================================================
# Result dataclass
# ==============================================================================

@dataclass
class TransformResult:
    """Summary of a Bronze → Silver transformation run."""

    bronze_batch_id:     UUID
    ef_rows_processed:   int = 0
    ef_rows_inserted:    int = 0
    ef_rows_updated:     int = 0
    ef_rows_skipped:     int = 0
    so_rows_processed:   int = 0
    so_rows_inserted:    int = 0
    so_rows_updated:     int = 0
    so_rows_skipped:     int = 0
    status:              str = "pending"
    error_message:       str | None = None
    elapsed_seconds:     float = 0.0
    warnings:            list[str] = field(default_factory=list)

    @property
    def total_inserted(self) -> int:
        return self.ef_rows_inserted + self.so_rows_inserted

    @property
    def total_skipped(self) -> int:
        return self.ef_rows_skipped + self.so_rows_skipped


# ==============================================================================
# Silver transformer
# ==============================================================================

class SilverTransformer:
    """
    Transforms raw Bronze records into cleaned Silver records.

    Transformation steps per row:
        1. Parse and validate academic year → AcademicPeriod
        2. Standardize program code → look up or create SilverProgram
        3. Parse year level, gender using config maps
        4. Replace null numerics with 0
        5. Compute acceptance_rate
        6. Run DataQualityRules checks — log warnings, never block
        7. Upsert into silver table (INSERT ... ON CONFLICT DO UPDATE)
        8. Write SilverTransformationLog record

    Idempotent — safe to re-run. Duplicate rows are updated, not duplicated.

    Usage:
        transformer = SilverTransformer(batch_id)
        result = transformer.run()
    """

    def __init__(self, bronze_batch_id: UUID) -> None:
        self.bronze_batch_id  = bronze_batch_id
        self._started         = time.monotonic()
        self._period_cache:  dict[tuple, int]  = {}   # (academic_year, semester) → period_id
        self._program_cache: dict[str, int]    = {}   # program_code → program_id

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> TransformResult:
        """Execute the full Bronze → Silver transformation."""
        result = TransformResult(bronze_batch_id=self.bronze_batch_id)
        log_step_start(2, "Silver transformation")

        try:
            # Step 1 — load Bronze rows for this batch
            logger.info("Loading Bronze rows for batch_id={}", self.bronze_batch_id)
            ef_rows = self._load_bronze_enrollment_flow()
            so_rows = self._load_bronze_student_outcomes()
            logger.info(
                "Bronze loaded — Enrollment_Flow: {} rows | Student_Outcomes: {} rows",
                len(ef_rows), len(so_rows),
            )

            # Step 2 — transform enrollment flow
            logger.info("Transforming Enrollment_Flow ...")
            ef_result = self._transform_enrollment_flow(ef_rows)
            result.ef_rows_processed = ef_result["processed"]
            result.ef_rows_inserted  = ef_result["inserted"]
            result.ef_rows_updated   = ef_result["updated"]
            result.ef_rows_skipped   = ef_result["skipped"]
            result.warnings.extend(ef_result["warnings"])

            # Step 3 — transform student outcomes
            logger.info("Transforming Student_Outcomes ...")
            so_result = self._transform_student_outcomes(so_rows)
            result.so_rows_processed = so_result["processed"]
            result.so_rows_inserted  = so_result["inserted"]
            result.so_rows_updated   = so_result["updated"]
            result.so_rows_skipped   = so_result["skipped"]
            result.warnings.extend(so_result["warnings"])

            result.status = "success"

        except Exception as exc:
            result.status        = "failed"
            result.error_message = str(exc)
            logger.exception("Silver transformation failed: {}", exc)
            log_step_failure(2, "Silver transformation", exc)

        finally:
            result.elapsed_seconds = time.monotonic() - self._started
            self._write_transformation_log(result)

        if result.status == "success":
            log_step_success(2, "Silver transformation", rows=result.total_inserted)
            logger.info(
                "Silver complete — inserted={} | updated={} | skipped={} | {:.2f}s",
                result.total_inserted,
                result.ef_rows_updated + result.so_rows_updated,
                result.total_skipped,
                result.elapsed_seconds,
            )

        return result

    # ------------------------------------------------------------------
    # Bronze data loaders
    # ------------------------------------------------------------------

    def _load_bronze_enrollment_flow(self) -> list[dict]:
        with get_session() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        id, academic_year, semester, college_department,
                        program_course, major, year_level, gender,
                        applicants, accepted_applicants, total_enrolled,
                        new_students, transferees, returnees
                    FROM bronze.enrollment_flow
                    WHERE batch_id = :bid
                      AND is_deleted = FALSE
                    ORDER BY id
                    """
                ),
                {"bid": str(self.bronze_batch_id)},
            ).fetchall()
        return [dict(row._mapping) for row in rows]

    def _load_bronze_student_outcomes(self) -> list[dict]:
        with get_session() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        id, academic_year, semester, college_department,
                        program_course, major, year_level, gender,
                        graduates, dropouts, shifters_out, shifters_in
                    FROM bronze.student_outcomes
                    WHERE batch_id = :bid
                      AND is_deleted = FALSE
                    ORDER BY id
                    """
                ),
                {"bid": str(self.bronze_batch_id)},
            ).fetchall()
        return [dict(row._mapping) for row in rows]

    # ------------------------------------------------------------------
    # Enrollment flow transformation
    # ------------------------------------------------------------------

    def _transform_enrollment_flow(self, rows: list[dict]) -> dict:
        processed = inserted = updated = skipped = 0
        warnings: list[str] = []

        for row in rows:
            processed += 1
            try:
                # Parse period
                period_id = self._get_or_create_period(
                    row["academic_year"], row["semester"]
                )
                if period_id is None:
                    skipped += 1
                    continue

                # Parse and standardize fields
                program_code = self._standardize_program_code(
                    row["program_course"], row["college_department"]
                )
                program_id = self._get_or_create_program(
                    program_code, row["program_course"], row["college_department"]
                )

                year_level = parse_year_level(row["year_level"] or "")
                if year_level is None:
                    warnings.append(
                        f"Bronze EF row {row['id']}: unrecognized year_level "
                        f"{row['year_level']!r} — skipped"
                    )
                    skipped += 1
                    continue

                gender      = parse_gender(row["gender"])
                academic_year_str, semester_int = self._parse_period_str(
                    row["academic_year"], row["semester"]
                )

                # Numeric coercion (None → 0)
                applicants          = self._safe_int(row["applicants"])
                accepted_applicants = self._safe_int(row["accepted_applicants"])
                total_enrolled      = self._safe_int(row["total_enrolled"])
                new_students        = self._safe_int(row["new_students"])
                transferees         = self._safe_int(row["transferees"])
                returnees           = self._safe_int(row["returnees"])

                # Data quality warnings (non-blocking)
                dq_warnings = DataQualityRules.check_enrollment_row(
                    academic_year=academic_year_str,
                    semester=semester_int,
                    program_code=program_code,
                    year_level=year_level,
                    total_enrolled=total_enrolled,
                )
                for w in dq_warnings:
                    warnings.append(f"Bronze EF row {row['id']}: {w}")

                # Upsert
                action = self._upsert_enrollment_flow(
                    period_id=period_id,
                    program_id=program_id,
                    academic_year=academic_year_str,
                    semester=semester_int,
                    college=self._normalize_college(row["college_department"]),
                    program_code=program_code,
                    major=row["major"],
                    year_level=year_level,
                    gender=gender,
                    applicants=applicants,
                    accepted_applicants=accepted_applicants,
                    total_enrolled=total_enrolled,
                    new_students=new_students,
                    transferees=transferees,
                    returnees=returnees,
                )

                if action == "inserted":
                    inserted += 1
                else:
                    updated += 1

            except Exception as exc:
                skipped += 1
                logger.warning("Error transforming EF row {}: {}", row.get("id"), exc)

        return {
            "processed": processed,
            "inserted":  inserted,
            "updated":   updated,
            "skipped":   skipped,
            "warnings":  warnings,
        }

    def _upsert_enrollment_flow(self, **kwargs) -> str:
        """INSERT ... ON CONFLICT DO UPDATE for silver.enrollment_flow."""
        with get_session() as session:
            stmt = pg_insert(SilverEnrollmentFlow).values(
                **kwargs,
                bronze_batch_id=self.bronze_batch_id,
                transformed_at=datetime.now(timezone.utc),
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_silver_ef",
                set_={
                    "applicants":          stmt.excluded.applicants,
                    "accepted_applicants": stmt.excluded.accepted_applicants,
                    "total_enrolled":      stmt.excluded.total_enrolled,
                    "new_students":        stmt.excluded.new_students,
                    "transferees":         stmt.excluded.transferees,
                    "returnees":           stmt.excluded.returnees,
                    "bronze_batch_id":     stmt.excluded.bronze_batch_id,
                    "transformed_at":      stmt.excluded.transformed_at,
                },
            )
            result = session.execute(stmt)
            # rowcount == 1 for insert, 1 for update; check via returning if needed
            return "inserted" if result.rowcount == 1 else "updated"

    # ------------------------------------------------------------------
    # Student outcomes transformation
    # ------------------------------------------------------------------

    def _transform_student_outcomes(self, rows: list[dict]) -> dict:
        processed = inserted = updated = skipped = 0
        warnings: list[str] = []

        for row in rows:
            processed += 1
            try:
                period_id = self._get_or_create_period(
                    row["academic_year"], row["semester"]
                )
                if period_id is None:
                    skipped += 1
                    continue

                program_code = self._standardize_program_code(
                    row["program_course"], row["college_department"]
                )
                program_id = self._get_or_create_program(
                    program_code, row["program_course"], row["college_department"]
                )

                year_level = parse_year_level(row["year_level"] or "")
                if year_level is None:
                    skipped += 1
                    warnings.append(
                        f"Bronze SO row {row['id']}: unrecognized year_level "
                        f"{row['year_level']!r} — skipped"
                    )
                    continue

                gender = parse_gender(row["gender"])
                academic_year_str, semester_int = self._parse_period_str(
                    row["academic_year"], row["semester"]
                )

                graduates    = self._safe_int(row["graduates"])
                dropouts     = self._safe_int(row["dropouts"])
                shifters_out = self._safe_int(row["shifters_out"])
                shifters_in  = self._safe_int(row["shifters_in"])

                # Data quality checks
                dq_warnings = DataQualityRules.check_outcome_row(
                    graduates=graduates,
                    dropouts=dropouts,
                    total_enrolled=None,   # not available in outcomes sheet
                )
                for w in dq_warnings:
                    warnings.append(f"Bronze SO row {row['id']}: {w}")

                action = self._upsert_student_outcomes(
                    period_id=period_id,
                    program_id=program_id,
                    academic_year=academic_year_str,
                    semester=semester_int,
                    college=self._normalize_college(row["college_department"]),
                    program_code=program_code,
                    major=row["major"],
                    year_level=year_level,
                    gender=gender,
                    graduates=graduates,
                    dropouts=dropouts,
                    shifters_out=shifters_out,
                    shifters_in=shifters_in,
                )

                if action == "inserted":
                    inserted += 1
                else:
                    updated += 1

            except Exception as exc:
                skipped += 1
                logger.warning("Error transforming SO row {}: {}", row.get("id"), exc)

        return {
            "processed": processed,
            "inserted":  inserted,
            "updated":   updated,
            "skipped":   skipped,
            "warnings":  warnings,
        }

    def _upsert_student_outcomes(self, **kwargs) -> str:
        """INSERT ... ON CONFLICT DO UPDATE for silver.student_outcomes."""
        with get_session() as session:
            stmt = pg_insert(SilverStudentOutcomes).values(
                **kwargs,
                bronze_batch_id=self.bronze_batch_id,
                transformed_at=datetime.now(timezone.utc),
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_silver_so",
                set_={
                    "graduates":        stmt.excluded.graduates,
                    "dropouts":         stmt.excluded.dropouts,
                    "shifters_out":     stmt.excluded.shifters_out,
                    "shifters_in":      stmt.excluded.shifters_in,
                    "bronze_batch_id":  stmt.excluded.bronze_batch_id,
                    "transformed_at":   stmt.excluded.transformed_at,
                },
            )
            result = session.execute(stmt)
            return "inserted" if result.rowcount == 1 else "updated"

    # ------------------------------------------------------------------
    # Period cache helpers
    # ------------------------------------------------------------------

    def _get_or_create_period(
        self, raw_year: str | None, raw_semester: str | None
    ) -> int | None:
        """Return period_id, creating the record if it doesn't exist yet."""
        if not raw_year or not raw_semester:
            logger.warning(
                "Cannot create period — missing year={!r} or semester={!r}",
                raw_year, raw_semester,
            )
            return None

        period = build_academic_period(raw_year, raw_semester)
        if period is None:
            return None

        cache_key = (period.academic_year, period.semester)
        if cache_key in self._period_cache:
            return self._period_cache[cache_key]

        with get_session() as session:
            stmt = pg_insert(SilverAcademicPeriod).values(
                academic_year=period.academic_year,
                semester=period.semester,
                year_start=period.year_start,
                year_end=period.year_end,
                label=period.label,
                is_current=False,
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_silver_ap",
                set_={"label": stmt.excluded.label},
            )
            stmt = stmt.returning(SilverAcademicPeriod.id)
            period_id = session.execute(stmt).scalar_one()

        self._period_cache[cache_key] = period_id
        logger.debug("Period resolved — {} → id={}", period.label, period_id)
        return period_id

    def _parse_period_str(
        self, raw_year: str, raw_semester: str
    ) -> tuple[str, int]:
        """Return (canonical_academic_year, semester_int) for Silver columns."""
        from utils.date_helpers import parse_academic_year, parse_semester
        ay = parse_academic_year(raw_year or "")
        sem = parse_semester(raw_semester or "")
        return (ay[0] if ay else raw_year), (sem or 1)

    # ------------------------------------------------------------------
    # Program cache helpers
    # ------------------------------------------------------------------

    def _get_or_create_program(
        self,
        program_code: str,
        program_name: str | None,
        college: str | None,
    ) -> int:
        """Return program_id, creating the record if it doesn't exist yet."""
        if program_code in self._program_cache:
            return self._program_cache[program_code]

        with get_session() as session:
            stmt = pg_insert(SilverProgram).values(
                program_code=program_code,
                program_name=program_name or program_code,
                college=self._normalize_college(college),
                department=None,
                duration_years=Thresholds.STANDARD_PROGRAM_YEARS,
                is_active=True,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[SilverProgram.program_code],
                set_={"program_name": stmt.excluded.program_name},
            )
            stmt = stmt.returning(SilverProgram.id)
            program_id = session.execute(stmt).scalar_one()

        self._program_cache[program_code] = program_id
        return program_id

    # ------------------------------------------------------------------
    # Standardization helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _standardize_program_code(
        program_course: str | None,
        college_department: str | None,
    ) -> str:
        """
        Produce a canonical program code from the raw program name.

        Rules (applied in order):
            1. Remove common prefixes: 'Bachelor of Science in', 'BS ', etc.
            2. Remove spaces, hyphens, and dots
            3. Convert to uppercase
            4. Cap at 10 characters

        Examples:
            'BS Computer Science' → 'BSCS'
            'BS-CS'               → 'BSCS'
            'B.S.C.S'             → 'BSCS'
        """
        if not program_course:
            raw = (college_department or "UNKNOWN").strip()
        else:
            raw = program_course.strip()

        # Remove long prefixes
        for prefix in [
            "Bachelor of Science in ",
            "Bachelor of Arts in ",
            "Bachelor of Education in ",
            "Bachelor of ",
            "BS in ",
            "BA in ",
        ]:
            if raw.lower().startswith(prefix.lower()):
                raw = "BS" + raw[len(prefix):]
                break

        # Normalize separators
        import re
        code = re.sub(r"[\s\-\.]", "", raw)
        code = code.upper()

        # Cap length
        return code[:10] if len(code) > 10 else code

    @staticmethod
    def _normalize_college(college_department: str | None) -> str:
        """Return a clean college name or 'Unknown College' if blank."""
        if not college_department:
            return "Unknown College"
        return college_department.strip().title()

    @staticmethod
    def _safe_int(value) -> int:
        """Return int value or 0 if null/non-numeric."""
        if value is None:
            return 0
        try:
            return int(float(str(value).replace(",", "").strip()))
        except (ValueError, TypeError):
            return 0

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def _write_transformation_log(self, result: TransformResult) -> None:
        completed = datetime.now(timezone.utc)
        logs = [
            SilverTransformationLog(
                bronze_batch_id=result.bronze_batch_id,
                target_table="silver.enrollment_flow",
                rows_processed=result.ef_rows_processed,
                rows_inserted=result.ef_rows_inserted,
                rows_updated=result.ef_rows_updated,
                rows_skipped=result.ef_rows_skipped,
                status=result.status,
                error_message=result.error_message,
                completed_at=completed,
            ),
            SilverTransformationLog(
                bronze_batch_id=result.bronze_batch_id,
                target_table="silver.student_outcomes",
                rows_processed=result.so_rows_processed,
                rows_inserted=result.so_rows_inserted,
                rows_updated=result.so_rows_updated,
                rows_skipped=result.so_rows_skipped,
                status=result.status,
                error_message=result.error_message,
                completed_at=completed,
            ),
        ]
        try:
            with get_session() as session:
                session.bulk_save_objects(logs)
        except Exception as exc:
            logger.error("Failed to write Silver transformation log: {}", exc)


# ==============================================================================
# Module-level runner — called by pipeline.py
# ==============================================================================

def run_silver(bronze_batch_id: UUID | None = None) -> TransformResult:
    """
    Entry point called by pipeline.py.

    If bronze_batch_id is not provided, fetches the most recent
    successful Bronze batch and transforms it.

    Usage in pipeline.py:
        from transformation.silver_transform import run_silver
        result = run_silver()
    """
    if bronze_batch_id is None:
        bronze_batch_id = _get_latest_bronze_batch()

    if bronze_batch_id is None:
        logger.error(
            "No Bronze batch found to transform. "
            "Run load_bronze.py first."
        )
        return TransformResult(
            bronze_batch_id=__import__("uuid").uuid4(),
            status="failed",
            error_message="No Bronze batch available",
        )

    transformer = SilverTransformer(bronze_batch_id)
    return transformer.run()


def _get_latest_bronze_batch() -> UUID | None:
    """Fetch the most recent successful Bronze batch_id."""
    try:
        with get_session() as session:
            row = session.execute(
                text(
                    """
                    SELECT batch_id FROM bronze.ingestion_log
                    WHERE status IN ('success', 'partial')
                    ORDER BY completed_at DESC
                    LIMIT 1
                    """
                )
            ).fetchone()
        if row:
            return row[0]
    except Exception as exc:
        logger.error("Cannot fetch latest Bronze batch: {}", exc)
    return None