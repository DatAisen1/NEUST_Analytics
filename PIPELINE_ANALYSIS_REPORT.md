# NEUST ETL PIPELINE: COMPREHENSIVE ARCHITECTURE ANALYSIS & SCHEMA CORRECTION REPORT

**Date**: 2026-05-21
**Analysis Type**: Full ETL/data pipeline architecture review + constraint error root cause analysis
**Status**: 🔴 **CRITICAL ISSUES FOUND** (but fixable)

---

## EXECUTIVE SUMMARY

Your analytics pipeline failed with:
```
psycopg2.errors.UndefinedObject: constraint "uq_silver_programs_program_code" for table "programs" does not exist
```

**Root Cause**: Split-brain schema definition across SQLAlchemy models and raw SQL migrations, combined with a **fragile ALTER TABLE statement** that adds the constraint separately from table creation.

**Impact**: 
- ✅ Pipeline currently fails mid-run when encountering new programs
- ✅ Deleting tables masks the issue (temporary relief, not a fix)
- ✅ Future database recreations will reproduce the error
- ✅ All upserts rely on constraints; one missing constraint breaks the entire pipeline

**This is NOT your fault** — this is a well-known architectural trap in projects mixing ORM definitions with raw SQL migrations.

---

## FINDINGS BY CATEGORY

### 1️⃣ SCHEMA DEFINITION ISSUE (CRITICAL)

**The Problem**: The constraint `uq_silver_programs_program_code` is defined in TWO separate places that don't communicate:

**Place 1: SQLAlchemy Model** ([silver_models.py](silver_models.py#L110))
```python
@declared_attr
def __table_args__(cls):
    return (
        UniqueConstraint("program_code", name="uq_silver_programs_program_code"),
```
- ✅ Tells Python ORM the constraint should exist
- ❌ Doesn't execute anything in the database

**Place 2: SQL Migration** ([001_initial_schema.sql](001_initial_schema.sql#L245-L260))
```sql
CREATE TABLE IF NOT EXISTS silver.programs (
    ...
);

ALTER TABLE silver.programs
    ADD CONSTRAINT uq_silver_programs_program_code UNIQUE (program_code);
```
- ✅ Actually creates the constraint in PostgreSQL
- ❌ **Uses a separate ALTER TABLE statement** (fragile)
- ❌ **NOT part of CREATE TABLE** (not atomic)

**Why This Is Bad**:
- If the ALTER TABLE statement doesn't run, constraint is missing
- If tables are dropped and recreated without running the full migration, constraint disappears
- If migration is incomplete or interrupted, constraint might not be created
- **No single source of truth** — code can't validate which is correct

---

### 2️⃣ INCONSISTENT CONSTRAINT PLACEMENT (MEDIUM RISK)

**Finding**: Only `uq_silver_programs_program_code` is added via ALTER TABLE. ALL other constraints are inline:

| Table | Constraint | Method | Risk |
|---|---|---|---|
| silver.programs | uq_silver_programs_program_code | ALTER TABLE | 🔴 HIGH |
| silver.academic_periods | uq_silver_ap | CREATE TABLE (inline) | ✅ LOW |
| silver.enrollment_flow | uq_silver_ef | CREATE TABLE (inline) | ✅ LOW |
| silver.student_outcomes | uq_silver_so | CREATE TABLE (inline) | ✅ LOW |
| gold.dim_time | uq_gold_dim_time | CREATE TABLE (inline) | ✅ LOW |
| gold.fact_enrollment_metrics | uq_gold_fact | CREATE TABLE (inline) | ✅ LOW |

**This indicates**: Someone added this constraint after table creation was already done, likely because they forgot it during initial table design. Then the approach (ALTER TABLE) got locked in, even though it's the worst practice.

---

### 3️⃣ NO SCHEMA VALIDATION AT STARTUP (CRITICAL)

**Current behavior** ([connection.py](connection.py#L65)):
```python
def check_connection() -> bool:
    """Verify that the database is reachable and all three schemas exist."""
    # ✅ Checks if database is reachable
    # ✅ Checks if schemas exist (bronze, silver, gold)
    # ❌ DOES NOT check if constraints exist
    # ❌ DOES NOT check if indexes exist
```

**Result**: Pipeline can start with a corrupted schema and fails mid-run instead of failing immediately.

**Where it fails**: Mid-run during Silver transformation:
```
Stage 1: Bronze ✅ (no constraints used here)
Stage 2: Silver ❌ (first upsert that needs constraint fails)
         → Error is cryptic: "constraint ... does not exist"
         → User has no idea what went wrong or how to fix it
```

**Why This Is Bad**:
- Developer has no early warning
- Failure happens after consuming time on Bronze ingestion
- Error message is confusing (seems like a DB error, not a schema error)
- No automated way to detect drift

---

### 4️⃣ UPSERT CODE HAS NO FALLBACK (HIGH RISK)

**Current code** ([silver_transform.py](silver_transform.py#L380)):
```python
def _get_or_create_program(self, program_code, program_name, college) -> int:
    with get_session() as session:
        stmt = pg_insert(SilverProgram).values(...)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_silver_programs_program_code",  # ⚠️ ASSUMES THIS EXISTS
            set_={"program_name": stmt.excluded.program_name},
        )
        result = session.execute(stmt)  # ← CRASHES HERE if constraint missing
        return program_id
```

**What happens if constraint is missing**:
```
Traceback:
  File ".../transformation/silver_transform.py", line 384, in _get_or_create_program
    program_id = session.execute(stmt).scalar_one()
psycopg2.errors.UndefinedObject: constraint "uq_silver_programs_program_code" does not exist
```

**Why This Is Bad**:
- No defensive programming
- No helpful error message
- No graceful degradation
- No way to proceed or work around

---

### 5️⃣ MULTIPLE UPSERTS DEPEND ON CONSTRAINTS (SYSTEMIC RISK)

The pipeline uses ON CONFLICT DO UPDATE in many places, **all dependent on constraints**:

| File | Line | Constraint | Fallback if Missing |
|---|---|---|---|
| [silver_transform.py](silver_transform.py#L374) | 374 | uq_silver_ap | ❌ NONE |
| [silver_transform.py](silver_transform.py#L286) | 286 | uq_silver_ef | ❌ NONE |
| [silver_transform.py](silver_transform.py#L347) | 347 | uq_silver_so | ❌ NONE |
| [silver_transform.py](silver_transform.py#L384) | 384 | uq_silver_programs_program_code | ❌ NONE |
| [gold_aggregate.py](gold_aggregate.py#L110) | 110 | uq_gold_dim_time | ❌ NONE |
| [gold_aggregate.py](gold_aggregate.py#L143) | 143 | dim_program_program_code_key | ❌ NONE |
| [gold_aggregate.py](gold_aggregate.py#L167) | 167 | uq_gold_fact | ❌ NONE |

**If ANY constraint is missing**, the entire pipeline cascade fails.

---

### 6️⃣ DATABASE DRIFT DETECTION IS MISSING (MEDIUM RISK)

No automated checks for:
- ✗ Missing constraints
- ✗ Missing indexes
- ✗ Missing columns
- ✗ Incorrect column types
- ✗ Orphaned rows (FK violations)
- ✗ Schema changes made outside of migration process

**Result**: Production database can silently drift from development environment with no way to detect it until something fails.

---

## WHY THIS HAPPENED

This is a **common architectural anti-pattern** in projects that:
1. ✓ Start with raw SQL migrations (low-tech, manual)
2. ✓ Add SQLAlchemy ORM later (more dev-friendly)
3. ✓ Don't establish a single source of truth for schema
4. ✓ Don't implement startup schema validation
5. ✓ Don't separate schema management from business logic

**Result**: Schema definitions drift across two codebases (Python + SQL) with no synchronization.

---

## WHY DELETING TABLES MASKS THE ISSUE

**What happens when you delete tables**:
```bash
DROP TABLE silver.programs CASCADE;
```

**Short term (temporary relief)**:
- Table is gone, constraint is gone
- Next migration re-creates both (table + ALTER TABLE constraint)
- Pipeline runs successfully
- 🎉 Problem appears to be fixed!

**Long term (fragile)**:
- ✗ Original split-brain definition persists
- ✗ Constraint STILL added via separate ALTER TABLE
- ✗ If future migration fails to run ALTER TABLE, constraint vanishes again
- ✗ No guarantee of success on next database rebuild
- ✗ Production data loss if real analytics data exists

**Deleting tables is NOT a fix** — it's a band-aid that works until it doesn't.

---

## PRODUCTION-GRADE FIX (3 PARTS)

### Part A: Fix the SQL Migration (Atomic Constraint Creation)

**File**: [001_initial_schema.sql](001_initial_schema.sql#L245-L260)

**Change from**:
```sql
CREATE TABLE IF NOT EXISTS silver.programs (
    id SERIAL PRIMARY KEY,
    program_code TEXT NOT NULL,
    ...
);

ALTER TABLE silver.programs
    ADD CONSTRAINT uq_silver_programs_program_code UNIQUE (program_code);
```

**Change to**:
```sql
CREATE TABLE IF NOT EXISTS silver.programs (
    id SERIAL PRIMARY KEY,
    program_code TEXT NOT NULL,
    ...,
    CONSTRAINT uq_silver_programs_program_code UNIQUE (program_code)
);

CREATE INDEX IF NOT EXISTS idx_silver_programs_college
    ON silver.programs (college);
```

**Why This Works**:
- ✅ Constraint is now **part of table creation** (atomic)
- ✅ Constraint exists if and only if table exists
- ✅ No risk of missing constraint if ALTER TABLE is skipped
- ✅ Idempotent: `CREATE TABLE IF NOT EXISTS` is safe to re-run

**Step to execute**:
```bash
# 1. Backup your database (just in case!)
pg_dump neust_analytics > backup_before_fix.sql

# 2. Apply the fixed migration
psql -U postgres -d neust_analytics -f database/migrations/001_initial_schema.sql

# 3. Verify constraint now exists
psql -U postgres -d neust_analytics -c "
SELECT constraint_name
FROM information_schema.table_constraints
WHERE table_schema = 'silver'
  AND table_name = 'programs'
  AND constraint_name = 'uq_silver_programs_program_code';
"

# Expected output: 
#         constraint_name
# ──────────────────────────────
#  uq_silver_programs_program_code
```

---

### Part B: Add Schema Validation at Startup (Early Detection)

**File to create**: [database/schema_validator.py](database/schema_validator.py)

This file has already been created for you. It:
- ✅ Verifies all required constraints exist at startup
- ✅ Provides clear error messages if constraints are missing
- ✅ Fails the pipeline immediately (not mid-run)
- ✅ Tells user exactly what to do

**Integration into connection check** ([connection.py](connection.py)):

```python
def check_connection() -> bool:
    """Verify database connectivity AND schema integrity."""
    try:
        # ... existing checks ...
        
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
    except Exception as e:
        logger.error("Database connection failed: {}", e)
        return False
```

**When to run this**: Every pipeline start (in `pipeline.py` main() before stages begin)

---

### Part C: Add Defensive Error Handling (Helpful Errors)

**File**: [silver_transform.py](silver_transform.py#L380) in `_get_or_create_program()`

Add try/except around the upsert to detect missing constraint:

```python
def _get_or_create_program(self, program_code, program_name, college) -> int:
    if program_code in self._program_cache:
        return self._program_cache[program_code]
    
    with get_session() as session:
        stmt = pg_insert(SilverProgram).values(...)
        
        try:
            stmt = stmt.on_conflict_do_update(
                constraint="uq_silver_programs_program_code",
                set_={"program_name": stmt.excluded.program_name},
            )
            stmt = stmt.returning(SilverProgram.id)
            program_id = session.execute(stmt).scalar_one()
        
        except Exception as e:
            if "uq_silver_programs_program_code" in str(e):
                logger.error(
                    "❌ CRITICAL SCHEMA ERROR: Constraint 'uq_silver_programs_program_code' "
                    "missing from silver.programs table.\n"
                    "This is a database schema corruption issue.\n"
                    "FIX: Re-run the migration:\n"
                    "  psql -U postgres -d neust_analytics -f database/migrations/001_initial_schema.sql"
                )
            raise
    
    self._program_cache[program_code] = program_id
    return program_id
```

**Benefit**: Much better error message for users when schema is corrupted.

---

## STEP-BY-STEP IMPLEMENTATION

### Immediate Actions (TODAY)

1. ✅ **Create schema_validator.py** (DONE — file is ready)
2. ✅ **Create SCHEMA_CORRECTION_FIX.md** (DONE — full documentation ready)
3. ✅ **Create MIGRATION_FIX.sql** (DONE — shows exact changes needed)

### Next Steps (THIS WEEK)

1. **Backup database**
   ```bash
   pg_dump neust_analytics > backup_$(date +%Y%m%d).sql
   ```

2. **Apply Part A: Update migration file**
   - Open [001_initial_schema.sql](001_initial_schema.sql)
   - Find line ~250 where `silver.programs` is created
   - Move constraint into CREATE TABLE
   - See [MIGRATION_FIX.sql](MIGRATION_FIX.sql) for exact syntax

3. **Apply Part A: Re-run migration**
   ```bash
   psql -U postgres -d neust_analytics -f database/migrations/001_initial_schema.sql
   ```

4. **Apply Part B: Integrate schema validator**
   - Update [connection.py](connection.py) `check_connection()` to call `validate_schema()`
   - See [SCHEMA_CORRECTION_FIX.md](SCHEMA_CORRECTION_FIX.md) Part B for code

5. **Apply Part C: Add defensive error handling** (optional but recommended)
   - Update [silver_transform.py](silver_transform.py) `_get_or_create_program()`
   - See [SCHEMA_CORRECTION_FIX.md](SCHEMA_CORRECTION_FIX.md) Part C for code

6. **Test the fix**
   ```bash
   python pipeline.py --reprocess
   ```

7. **Verify schema is valid**
   ```bash
   psql -U postgres -d neust_analytics -c "
   SELECT constraint_name
   FROM information_schema.table_constraints
   WHERE table_schema = 'silver'
     AND table_name = 'programs'
     AND constraint_name = 'uq_silver_programs_program_code';
   "
   ```

---

## LONG-TERM ARCHITECTURAL IMPROVEMENTS

### Recommendation 1: Adopt Alembic for Managed Migrations

**Current state**: Raw SQL migrations with no version tracking
**Better state**: Alembic-managed migrations with automatic versioning

```bash
pip install alembic
alembic init migrations
```

**Benefits**:
- ✅ Prevents partial migrations (atomic execution)
- ✅ Tracks migration history (no guessing what ran)
- ✅ Can rollback previous migrations
- ✅ Can auto-generate from SQLAlchemy models

### Recommendation 2: Schema-as-Code

**Future state**: Generate migrations from SQLAlchemy models:

```bash
alembic revision --autogenerate -m "Add programs table"
```

This eliminates the split-brain problem entirely.

### Recommendation 3: Automated Schema Drift Detection in CI/CD

Add to your test pipeline:
```bash
pytest tests/test_schema_integrity.py
```

This validates all constraints exist before any tests run.

---

## FILES PROVIDED

1. 📄 **[SCHEMA_CORRECTION_FIX.md](SCHEMA_CORRECTION_FIX.md)** — Comprehensive fix guide with code examples
2. 📄 **[MIGRATION_FIX.sql](MIGRATION_FIX.sql)** — Exact SQL changes needed for 001_initial_schema.sql
3. 📄 **[database/schema_validator.py](database/schema_validator.py)** — Ready-to-use schema validation code
4. 📋 **This report** — Complete analysis and recommendations

---

## VERIFICATION CHECKLIST

After applying fixes:

- [ ] Backup database created
- [ ] SQL migration file updated (constraint in CREATE TABLE)
- [ ] Migration re-run successfully
- [ ] Constraint verified to exist in database
- [ ] schema_validator.py integrated into connection check
- [ ] Pipeline starts and validates schema ✅
- [ ] Pipeline runs to completion without constraint errors
- [ ] Defensive error handling added (optional)
- [ ] Unit tests pass
- [ ] Documentation updated
- [ ] Team trained on new schema validation

---

## SUMMARY TABLE: Issues vs Fixes

| Issue | Severity | Root Cause | Fix | Benefit |
|---|---|---|---|---|
| Constraint added via ALTER (non-atomic) | 🔴 CRITICAL | Schema design | Move to CREATE TABLE | Atomic, guaranteed |
| No startup validation | 🔴 CRITICAL | Missing checks | Add schema_validator | Early detection |
| Upsert crashes with no fallback | 🟠 HIGH | No defensive code | Add try/except | Better errors |
| Multiple constraints at risk | 🟠 HIGH | No validation | Check all at startup | Comprehensive |
| Schema drift undetected | 🟡 MEDIUM | No monitoring | Add tests | Prevents corruption |
| Split-brain definitions | 🟡 MEDIUM | Architecture | Adopt Alembic (future) | Single source of truth |

---

## NEXT STEPS

1. **This week**: Apply all three parts of the fix
2. **This sprint**: Add schema_validator to CI/CD tests
3. **Next sprint**: Evaluate Alembic for future migrations
4. **Documentation**: Update README with schema management procedures

---

## QUESTIONS?

- **What if I'm not sure about the fix?** → Refer to [SCHEMA_CORRECTION_FIX.md](SCHEMA_CORRECTION_FIX.md)
- **How do I verify it worked?** → See verification SQL commands above
- **What if constraint still doesn't exist after re-run?** → Migration script may not have executed properly; verify by checking information_schema
- **Should I delete tables?** → No! Apply the fix instead. Deleting tables masks the issue.
- **Will this affect my data?** → No, the constraint is metadata-only; existing valid data won't be affected

---

**Report Generated**: 2026-05-21
**Status**: Ready for implementation
**Risk Level**: Low (fixes are well-tested patterns)
**Estimated Fix Time**: 30-60 minutes

