# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# database/schema_validator.py
# Verifies database schema matches expected constraints and indexes
# Run at pipeline startup to catch drift early
# ==============================================================================

from sqlalchemy import text
from database.connection import get_session
from utils.logger import logger

# All constraints that MUST exist for the pipeline to work
REQUIRED_CONSTRAINTS = {
    "silver.programs": ["uq_silver_programs_program_code"],
    "silver.academic_periods": ["uq_silver_ap"],
    "silver.enrollment_flow": ["uq_silver_ef"],
    "silver.student_outcomes": ["uq_silver_so"],
    "gold.dim_time": ["uq_gold_dim_time"],
    "gold.fact_enrollment_metrics": ["uq_gold_fact"],
}


def validate_schema() -> tuple[bool, list[str]]:
    """
    Verify all required constraints exist in the database.
    
    This catches schema drift early before the pipeline crashes
    mid-run with a cryptic "constraint does not exist" error.
    
    Returns:
        (ok: bool, missing: list[str])
        - ok = True if all constraints exist
        - missing = list of missing constraint names (e.g., "silver.programs.uq_...")
    """
    missing = []
    
    try:
        with get_session() as session:
            for table_full_name, constraints in REQUIRED_CONSTRAINTS.items():
                schema, table = table_full_name.split(".")
                
                for constraint_name in constraints:
                    exists = session.execute(
                        text(
                            """
                            SELECT EXISTS (
                                SELECT 1 FROM information_schema.table_constraints
                                WHERE table_schema = :schema
                                  AND table_name = :table
                                  AND constraint_name = :name
                            )
                            """
                        ),
                        {
                            "schema": schema,
                            "table": table,
                            "name": constraint_name,
                        },
                    ).scalar()
                    
                    if not exists:
                        full_name = f"{schema}.{table}.{constraint_name}"
                        missing.append(full_name)
                        logger.error(
                            "SCHEMA VALIDATION FAILED: Constraint '{}' missing from table '{}.{}'",
                            constraint_name,
                            schema,
                            table,
                        )
    
    except Exception as e:
        logger.error("Schema validation error (connection issue?): {}", e)
        return False, missing
    
    if missing:
        logger.error(
            "❌ SCHEMA VALIDATION FAILED — {} constraint(s) missing:\n{}",
            len(missing),
            "\n  ".join(f"  - {m}" for m in missing),
        )
        logger.error(
            "This will cause pipeline upserts to fail with 'constraint does not exist' errors.\n"
            "FIX: Re-run the migration:\n"
            "  psql -U postgres -d neust_analytics -f database/migrations/001_initial_schema.sql"
        )
        return False, missing
    
    total_constraints = sum(len(c) for c in REQUIRED_CONSTRAINTS.values())
    logger.info("✅ Schema validation PASSED — all {} constraints exist", total_constraints)
    return True, []


def validate_constraint_exists(schema: str, table: str, constraint_name: str) -> bool:
    """
    Quick check if a specific constraint exists.
    Useful for defensive coding before attempting ON CONFLICT.
    """
    try:
        with get_session() as session:
            exists = session.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.table_constraints
                        WHERE table_schema = :schema
                          AND table_name = :table
                          AND constraint_name = :name
                    )
                    """
                ),
                {
                    "schema": schema,
                    "table": table,
                    "name": constraint_name,
                },
            ).scalar()
        return exists
    except Exception as e:
        logger.warning("Could not check constraint {}.{}.{}: {}", schema, table, constraint_name, e)
        return False
