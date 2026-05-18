-- ==============================================================================
-- NEUST Academic Analytics and Forecasting System
-- Migration: 001_initial_schema.sql
-- Description: Creates Bronze, Silver, and Gold schemas with all tables
-- Run: psql -U postgres -d neust_analytics -f 001_initial_schema.sql
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- EXTENSIONS
-- ------------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";     -- fast LIKE / ILIKE on text columns
CREATE EXTENSION IF NOT EXISTS "btree_gin";   -- GIN indexes on scalar types

-- ------------------------------------------------------------------------------
-- SCHEMAS
-- ------------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;

COMMENT ON SCHEMA bronze IS 'Raw data landing zone — unmodified source records';
COMMENT ON SCHEMA silver IS 'Cleaned, standardized, and validated records';
COMMENT ON SCHEMA gold   IS 'Analytics-ready star schema — fact and dimension tables';

-- ==============================================================================
-- BRONZE LAYER
-- Raw data exactly as received from Excel exports.
-- No transformation applied. Append-only. Retained indefinitely.
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- bronze.enrollment_flow
-- Maps to: Enrollment_Flow sheet
-- Columns: Academic Year, Semester, College/Department, Program/Course, Major,
--          Year Level, Gender, Applicants, Accepted Applicants, Total Enrolled,
--          New Students, Transferees, Returnees
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bronze.enrollment_flow (
    id                  BIGSERIAL       PRIMARY KEY,

    -- Source tracking
    source_file         TEXT            NOT NULL,
    ingested_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    batch_id            UUID            NOT NULL DEFAULT uuid_generate_v4(),

    -- Raw source columns (stored as TEXT to preserve exact source values)
    academic_year       TEXT,
    semester            TEXT,
    college_department  TEXT,
    program_course      TEXT,
    major               TEXT,
    year_level          TEXT,
    gender              TEXT,

    -- Numeric metrics (nullable — source may have blanks)
    applicants          INTEGER,
    accepted_applicants INTEGER,
    total_enrolled      INTEGER,
    new_students        INTEGER,
    transferees         INTEGER,
    returnees           INTEGER,

    -- Soft delete / audit
    is_deleted          BOOLEAN         NOT NULL DEFAULT FALSE,
    deleted_at          TIMESTAMPTZ
);

COMMENT ON TABLE  bronze.enrollment_flow                    IS 'Raw enrollment intake records from Excel Enrollment_Flow sheet';
COMMENT ON COLUMN bronze.enrollment_flow.source_file        IS 'Original filename that was ingested (e.g. enrollment_flow_AY2024.xlsx)';
COMMENT ON COLUMN bronze.enrollment_flow.batch_id           IS 'Groups all rows from a single ingestion run';
COMMENT ON COLUMN bronze.enrollment_flow.academic_year      IS 'Raw academic year string (e.g. 2023-2024)';

CREATE INDEX IF NOT EXISTS idx_bronze_ef_batch_id
    ON bronze.enrollment_flow (batch_id);

CREATE INDEX IF NOT EXISTS idx_bronze_ef_ingested_at
    ON bronze.enrollment_flow (ingested_at DESC);

CREATE INDEX IF NOT EXISTS idx_bronze_ef_academic_year
    ON bronze.enrollment_flow (academic_year);

-- ------------------------------------------------------------------------------
-- bronze.student_outcomes
-- Maps to: Student_Outcomes sheet
-- Columns: Academic Year, Semester, College/Department, Program/Course, Major,
--          Year Level, Gender, Graduates, Dropouts, Shifters Out, Shifters In
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bronze.student_outcomes (
    id                  BIGSERIAL       PRIMARY KEY,

    -- Source tracking
    source_file         TEXT            NOT NULL,
    ingested_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    batch_id            UUID            NOT NULL DEFAULT uuid_generate_v4(),

    -- Raw source columns
    academic_year       TEXT,
    semester            TEXT,
    college_department  TEXT,
    program_course      TEXT,
    major               TEXT,
    year_level          TEXT,
    gender              TEXT,

    -- Outcome metrics
    graduates           INTEGER,
    dropouts            INTEGER,
    shifters_out        INTEGER,
    shifters_in         INTEGER,

    -- Soft delete / audit
    is_deleted          BOOLEAN         NOT NULL DEFAULT FALSE,
    deleted_at          TIMESTAMPTZ
);

COMMENT ON TABLE bronze.student_outcomes IS 'Raw student outcome records from Excel Student_Outcomes sheet';

CREATE INDEX IF NOT EXISTS idx_bronze_so_batch_id
    ON bronze.student_outcomes (batch_id);

CREATE INDEX IF NOT EXISTS idx_bronze_so_ingested_at
    ON bronze.student_outcomes (ingested_at DESC);

CREATE INDEX IF NOT EXISTS idx_bronze_so_academic_year
    ON bronze.student_outcomes (academic_year);

-- ------------------------------------------------------------------------------
-- bronze.ingestion_log
-- Audit trail for every file loaded into Bronze.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bronze.ingestion_log (
    id              BIGSERIAL       PRIMARY KEY,
    batch_id        UUID            NOT NULL DEFAULT uuid_generate_v4(),
    source_file     TEXT            NOT NULL,
    target_table    TEXT            NOT NULL,
    rows_inserted   INTEGER         NOT NULL DEFAULT 0,
    rows_rejected   INTEGER         NOT NULL DEFAULT 0,
    status          TEXT            NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'success', 'failed', 'partial')),
    error_message   TEXT,
    started_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

COMMENT ON TABLE bronze.ingestion_log IS 'Audit log for every Bronze ingestion run';

CREATE INDEX IF NOT EXISTS idx_bronze_log_batch_id
    ON bronze.ingestion_log (batch_id);

CREATE INDEX IF NOT EXISTS idx_bronze_log_status
    ON bronze.ingestion_log (status);


-- ==============================================================================
-- SILVER LAYER
-- Cleaned, deduplicated, standardized, and validated records.
-- Source of truth for all downstream analytics.
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- silver.academic_periods
-- Lookup table for standardized academic year + semester combinations.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.academic_periods (
    id              SERIAL          PRIMARY KEY,
    academic_year   TEXT            NOT NULL,   -- e.g. '2023-2024'
    semester        SMALLINT        NOT NULL,   -- 1, 2, or 3 (summer)
    year_start      SMALLINT        NOT NULL,
    year_end        SMALLINT        NOT NULL,
    label           TEXT            NOT NULL,   -- e.g. 'AY 2023-2024 Sem 1'
    is_current      BOOLEAN         NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_silver_ap UNIQUE (academic_year, semester)
);

COMMENT ON TABLE silver.academic_periods IS 'Standardized academic year and semester reference table';

-- ------------------------------------------------------------------------------
-- silver.programs
-- Lookup table for standardized college, department, and program codes.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.programs (
    id                  SERIAL          PRIMARY KEY,
    program_code        TEXT            NOT NULL UNIQUE,  -- canonical code e.g. 'BSCS'
    program_name        TEXT            NOT NULL,
    college             TEXT            NOT NULL,
    department          TEXT,
    duration_years      SMALLINT        NOT NULL DEFAULT 4,
    is_active           BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE silver.programs IS 'Canonical program and college reference table';

CREATE INDEX IF NOT EXISTS idx_silver_programs_college
    ON silver.programs (college);

-- ------------------------------------------------------------------------------
-- silver.enrollment_flow
-- Cleaned and standardized enrollment intake records.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.enrollment_flow (
    id                  BIGSERIAL       PRIMARY KEY,

    -- Foreign keys to lookups
    period_id           INTEGER         NOT NULL REFERENCES silver.academic_periods(id),
    program_id          INTEGER         NOT NULL REFERENCES silver.programs(id),

    -- Standardized fields
    academic_year       TEXT            NOT NULL,
    semester            SMALLINT        NOT NULL,
    college             TEXT            NOT NULL,
    program_code        TEXT            NOT NULL,
    major               TEXT,
    year_level          SMALLINT        NOT NULL CHECK (year_level BETWEEN 1 AND 6),
    gender              TEXT            CHECK (gender IN ('Male', 'Female', 'Other', 'Not Specified')),

    -- Enrollment metrics
    applicants          INTEGER         NOT NULL DEFAULT 0 CHECK (applicants >= 0),
    accepted_applicants INTEGER         NOT NULL DEFAULT 0 CHECK (accepted_applicants >= 0),
    total_enrolled      INTEGER         NOT NULL DEFAULT 0 CHECK (total_enrolled >= 0),
    new_students        INTEGER         NOT NULL DEFAULT 0 CHECK (new_students >= 0),
    transferees         INTEGER         NOT NULL DEFAULT 0 CHECK (transferees >= 0),
    returnees           INTEGER         NOT NULL DEFAULT 0 CHECK (returnees >= 0),

    -- Derived metrics
    acceptance_rate     NUMERIC(5,2)    GENERATED ALWAYS AS (
                            CASE WHEN applicants > 0
                            THEN ROUND((accepted_applicants::NUMERIC / applicants) * 100, 2)
                            ELSE NULL END
                        ) STORED,

    -- Lineage
    bronze_batch_id     UUID            NOT NULL,
    transformed_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_silver_ef UNIQUE (academic_year, semester, program_code, major, year_level, gender)
);

COMMENT ON TABLE silver.enrollment_flow IS 'Cleaned enrollment intake records — Bronze → Silver transformation output';

CREATE INDEX IF NOT EXISTS idx_silver_ef_period
    ON silver.enrollment_flow (period_id);

CREATE INDEX IF NOT EXISTS idx_silver_ef_program
    ON silver.enrollment_flow (program_id);

CREATE INDEX IF NOT EXISTS idx_silver_ef_year_sem
    ON silver.enrollment_flow (academic_year, semester);

CREATE INDEX IF NOT EXISTS idx_silver_ef_college
    ON silver.enrollment_flow (college);

-- ------------------------------------------------------------------------------
-- silver.student_outcomes
-- Cleaned and standardized student outcome records.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.student_outcomes (
    id                  BIGSERIAL       PRIMARY KEY,

    -- Foreign keys
    period_id           INTEGER         NOT NULL REFERENCES silver.academic_periods(id),
    program_id          INTEGER         NOT NULL REFERENCES silver.programs(id),

    -- Standardized fields
    academic_year       TEXT            NOT NULL,
    semester            SMALLINT        NOT NULL,
    college             TEXT            NOT NULL,
    program_code        TEXT            NOT NULL,
    major               TEXT,
    year_level          SMALLINT        NOT NULL CHECK (year_level BETWEEN 1 AND 6),
    gender              TEXT            CHECK (gender IN ('Male', 'Female', 'Other', 'Not Specified')),

    -- Outcome metrics
    graduates           INTEGER         NOT NULL DEFAULT 0 CHECK (graduates >= 0),
    dropouts            INTEGER         NOT NULL DEFAULT 0 CHECK (dropouts >= 0),
    shifters_out        INTEGER         NOT NULL DEFAULT 0 CHECK (shifters_out >= 0),
    shifters_in         INTEGER         NOT NULL DEFAULT 0 CHECK (shifters_in >= 0),

    -- Lineage
    bronze_batch_id     UUID            NOT NULL,
    transformed_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_silver_so UNIQUE (academic_year, semester, program_code, major, year_level, gender)
);

COMMENT ON TABLE silver.student_outcomes IS 'Cleaned student outcome records — Bronze → Silver transformation output';

CREATE INDEX IF NOT EXISTS idx_silver_so_period
    ON silver.student_outcomes (period_id);

CREATE INDEX IF NOT EXISTS idx_silver_so_program
    ON silver.student_outcomes (program_id);

CREATE INDEX IF NOT EXISTS idx_silver_so_year_sem
    ON silver.student_outcomes (academic_year, semester);

-- ------------------------------------------------------------------------------
-- silver.transformation_log
-- Audit trail for Silver transformation runs.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.transformation_log (
    id                  BIGSERIAL       PRIMARY KEY,
    bronze_batch_id     UUID            NOT NULL,
    target_table        TEXT            NOT NULL,
    rows_processed      INTEGER         NOT NULL DEFAULT 0,
    rows_inserted       INTEGER         NOT NULL DEFAULT 0,
    rows_updated        INTEGER         NOT NULL DEFAULT 0,
    rows_skipped        INTEGER         NOT NULL DEFAULT 0,
    status              TEXT            NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'success', 'failed', 'partial')),
    error_message       TEXT,
    started_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

COMMENT ON TABLE silver.transformation_log IS 'Audit log for every Silver transformation run';


-- ==============================================================================
-- GOLD LAYER
-- Star schema optimized for dashboard queries and forecasting models.
-- Pre-aggregated. Read-heavy. Rebuilt from Silver on each pipeline run.
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- gold.dim_time
-- Time dimension — one row per academic year + semester combination.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gold.dim_time (
    time_id             SERIAL          PRIMARY KEY,
    academic_year       TEXT            NOT NULL,
    semester            SMALLINT        NOT NULL,
    year_start          SMALLINT        NOT NULL,
    year_end            SMALLINT        NOT NULL,
    semester_label      TEXT            NOT NULL,   -- '1st Semester', '2nd Semester', 'Summer'
    full_label          TEXT            NOT NULL,   -- 'AY 2023-2024 1st Semester'
    sort_key            INTEGER         NOT NULL,   -- for ordering: year_start * 10 + semester
    is_current          BOOLEAN         NOT NULL DEFAULT FALSE,

    CONSTRAINT uq_gold_dim_time UNIQUE (academic_year, semester)
);

COMMENT ON TABLE gold.dim_time IS 'Time dimension — academic year and semester';

CREATE INDEX IF NOT EXISTS idx_gold_dim_time_sort
    ON gold.dim_time (sort_key);

-- ------------------------------------------------------------------------------
-- gold.dim_program
-- Program dimension — one row per canonical program.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gold.dim_program (
    program_id          SERIAL          PRIMARY KEY,
    program_code        TEXT            NOT NULL UNIQUE,
    program_name        TEXT            NOT NULL,
    college             TEXT            NOT NULL,
    department          TEXT,
    duration_years      SMALLINT        NOT NULL DEFAULT 4,
    is_active           BOOLEAN         NOT NULL DEFAULT TRUE
);

COMMENT ON TABLE gold.dim_program IS 'Program dimension — canonical list of programs and colleges';

CREATE INDEX IF NOT EXISTS idx_gold_dim_program_college
    ON gold.dim_program (college);

-- ------------------------------------------------------------------------------
-- gold.dim_year_level
-- Year level dimension with descriptive labels.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gold.dim_year_level (
    year_level_id       SERIAL          PRIMARY KEY,
    year_level          SMALLINT        NOT NULL UNIQUE,
    level_name          TEXT            NOT NULL,   -- 'Freshman', 'Sophomore', etc.
    is_irregular        BOOLEAN         NOT NULL DEFAULT FALSE
);

COMMENT ON TABLE gold.dim_year_level IS 'Year level dimension with readable labels';

-- Pre-populate year levels
INSERT INTO gold.dim_year_level (year_level, level_name, is_irregular) VALUES
    (1, 'Freshman',     FALSE),
    (2, 'Sophomore',    FALSE),
    (3, 'Junior',       FALSE),
    (4, 'Senior',       FALSE),
    (5, 'Super Senior', TRUE),
    (6, 'Extended',     TRUE)
ON CONFLICT (year_level) DO NOTHING;

-- ------------------------------------------------------------------------------
-- gold.fact_enrollment_metrics
-- Central fact table — one row per period + program + year_level + gender.
-- Aggregates both enrollment and outcomes for unified querying.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gold.fact_enrollment_metrics (
    metric_id               BIGSERIAL       PRIMARY KEY,

    -- Dimension keys
    time_id                 INTEGER         NOT NULL REFERENCES gold.dim_time(time_id),
    program_id              INTEGER         NOT NULL REFERENCES gold.dim_program(program_id),
    year_level_id           INTEGER         NOT NULL REFERENCES gold.dim_year_level(year_level_id),
    gender                  TEXT            CHECK (gender IN ('Male', 'Female', 'Other', 'Not Specified', 'All')),

    -- Enrollment metrics
    applicants              INTEGER         NOT NULL DEFAULT 0,
    accepted_applicants     INTEGER         NOT NULL DEFAULT 0,
    total_enrolled          INTEGER         NOT NULL DEFAULT 0,
    new_students            INTEGER         NOT NULL DEFAULT 0,
    transferees             INTEGER         NOT NULL DEFAULT 0,
    returnees               INTEGER         NOT NULL DEFAULT 0,

    -- Outcome metrics
    graduates               INTEGER         NOT NULL DEFAULT 0,
    dropouts                INTEGER         NOT NULL DEFAULT 0,
    shifters_out            INTEGER         NOT NULL DEFAULT 0,
    shifters_in             INTEGER         NOT NULL DEFAULT 0,

    -- Pre-computed KPI rates (avoids division on every dashboard query)
    acceptance_rate         NUMERIC(5,2),   -- accepted / applicants * 100
    dropout_rate            NUMERIC(5,2),   -- dropouts / total_enrolled * 100
    graduation_rate         NUMERIC(5,2),   -- graduates / total_enrolled * 100
    retention_rate          NUMERIC(5,2),   -- (enrolled - dropouts) / enrolled * 100
    net_shifter_balance     INTEGER         -- shifters_in - shifters_out

    -- Lineage
    ,refreshed_at           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_gold_fact UNIQUE (time_id, program_id, year_level_id, gender)
);

COMMENT ON TABLE gold.fact_enrollment_metrics IS 'Central fact table — enrollment and outcome metrics per period/program/year_level/gender';

CREATE INDEX IF NOT EXISTS idx_gold_fact_time
    ON gold.fact_enrollment_metrics (time_id);

CREATE INDEX IF NOT EXISTS idx_gold_fact_program
    ON gold.fact_enrollment_metrics (program_id);

CREATE INDEX IF NOT EXISTS idx_gold_fact_year_level
    ON gold.fact_enrollment_metrics (year_level_id);

-- Composite index for the most common dashboard filter pattern
CREATE INDEX IF NOT EXISTS idx_gold_fact_time_program
    ON gold.fact_enrollment_metrics (time_id, program_id);

-- GIN index for fast aggregation on gender
CREATE INDEX IF NOT EXISTS idx_gold_fact_gender
    ON gold.fact_enrollment_metrics USING GIN (gender gin_trgm_ops);

-- ------------------------------------------------------------------------------
-- gold.agg_program_performance
-- Pre-aggregated program-level summary across all years.
-- Refreshed every pipeline run. Powers the program comparison dashboard.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gold.agg_program_performance (
    agg_id                      BIGSERIAL   PRIMARY KEY,
    program_id                  INTEGER     NOT NULL REFERENCES gold.dim_program(program_id),
    time_id                     INTEGER     NOT NULL REFERENCES gold.dim_time(time_id),

    -- Totals
    total_applicants            INTEGER     NOT NULL DEFAULT 0,
    total_accepted              INTEGER     NOT NULL DEFAULT 0,
    total_enrolled              INTEGER     NOT NULL DEFAULT 0,
    total_graduates             INTEGER     NOT NULL DEFAULT 0,
    total_dropouts              INTEGER     NOT NULL DEFAULT 0,
    total_shifters_out          INTEGER     NOT NULL DEFAULT 0,
    total_shifters_in           INTEGER     NOT NULL DEFAULT 0,

    -- KPIs
    avg_acceptance_rate         NUMERIC(5,2),
    avg_graduation_rate         NUMERIC(5,2),
    avg_dropout_rate            NUMERIC(5,2),
    avg_retention_rate          NUMERIC(5,2),

    -- Trend indicators (vs previous semester)
    enrollment_change_pct       NUMERIC(6,2),
    dropout_change_pct          NUMERIC(6,2),

    refreshed_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_gold_agg_prog UNIQUE (program_id, time_id)
);

COMMENT ON TABLE gold.agg_program_performance IS 'Pre-aggregated program KPIs per semester — feeds program comparison dashboards';

CREATE INDEX IF NOT EXISTS idx_gold_agg_prog_time
    ON gold.agg_program_performance (time_id);

CREATE INDEX IF NOT EXISTS idx_gold_agg_prog_program
    ON gold.agg_program_performance (program_id);

-- ------------------------------------------------------------------------------
-- gold.agg_college_summary
-- College-level rollup. One row per college per semester.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gold.agg_college_summary (
    agg_id              BIGSERIAL   PRIMARY KEY,
    college             TEXT        NOT NULL,
    time_id             INTEGER     NOT NULL REFERENCES gold.dim_time(time_id),

    total_enrolled      INTEGER     NOT NULL DEFAULT 0,
    total_graduates     INTEGER     NOT NULL DEFAULT 0,
    total_dropouts      INTEGER     NOT NULL DEFAULT 0,
    program_count       INTEGER     NOT NULL DEFAULT 0,
    avg_dropout_rate    NUMERIC(5,2),
    avg_graduation_rate NUMERIC(5,2),

    refreshed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_gold_agg_college UNIQUE (college, time_id)
);

COMMENT ON TABLE gold.agg_college_summary IS 'College-level enrollment and outcome rollup per semester';

CREATE INDEX IF NOT EXISTS idx_gold_agg_college_time
    ON gold.agg_college_summary (time_id);

-- ------------------------------------------------------------------------------
-- gold.pipeline_run_log
-- Records every full pipeline execution for monitoring.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gold.pipeline_run_log (
    run_id          BIGSERIAL   PRIMARY KEY,
    run_label       TEXT,                       -- optional: 'SEM1_2024_manual'
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    status          TEXT        NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'success', 'failed', 'partial')),
    bronze_rows     INTEGER,
    silver_rows     INTEGER,
    gold_rows       INTEGER,
    error_message   TEXT,
    triggered_by    TEXT        NOT NULL DEFAULT 'manual'
);

COMMENT ON TABLE gold.pipeline_run_log IS 'Top-level audit log for every pipeline.py execution';


-- ==============================================================================
-- VIEWS (convenience shortcuts used by Metabase / reporting scripts)
-- ==============================================================================

CREATE OR REPLACE VIEW gold.v_enrollment_summary AS
SELECT
    dt.full_label               AS period,
    dt.academic_year,
    dt.semester,
    dp.college,
    dp.program_code,
    dp.program_name,
    dyl.level_name              AS year_level,
    f.gender,
    f.total_enrolled,
    f.new_students,
    f.transferees,
    f.returnees,
    f.graduates,
    f.dropouts,
    f.dropout_rate,
    f.graduation_rate,
    f.retention_rate,
    f.refreshed_at
FROM gold.fact_enrollment_metrics   f
JOIN gold.dim_time                  dt  ON dt.time_id     = f.time_id
JOIN gold.dim_program               dp  ON dp.program_id  = f.program_id
JOIN gold.dim_year_level            dyl ON dyl.year_level_id = f.year_level_id;

COMMENT ON VIEW gold.v_enrollment_summary IS 'Flattened fact table with all dimension labels — primary Metabase view';

CREATE OR REPLACE VIEW gold.v_program_kpis AS
SELECT
    dt.academic_year,
    dt.semester,
    dt.full_label                   AS period,
    dp.college,
    dp.program_code,
    dp.program_name,
    agg.total_enrolled,
    agg.total_graduates,
    agg.total_dropouts,
    agg.avg_acceptance_rate,
    agg.avg_graduation_rate,
    agg.avg_dropout_rate,
    agg.avg_retention_rate,
    agg.enrollment_change_pct,
    agg.dropout_change_pct
FROM gold.agg_program_performance   agg
JOIN gold.dim_time                  dt  ON dt.time_id    = agg.time_id
JOIN gold.dim_program               dp  ON dp.program_id = agg.program_id;

COMMENT ON VIEW gold.v_program_kpis IS 'Program KPI dashboard view — use this in Metabase program comparison charts';


-- ==============================================================================
-- GRANT PERMISSIONS
-- Replace 'neust_app' with your application DB user
-- ==============================================================================

-- GRANT USAGE ON SCHEMA bronze TO neust_app;
-- GRANT USAGE ON SCHEMA silver TO neust_app;
-- GRANT USAGE ON SCHEMA gold   TO neust_app;

-- GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA bronze TO neust_app;
-- GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA silver TO neust_app;
-- GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA gold   TO neust_app;

-- GRANT USAGE ON ALL SEQUENCES IN SCHEMA bronze TO neust_app;
-- GRANT USAGE ON ALL SEQUENCES IN SCHEMA silver TO neust_app;
-- GRANT USAGE ON ALL SEQUENCES IN SCHEMA gold   TO neust_app;


-- ==============================================================================
-- DONE
-- ==============================================================================
DO $$
BEGIN
    RAISE NOTICE '======================================================';
    RAISE NOTICE 'NEUST Analytics schema initialized successfully.';
    RAISE NOTICE 'Schemas created : bronze, silver, gold';
    RAISE NOTICE 'Tables created  : 14';
    RAISE NOTICE 'Views created   : 2';
    RAISE NOTICE '======================================================';
END $$;