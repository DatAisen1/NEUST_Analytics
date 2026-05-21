# QUICK START: Immediate Fix for Constraint Error

## 🔴 YOU ARE HERE: Pipeline fails with constraint error

```
psycopg2.errors.UndefinedObject: constraint "uq_silver_programs_program_code" for table "programs" does not exist
```

---

## ✅ QUICK FIX (30 minutes)

### Step 1: Backup Database (2 min)
```bash
pg_dump neust_analytics > backup_$(date +%Y%m%d_%H%M%S).sql
echo "✅ Backup saved"
```

### Step 2: Fix the Migration File (5 min)

Open: `database/migrations/001_initial_schema.sql`

Find around line 250. You should see:
```sql
CREATE TABLE IF NOT EXISTS silver.programs (
    id SERIAL PRIMARY KEY,
    program_code TEXT NOT NULL,
    ...
);

ALTER TABLE silver.programs
    ADD CONSTRAINT uq_silver_programs_program_code UNIQUE (program_code);
```

Replace with:
```sql
CREATE TABLE IF NOT EXISTS silver.programs (
    id SERIAL PRIMARY KEY,
    program_code TEXT NOT NULL,
    program_name TEXT NOT NULL,
    college TEXT NOT NULL,
    department TEXT,
    duration_years SMALLINT NOT NULL DEFAULT 4,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- ✅ MOVE CONSTRAINT INTO CREATE TABLE
    CONSTRAINT uq_silver_programs_program_code UNIQUE (program_code)
);

CREATE INDEX IF NOT EXISTS idx_silver_programs_college
    ON silver.programs (college);
```

### Step 3: Re-run Migration (5 min)
```bash
psql -U postgres -d neust_analytics -f database/migrations/001_initial_schema.sql
```

Expected output: Should complete without errors.

### Step 4: Verify Constraint Exists (2 min)
```bash
psql -U postgres -d neust_analytics -c "
SELECT constraint_name, constraint_type
FROM information_schema.table_constraints
WHERE table_schema = 'silver'
  AND table_name = 'programs'
  AND constraint_name = 'uq_silver_programs_program_code';
"
```

Expected output:
```
        constraint_name         | constraint_type
──────────────────────────────┼────────────────
 uq_silver_programs_program_code | UNIQUE
```

If you see this row, constraint exists! ✅

If NO rows, constraint still missing! ❌ (troubleshoot below)

### Step 5: Run Pipeline (10 min)
```bash
python pipeline.py --reprocess
```

Should complete without constraint errors.

---

## 🔧 TROUBLESHOOTING

### Constraint still doesn't exist after Step 3?

This means the migration script didn't actually create the table or constraint. Try:

```bash
# Drop the table first
psql -U postgres -d neust_analytics -c "DROP TABLE IF EXISTS silver.programs CASCADE;"

# Then re-run the migration
psql -U postgres -d neust_analytics -f database/migrations/001_initial_schema.sql

# Verify again
psql -U postgres -d neust_analytics -c "
SELECT constraint_name
FROM information_schema.table_constraints
WHERE table_schema = 'silver'
  AND table_name = 'programs'
  AND constraint_name = 'uq_silver_programs_program_code';
"
```

### Migration script errors?

If you see errors like `ERROR: syntax error`, check:
1. Did you save the file after editing?
2. Are all SQL statements properly terminated with `;`?
3. Is the file UTF-8 encoded (not UTF-16 or other)?

### Still failing?

Read the detailed report: `PIPELINE_ANALYSIS_REPORT.md`

---

## 📋 OPTIONAL: Add Startup Validation (10 min)

This prevents future constraint errors by checking schema at pipeline startup.

### Add to connection.py

Find the `check_connection()` function around line 65.

Add after the existing schema checks:

```python
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

logger.info("✅ All constraints verified to exist")
```

This makes the pipeline fail immediately if schema is corrupted, rather than mid-run.

---

## 📊 What Was Wrong

**The Issue**: The constraint was added via a separate `ALTER TABLE` statement instead of being part of the `CREATE TABLE`.

**Why It Failed**:
- If table was recreated without re-running the migration, constraint vanished
- On production systems, ALTER statements can be skipped or forgotten
- Code expects constraint to exist but doesn't check

**Why This Fix Works**:
- Constraint is now part of table creation (atomic — both exist or both don't)
- Safe to re-run (idempotent)
- Consistent with how all other constraints are defined

---

## 📚 Want More Details?

See:
- `PIPELINE_ANALYSIS_REPORT.md` — Full analysis of all issues
- `SCHEMA_CORRECTION_FIX.md` — Detailed fix guide with all components
- `database/schema_validator.py` — Ready-to-use validation code

---

## ✨ After the Fix

You should:
1. ✅ Be able to run pipeline without constraint errors
2. ✅ Have early validation if schema ever gets corrupted
3. ✅ Have better error messages if something goes wrong
4. ✅ Have documented why this issue happened
5. ✅ Be protected against future similar issues

---

**Total Time**: 30-45 minutes
**Risk**: Low (backup created first)
**Data Loss**: None (metadata only)

