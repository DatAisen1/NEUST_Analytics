-- ==============================================================================
-- FIX FOR 001_initial_schema.sql
-- ==============================================================================
-- THIS FILE shows the EXACT changes needed to fix the split-brain constraint issue
--
-- BACKGROUND:
-- The constraint uq_silver_programs_program_code is currently added via ALTER TABLE
-- AFTER table creation. This is fragile — if the ALTER statement doesn't run, 
-- the constraint vanishes and the pipeline fails with:
--   psycopg2.errors.UndefinedObject: constraint "uq_silver_programs_program_code" ... does not exist
--
-- FIX: Include the constraint IN the CREATE TABLE statement, not after.
-- This ensures atomicity — constraint exists whenever table exists.
--
-- ==============================================================================

-- BEFORE (FRAGILE):
/*
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

-- Separate ALTER — can be skipped, forgotten, or rolled back independently
ALTER TABLE silver.programs
    ADD CONSTRAINT uq_silver_programs_program_code UNIQUE (program_code);

CREATE INDEX IF NOT EXISTS idx_silver_programs_college
    ON silver.programs (college);
*/

-- AFTER (PRODUCTION-GRADE):
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

    -- ✅ CONSTRAINT IS NOW PART OF TABLE CREATION
    -- ✅ Atomic: constraint exists if and only if table exists
    -- ✅ No risk of missing constraint if ALTER TABLE is skipped
    CONSTRAINT uq_silver_programs_program_code UNIQUE (program_code)
);

COMMENT ON TABLE silver.programs IS 'Canonical program and college reference table';

-- Index creation is safe to do in a separate statement (it's idempotent)
CREATE INDEX IF NOT EXISTS idx_silver_programs_college
    ON silver.programs (college);

-- ==============================================================================
-- VERIFICATION AFTER RUNNING FIXED MIGRATION:
-- ==============================================================================
-- 
-- Run this SQL to verify the constraint exists:
--
-- SELECT constraint_name, constraint_type
-- FROM information_schema.table_constraints
-- WHERE table_schema = 'silver'
--   AND table_name = 'programs'
--   AND constraint_name = 'uq_silver_programs_program_code';
--
-- Expected output:
--         constraint_name         | constraint_type
-- ──────────────────────────────┼────────────────
--  uq_silver_programs_program_code | UNIQUE
--
-- If no rows are returned, the constraint still doesn't exist and 
-- the pipeline WILL crash with constraint_not_found error.

