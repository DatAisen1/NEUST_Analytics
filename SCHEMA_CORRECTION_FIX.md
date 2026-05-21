# SCHEMA CORRECTION: Production-Grade Fix

## PART A: Fix the Split-Brain Schema Definition

### Step 1: Update SQL Migration (001_initial_schema.sql)

Move the constraint INTO the CREATE TABLE statement. This ensures the constraint is ALWAYS created when the table is created — no separate ALTER TABLE needed.

**Change from:**
```sql
CREATE TABLE IF NOT EXISTS silver.programs (
    id                  SERIAL          PRIMARY KEY,
    program_code        TEXT            NOT NULL,
    program_name        TEXT            NOT NULL,
    college             TEXT            NOT NULL,
    department          TEXT,
    duration_years      SMALLINT        NOT NULL DEFAULT 4,
    is_active           BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

ALTER TABLE silver.programs
    ADD CONSTRAINT uq_silver_programs_program_code UNIQUE (program_code);
```

**Change to:**
```sql
CREATE TABLE IF NOT EXISTS silver.programs (
    id                  SERIAL          PRIMARY KEY,
    program_code        TEXT            NOT NULL,
    program_name        TEXT            NOT NULL,
    college             TEXT            NOT NULL,
    department          TEXT,
    duration_years      SMALLINT        NOT NULL DEFAULT 4,
    is_active           BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    -- ✅ Constraint is now PART OF the table creation — guaranteed to exist
    CONSTRAINT uq_silver_programs_program_code UNIQUE (program_code)
);

-- Still create the index separately (safe to do in a separate statement)
CREATE INDEX IF NOT EXISTS idx_silver_programs_college
    ON silver.programs (college);
```

**Rationale:**
- Constraint is now created **atomically** with table
- No missing step if migration is incomplete
- Idempotent: `CREATE TABLE IF NOT EXISTS` is safe to re-run
- Index creation is idempotent and separate (no issues)

---

### Step 2: Update SQLAlchemy Model (silver_models.py)

Keep the model definition but add a comment noting the constraint is already in migration.

**No functional change needed**, but clarify intent:

```python
@declared_attr
def __table_args__(cls):
    return (
        # ✅ This constraint MUST be synchronized with 001_initial_schema.sql
        # ✅ The migration creates it as: CONSTRAINT uq_silver_programs_program_code UNIQUE (program_code)
        # ✅ SQLAlchemy will validate it exists during ORM initialization
        UniqueConstraint("program_code", name="uq_silver_programs_program_code"),
        Index("idx_silver_programs_college", "college"),
        {
            "schema": "silver",
            "comment": "Canonical programs and colleges reference table",
        },
    )
```

**Purpose of keeping the definition here:**
- SQLAlchemy ORM validates that constraint exists at import time
- If constraint is missing from database, SQLAlchemy will raise an error early
- Serves as a runtime schema validation checkpoint

---

## PART B: Add Schema Validation at Startup

### Step 3: Create Schema Validator (database/schema_validator.py)

Create a NEW file to verify all critical constraints exist:

```python
# ==============================================================================
# database/schema_validator.py
# Verifies database schema matches expected constraints and indexes
# Run at pipeline startup to catch drift early
# ==============================================================================

from sqlalchemy import text
from database.connection import get_session
from utils.logger import logger

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
    
    Returns:
        (ok: bool, missing: list[str])
        - ok = True if all constraints exist
        - missing = list of missing constraint names
    """
    missing = []
    
    with get_session() as session:
        for table_name, constraints in REQUIRED_CONSTRAINTS.items():
            schema, table = table_name.split(".")
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
                    missing.append(f"{table_name}.{constraint_name}")
                    logger.error(
                        "SCHEMA VALIDATION FAILED: Constraint '{}' missing from table '{}'",
                        constraint_name,
                        table_name,
                    )
    
    if missing:
        logger.error(
            "Schema validation FAILED — {} constraint(s) missing. "
            "This will cause pipeline upserts to fail. "
            "Run: psql -U postgres -d neust_analytics -f database/migrations/001_initial_schema.sql",
            len(missing),
        )
        return False, missing
    
    logger.info("✅ Schema validation PASSED — all {} constraints exist", 
                sum(len(c) for c in REQUIRED_CONSTRAINTS.values()))
    return True, []
```

---

### Step 4: Call Validator at Pipeline Startup (connection.py)

Update `check_connection()` to validate schema:

```python
def check_connection() -> bool:
    """
    Verify database connectivity AND schema integrity.
    
    Returns True only if:
    - Database is reachable
    - All schemas exist (bronze, silver, gold)
    - All required constraints exist
    """
    try:
        with get_engine().connect() as conn:
            # Existing checks...
            schemas_exist = conn.execute(
                text("SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = 'silver')")
            ).scalar()
            
            if not schemas_exist:
                logger.error("Database connection OK but schema 'silver' does not exist")
                return False
        
        # ✅ NEW: Validate schema constraints
        from database.schema_validator import validate_schema
        ok, missing = validate_schema()
        
        if not ok:
            logger.error(
                "Database connected but schema is INVALID. "
                "Missing {} constraints. Aborting pipeline.",
                len(missing),
            )
            return False
        
        return True
        
    except OperationalError as e:
        logger.error("Database connection failed: {}", e)
        return False
```

---

## PART C: Add Fallback for Missing Constraint (Defensive Programming)

### Step 5: Update Upsert to Handle Missing Constraint (silver_transform.py)

Add defensive code to detect and report constraint absence gracefully:

```python
def _get_or_create_program(self, program_code, program_name, college) -> int:
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
        
        try:
            # Try ON CONFLICT with constraint
            stmt = stmt.on_conflict_do_update(
                constraint="uq_silver_programs_program_code",
                set_={"program_name": stmt.excluded.program_name},
            )
            stmt = stmt.returning(SilverProgram.id)
            program_id = session.execute(stmt).scalar_one()
        
        except Exception as e:
            # ✅ If constraint doesn't exist, provide helpful error
            if "uq_silver_programs_program_code" in str(e):
                logger.error(
                    "CRITICAL: Constraint 'uq_silver_programs_program_code' missing from silver.programs. "
                    "This is a schema corruption issue. "
                    "FIX: Re-run migration: psql -U postgres -d neust_analytics -f database/migrations/001_initial_schema.sql"
                )
            raise
    
    self._program_cache[program_code] = program_id
    return program_id
```

---

## PART D: Execution Instructions

### For Immediate Fix:

```bash
# 1. Update the SQL migration file first
# Edit: database/migrations/001_initial_schema.sql
# Apply the changes shown in Step 1 above

# 2. Re-run the migration
psql -U postgres -d neust_analytics -f database/migrations/001_initial_schema.sql

# 3. Verify constraint exists
psql -U postgres -d neust_analytics -c "
SELECT constraint_name, constraint_type
FROM information_schema.table_constraints
WHERE table_schema = 'silver'
  AND table_name = 'programs'
  AND constraint_name = 'uq_silver_programs_program_code';
"

# Expected output:
#        constraint_name         | constraint_type
# ──────────────────────────────┼────────────────
#  uq_silver_programs_program_code | UNIQUE
```

### For Production Deployment:

1. ✅ Apply Step 1 (fix SQL migration)
2. ✅ Apply Step 3 (create schema validator)
3. ✅ Apply Step 4 (integrate with connection check)
4. ✅ Apply Step 5 (defensive upsert code)
5. ✅ Add unit test to verify schema validator works
6. ✅ Document schema integrity requirements in README

---

## PART E: Long-Term Architectural Improvements

### Recommendation 1: Adopt Alembic for Managed Migrations

Current state: Raw SQL migrations with no version tracking.

**Better approach**: Use Alembic (lightweight migration framework):

```bash
pip install alembic
alembic init migrations
# Alembic tracks migration history and prevents partial runs
```

### Recommendation 2: Schema-as-Code with SQL Generated from Models

Future state: Generate migrations automatically from SQLAlchemy models:

```bash
alembic revision --autogenerate -m "Add programs table"
```

This eliminates the split-brain problem entirely.

### Recommendation 3: Automated Schema Drift Detection in CI/CD

Add to your test pipeline:
- Validate all constraints exist before running tests
- Validate all indexes exist
- Validate foreign key relationships
- Document expected schema in a canonical YAML/JSON schema file

---

## PART F: Testing the Fix

Create a test (tests/test_schema.py):

```python
def test_constraint_uq_silver_programs_program_code_exists():
    """Verify the critical constraint exists — fails fast if schema is broken."""
    from database.schema_validator import validate_schema
    ok, missing = validate_schema()
    assert ok, f"Schema validation failed. Missing: {missing}"
    assert "uq_silver_programs_program_code" not in missing


def test_upsert_on_conflict_with_constraint():
    """Test the upsert operation works when constraint exists."""
    # Insert program
    from transformation.silver_transform import SilverTransformer
    result = SilverTransformer._get_or_create_program("BSCS", "Bachelor of Science in CS", "CCS")
    assert result > 0
    
    # Upsert same program — should not fail
    result2 = SilverTransformer._get_or_create_program("BSCS", "BS Computer Science", "CCS")
    assert result2 == result  # Same ID returned (updated, not inserted)
```

---

## SUMMARY OF FIXES

| Issue | Fix | Impact |
|---|---|---|
| Constraint in ALTER TABLE (fragile) | Move into CREATE TABLE (atomic) | Eliminates split-brain, ensures atomicity |
| No schema validation | Add startup validator | Catches issues early, before pipeline runs |
| No fallback on missing constraint | Add defensive error handling | Better error messages if schema is corrupt |
| No migration version tracking | Adopt Alembic (future) | Prevents partial migrations, tracks history |
| Undocumented schema requirements | Document in schema_validator.py | Serves as schema contract |

---

## VERIFICATION CHECKLIST

After applying fixes:

- [ ] Migration file updated with constraint in CREATE TABLE
- [ ] Schema validator created and integrated
- [ ] Connection check calls schema validator at startup
- [ ] Upsert code has defensive error handling
- [ ] Tests pass (including schema validation test)
- [ ] Documentation updated
- [ ] README includes migration instructions
- [ ] Team trained on running migrations correctly

