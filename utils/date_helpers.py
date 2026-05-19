# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# utils/date_helpers.py
# Academic year and semester parsing, validation, and formatting utilities
# ==============================================================================

from __future__ import annotations

import re
from dataclasses import dataclass

from utils.config import get_config
from utils.logger import logger

_config = get_config()


# ==============================================================================
# Data classes
# ==============================================================================

@dataclass(frozen=True)
class AcademicPeriod:
    """
    Represents a single academic year + semester combination.

    Attributes:
        academic_year   — canonical string e.g. '2023-2024'
        semester        — integer 1, 2, or 3
        year_start      — integer e.g. 2023
        year_end        — integer e.g. 2024
        label           — human-readable e.g. 'AY 2023-2024 1st Semester'
        sort_key        — integer for chronological ordering (year_start * 10 + semester)
    """

    academic_year: str
    semester:      int
    year_start:    int
    year_end:      int
    label:         str
    sort_key:      int


# ==============================================================================
# Academic year parsing
# ==============================================================================

# Patterns that match common academic year formats from the Excel source:
#   2023-2024  |  2023-24  |  AY 2023-2024  |  SY 2023-2024
_AY_PATTERN = re.compile(
    r"(?:AY|SY|A\.Y\.|S\.Y\.)?\s*"     # optional prefix
    r"(\d{4})"                           # 4-digit start year
    r"\s*[-–—/]\s*"                      # separator (hyphen, en-dash, em-dash, slash)
    r"(\d{2,4})",                        # 2 or 4-digit end year
    re.IGNORECASE,
)


def parse_academic_year(raw: str) -> tuple[str, int, int] | None:
    """
    Parse a raw academic year string into a canonical (academic_year, year_start, year_end) tuple.

    Handles formats:
        '2023-2024'     → ('2023-2024', 2023, 2024)
        'AY 2023-2024'  → ('2023-2024', 2023, 2024)
        'SY 2023-24'    → ('2023-2024', 2023, 2024)
        '2023/2024'     → ('2023-2024', 2023, 2024)

    Returns None and logs a warning if parsing fails.
    """
    if not raw or not isinstance(raw, str):
        logger.warning("parse_academic_year received empty or non-string value: {!r}", raw)
        return None

    match = _AY_PATTERN.search(raw.strip())
    if not match:
        logger.warning("Cannot parse academic year from: {!r}", raw)
        return None

    year_start = int(match.group(1))
    end_raw    = match.group(2)

    # Expand 2-digit end year: '23' → 2023, '24' → 2024
    if len(end_raw) == 2:
        century    = (year_start // 100) * 100
        year_end   = century + int(end_raw)
    else:
        year_end = int(end_raw)

    # Sanity check
    if year_end != year_start + 1:
        logger.warning(
            "Academic year span is not exactly 1 year: {!r} → {}-{}",
            raw, year_start, year_end,
        )

    academic_year = f"{year_start}-{year_end}"
    return academic_year, year_start, year_end


# ==============================================================================
# Semester parsing
# ==============================================================================

def parse_semester(raw: str) -> int | None:
    """
    Map a raw semester string to a canonical integer (1, 2, or 3).

    Uses the semester_map defined in config.py which covers all
    known variants from the NEUST Excel exports.

    Returns None and logs a warning if the value is unrecognized.
    """
    if not raw or not isinstance(raw, str):
        logger.warning("parse_semester received empty or non-string value: {!r}", raw)
        return None

    key = raw.strip().lower()
    result = _config.semester_map.get(key)

    if result is None:
        logger.warning("Unrecognized semester value: {!r} — add it to semester_map in config.py", raw)

    return result


def semester_to_label(semester: int) -> str:
    """
    Convert a semester integer to a human-readable label.

        1 → '1st Semester'
        2 → '2nd Semester'
        3 → 'Summer'
    """
    labels = {1: "1st Semester", 2: "2nd Semester", 3: "Summer"}
    return labels.get(semester, f"Semester {semester}")


# ==============================================================================
# Year level parsing
# ==============================================================================

def parse_year_level(raw: str) -> int | None:
    """
    Map a raw year level string to a canonical integer (1–6).

    Uses the year_level_map from config.py.
    Returns None if value is unrecognized.
    """
    if not raw or not isinstance(raw, str):
        logger.warning("parse_year_level received empty or non-string: {!r}", raw)
        return None

    key = raw.strip().lower()
    result = _config.year_level_map.get(key)

    if result is None:
        logger.warning("Unrecognized year level value: {!r} — add it to year_level_map in config.py", raw)

    return result


# ==============================================================================
# Gender parsing
# ==============================================================================

def parse_gender(raw: str | None) -> str:
    """
    Map a raw gender string to a canonical value.

    Returns 'Not Specified' for null, empty, or unrecognized values.
    Never raises — always returns a safe default.
    """
    if not raw or not isinstance(raw, str):
        return "Not Specified"

    key = raw.strip().lower()
    return _config.gender_map.get(key, "Not Specified")


# ==============================================================================
# AcademicPeriod builder
# ==============================================================================

def build_academic_period(raw_year: str, raw_semester: str) -> AcademicPeriod | None:
    """
    Parse a raw academic year + semester pair and return an AcademicPeriod.

    This is the main function called by silver_transform.py.
    Returns None if either value cannot be parsed.

    Example:
        period = build_academic_period('AY 2023-2024', '1st Semester')
        # → AcademicPeriod(
        #       academic_year='2023-2024', semester=1,
        #       year_start=2023, year_end=2024,
        #       label='AY 2023-2024 1st Semester',
        #       sort_key=20231
        #   )
    """
    parsed_year = parse_academic_year(raw_year)
    if parsed_year is None:
        return None

    academic_year, year_start, year_end = parsed_year

    semester = parse_semester(raw_semester)
    if semester is None:
        return None

    label    = f"AY {academic_year} {semester_to_label(semester)}"
    sort_key = year_start * 10 + semester

    return AcademicPeriod(
        academic_year=academic_year,
        semester=semester,
        year_start=year_start,
        year_end=year_end,
        label=label,
        sort_key=sort_key,
    )


# ==============================================================================
# Sort key helpers (used in Gold aggregation and forecasting)
# ==============================================================================

def sort_key_to_period(sort_key: int) -> tuple[int, int]:
    """
    Convert a sort_key back to (year_start, semester).

        20231 → (2023, 1)
        20242 → (2024, 2)
    """
    semester   = sort_key % 10
    year_start = sort_key // 10
    return year_start, semester


def previous_sort_key(sort_key: int) -> int:
    """
    Return the sort_key for the immediately preceding semester.

    Handles semester wrap-around:
        20232 → 20231  (sem 2 → sem 1, same year)
        20231 → 20223  (sem 1 → previous summer)
        20223 → 20222  (summer → sem 2, same start year)
    """
    year_start, semester = sort_key_to_period(sort_key)

    if semester == 1:
        # Go back to Summer of the previous academic year
        return (year_start - 1) * 10 + 3
    elif semester == 2:
        return year_start * 10 + 1
    else:  # semester == 3 (summer)
        return year_start * 10 + 2


def get_all_sort_keys_for_year(year_start: int) -> list[int]:
    """
    Return all sort keys for a given academic year start.

        get_all_sort_keys_for_year(2023) → [20231, 20232, 20233]
    """
    return [year_start * 10 + s for s in (1, 2, 3)]


# ==============================================================================
# Validation helpers
# ==============================================================================

def is_valid_academic_year(academic_year: str) -> bool:
    """Check that a canonical academic year string is well-formed (e.g. '2023-2024')."""
    pattern = re.compile(r"^\d{4}-\d{4}$")
    if not pattern.match(academic_year):
        return False
    start, end = map(int, academic_year.split("-"))
    return end == start + 1


def is_valid_semester(semester: int) -> bool:
    """Check that a semester integer is within the valid range (1–3)."""
    return semester in (1, 2, 3)


def is_valid_year_level(year_level: int) -> bool:
    """Check that a year level integer is within the valid range (1–6)."""
    return 1 <= year_level <= 6