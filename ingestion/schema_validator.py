# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# ingestion/schema_validator.py
# Validates uploaded Excel files before any data touches the database
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from utils.logger import logger


# ==============================================================================
# Expected schemas — exact column names from the NEUST Excel template
# ==============================================================================

ENROLLMENT_FLOW_COLUMNS: list[str] = [
    "Academic Year",
    "Semester",
    "College/Department",
    "Program/Course",
    "Major",
    "Year Level",
    "Gender",
    "Applicants",
    "Accepted Applicants",
    "Total Enrolled",
    "New Students",
    "Transferees",
    "Returnees",
]

STUDENT_OUTCOMES_COLUMNS: list[str] = [
    "Academic Year",
    "Semester",
    "College/Department",
    "Program/Course",
    "Major",
    "Year Level",
    "Gender",
    "Graduates",
    "Dropouts",
    "Shifters Out",
    "Shifters In",
]

# Columns that must never be completely empty
ENROLLMENT_FLOW_REQUIRED: list[str] = [
    "Academic Year",
    "Semester",
    "College/Department",
    "Program/Course",
    "Year Level",
    "Total Enrolled",
]

STUDENT_OUTCOMES_REQUIRED: list[str] = [
    "Academic Year",
    "Semester",
    "College/Department",
    "Program/Course",
    "Year Level",
]

# Columns expected to contain numeric values
ENROLLMENT_FLOW_NUMERIC: list[str] = [
    "Applicants",
    "Accepted Applicants",
    "Total Enrolled",
    "New Students",
    "Transferees",
    "Returnees",
]

STUDENT_OUTCOMES_NUMERIC: list[str] = [
    "Graduates",
    "Dropouts",
    "Shifters Out",
    "Shifters In",
]

# Sheet names in the Excel workbook
SHEET_ENROLLMENT_FLOW   = "Enrollment_Flow"
SHEET_STUDENT_OUTCOMES  = "Student_Outcomes"


# ==============================================================================
# Validation result
# ==============================================================================

@dataclass
class ValidationResult:
    """
    Holds the outcome of a file validation run.

    is_valid    — True only when zero errors are found
    errors      — blocking issues that prevent ingestion
    warnings    — non-blocking issues logged but not fatal
    sheet_name  — which sheet was validated
    row_count   — number of data rows found (excluding header)
    """

    sheet_name: str
    is_valid:   bool = True
    errors:     list[str] = field(default_factory=list)
    warnings:   list[str] = field(default_factory=list)
    row_count:  int = 0

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.is_valid = False

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def log_summary(self) -> None:
        status = "PASSED" if self.is_valid else "FAILED"
        logger.info(
            "Validation [{}] {} — {} rows | {} errors | {} warnings",
            self.sheet_name, status,
            self.row_count, len(self.errors), len(self.warnings),
        )
        for err in self.errors:
            logger.error("  ✗ {}", err)
        for warn in self.warnings:
            logger.warning("  ⚠ {}", warn)


# ==============================================================================
# Core validator
# ==============================================================================

class SchemaValidator:
    """
    Validates an Excel file against the expected NEUST enrollment template.

    Checks performed (in order):
        1. File exists and is readable
        2. Required sheets are present
        3. All expected columns are present (exact name match)
        4. No extra/renamed columns that indicate a wrong file
        5. Required columns have no fully-empty data
        6. Numeric columns contain only numeric or null values
        7. Row count is non-zero
        8. No completely duplicate rows
        9. Academic Year and Semester columns have recognizable formats

    Usage:
        validator = SchemaValidator("data/raw/enrollment_flow_AY2024.xlsx")
        ef_result, so_result = validator.validate()
        if not ef_result.is_valid or not so_result.is_valid:
            raise ValueError("File failed validation — check logs")
    """

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)
        self._ef_df: pd.DataFrame | None = None
        self._so_df: pd.DataFrame | None = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def validate(self) -> tuple[ValidationResult, ValidationResult]:
        """
        Run full validation on both sheets.

        Returns a tuple of (enrollment_flow_result, student_outcomes_result).
        Both results must be is_valid=True for ingestion to proceed.
        """
        logger.info("Starting schema validation for: {}", self.file_path.name)

        ef_result = ValidationResult(sheet_name=SHEET_ENROLLMENT_FLOW)
        so_result = ValidationResult(sheet_name=SHEET_STUDENT_OUTCOMES)

        # Step 1 — file existence
        if not self._check_file_exists(ef_result):
            so_result.add_error(f"File not found: {self.file_path}")
            return ef_result, so_result

        # Step 2 — load sheets
        sheets = self._load_sheets(ef_result, so_result)
        if sheets is None:
            return ef_result, so_result

        self._ef_df, self._so_df = sheets

        # Step 3–9 — validate each sheet
        self._validate_sheet(
            df=self._ef_df,
            result=ef_result,
            expected_columns=ENROLLMENT_FLOW_COLUMNS,
            required_columns=ENROLLMENT_FLOW_REQUIRED,
            numeric_columns=ENROLLMENT_FLOW_NUMERIC,
        )
        self._validate_sheet(
            df=self._so_df,
            result=so_result,
            expected_columns=STUDENT_OUTCOMES_COLUMNS,
            required_columns=STUDENT_OUTCOMES_REQUIRED,
            numeric_columns=STUDENT_OUTCOMES_NUMERIC,
        )

        ef_result.log_summary()
        so_result.log_summary()

        return ef_result, so_result

    # ------------------------------------------------------------------
    # Step 1 — file existence
    # ------------------------------------------------------------------

    def _check_file_exists(self, result: ValidationResult) -> bool:
        if not self.file_path.exists():
            result.add_error(f"File not found: {self.file_path}")
            return False
        if not self.file_path.suffix.lower() in (".xlsx", ".xls"):
            result.add_error(
                f"Unsupported file type: {self.file_path.suffix!r} — "
                "expected .xlsx or .xls"
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Step 2 — load both sheets
    # ------------------------------------------------------------------

    def _load_sheets(
        self,
        ef_result: ValidationResult,
        so_result: ValidationResult,
    ) -> tuple[pd.DataFrame, pd.DataFrame] | None:
        try:
            xl = pd.ExcelFile(self.file_path, engine="openpyxl")
        except Exception as exc:
            ef_result.add_error(f"Cannot open file: {exc}")
            so_result.add_error(f"Cannot open file: {exc}")
            return None

        available_sheets = xl.sheet_names

        ef_df = self._load_single_sheet(xl, SHEET_ENROLLMENT_FLOW,  ef_result, available_sheets)
        so_df = self._load_single_sheet(xl, SHEET_STUDENT_OUTCOMES, so_result, available_sheets)

        if ef_df is None or so_df is None:
            return None

        return ef_df, so_df

    def _load_single_sheet(
        self,
        xl: pd.ExcelFile,
        sheet_name: str,
        result: ValidationResult,
        available_sheets: list[str],
    ) -> pd.DataFrame | None:
        if sheet_name not in available_sheets:
            result.add_error(
                f"Sheet '{sheet_name}' not found. "
                f"Available sheets: {available_sheets}"
            )
            return None

        try:
            df = xl.parse(sheet_name, dtype=str)   # read everything as string initially
            # Drop fully empty rows
            df = df.dropna(how="all").reset_index(drop=True)
            return df
        except Exception as exc:
            result.add_error(f"Cannot parse sheet '{sheet_name}': {exc}")
            return None

    # ------------------------------------------------------------------
    # Steps 3–9 — sheet-level validation
    # ------------------------------------------------------------------

    def _validate_sheet(
        self,
        df: pd.DataFrame,
        result: ValidationResult,
        expected_columns: list[str],
        required_columns: list[str],
        numeric_columns: list[str],
    ) -> None:
        result.row_count = len(df)

        self._check_columns(df, result, expected_columns)
        self._check_row_count(df, result)

        if not result.is_valid:
            # If columns are wrong or no rows, skip further checks
            return

        self._check_required_columns(df, result, required_columns)
        self._check_numeric_columns(df, result, numeric_columns)
        self._check_duplicates(df, result)
        self._check_academic_year_format(df, result)
        self._check_negative_values(df, result, numeric_columns)

    def _check_columns(
        self,
        df: pd.DataFrame,
        result: ValidationResult,
        expected: list[str],
    ) -> None:
        actual   = set(df.columns.str.strip())
        expected_set = set(expected)

        missing = expected_set - actual
        extra   = actual - expected_set

        if missing:
            result.add_error(
                f"Missing columns: {sorted(missing)}. "
                "Check that the correct Excel template is being uploaded."
            )
        if extra:
            result.add_warning(
                f"Unexpected extra columns found (will be ignored): {sorted(extra)}"
            )

    def _check_row_count(self, df: pd.DataFrame, result: ValidationResult) -> None:
        if len(df) == 0:
            result.add_error(
                "Sheet contains no data rows. "
                "The file may be an empty template."
            )
        elif len(df) < 3:
            result.add_warning(
                f"Only {len(df)} data row(s) found — unusually low. "
                "Verify this is not a partial export."
            )

    def _check_required_columns(
        self,
        df: pd.DataFrame,
        result: ValidationResult,
        required: list[str],
    ) -> None:
        for col in required:
            if col not in df.columns:
                continue
            null_count = df[col].isna().sum() + (df[col].str.strip() == "").sum()
            null_pct   = (null_count / len(df)) * 100
            if null_count == len(df):
                result.add_error(f"Required column '{col}' is entirely empty.")
            elif null_pct > 20:
                result.add_warning(
                    f"Column '{col}' has {null_count} empty values "
                    f"({null_pct:.1f}% of rows)."
                )

    def _check_numeric_columns(
        self,
        df: pd.DataFrame,
        result: ValidationResult,
        numeric_cols: list[str],
    ) -> None:
        for col in numeric_cols:
            if col not in df.columns:
                continue
            non_null = df[col].dropna()
            non_null = non_null[non_null.str.strip() != ""]
            if non_null.empty:
                result.add_warning(f"Numeric column '{col}' has no values.")
                continue

            def _is_numeric(val: str) -> bool:
                try:
                    float(str(val).replace(",", "").strip())
                    return True
                except ValueError:
                    return False

            bad = non_null[~non_null.apply(_is_numeric)]
            if not bad.empty:
                sample = bad.head(3).tolist()
                result.add_error(
                    f"Column '{col}' contains {len(bad)} non-numeric value(s). "
                    f"Sample: {sample}"
                )

    def _check_duplicates(self, df: pd.DataFrame, result: ValidationResult) -> None:
        dup_count = df.duplicated().sum()
        if dup_count > 0:
            result.add_warning(
                f"{dup_count} completely duplicate row(s) found. "
                "They will be deduplicated during ingestion."
            )

    def _check_academic_year_format(
        self, df: pd.DataFrame, result: ValidationResult
    ) -> None:
        if "Academic Year" not in df.columns:
            return

        import re
        pattern = re.compile(
            r"(?:AY|SY|A\.Y\.|S\.Y\.)?\s*\d{4}\s*[-–—/]\s*\d{2,4}",
            re.IGNORECASE,
        )
        sample = df["Academic Year"].dropna().head(20)
        unrecognized = [v for v in sample if not pattern.search(str(v))]
        if unrecognized:
            result.add_warning(
                f"Some 'Academic Year' values may not parse correctly: "
                f"{unrecognized[:3]}. "
                "Expected format: '2023-2024' or 'AY 2023-2024'."
            )

    def _check_negative_values(
        self,
        df: pd.DataFrame,
        result: ValidationResult,
        numeric_cols: list[str],
    ) -> None:
        for col in numeric_cols:
            if col not in df.columns:
                continue
            try:
                numeric_series = pd.to_numeric(
                    df[col].str.replace(",", "", regex=False),
                    errors="coerce",
                )
                neg_count = (numeric_series < 0).sum()
                if neg_count > 0:
                    result.add_error(
                        f"Column '{col}' has {neg_count} negative value(s). "
                        "Enrollment counts cannot be negative."
                    )
            except Exception:
                pass   # already flagged by _check_numeric_columns


# ==============================================================================
# Convenience function used by load_bronze.py
# ==============================================================================

def validate_file(file_path: str | Path) -> tuple[ValidationResult, ValidationResult]:
    """
    Validate an Excel file and return (ef_result, so_result).

    Raises ValueError if either sheet fails validation.
    Call this at the top of load_bronze.py before reading any data.

    Usage:
        from ingestion.schema_validator import validate_file
        ef_result, so_result = validate_file("data/raw/enrollment_AY2024.xlsx")
    """
    validator = SchemaValidator(file_path)
    ef_result, so_result = validator.validate()

    if not ef_result.is_valid or not so_result.is_valid:
        raise ValueError(
            f"File '{Path(file_path).name}' failed schema validation. "
            "Check the logs above for details."
        )

    return ef_result, so_result