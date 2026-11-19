CREATE TABLE IF NOT EXISTS geo_datasets (
    gse_id          VARCHAR PRIMARY KEY,
    title           VARCHAR,
    summary         VARCHAR,
    overall_design  VARCHAR,
    organism        VARCHAR,
    platform        VARCHAR,
    n_samples       INTEGER,
    discovered_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS geo_samples (
    gsm_id           VARCHAR PRIMARY KEY,
    gse_id           VARCHAR NOT NULL,
    title            VARCHAR,
    summary          VARCHAR,
    source_name      VARCHAR,
    organism         VARCHAR,
    platform         VARCHAR,
    characteristics  JSON,
    patient_id       VARCHAR,
    timepoint        VARCHAR,
    timepoint_value  VARCHAR,
    timepoint_unit   VARCHAR,
    is_longitudinal  BOOLEAN,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (gse_id) REFERENCES geo_datasets(gse_id)
);

CREATE TABLE IF NOT EXISTS geo_sample_characteristics (
    gsm_id          VARCHAR NOT NULL,
    characteristic  VARCHAR NOT NULL,
    value           VARCHAR,
    raw_value       VARCHAR,
    PRIMARY KEY (gsm_id, characteristic),
    FOREIGN KEY (gsm_id) REFERENCES geo_samples(gsm_id)
);

CREATE TABLE IF NOT EXISTS geo_sample_metadata (
    gsm_id              VARCHAR PRIMARY KEY,
    disease              VARCHAR,
    health_state         VARCHAR,
    tissue               VARCHAR,
    cell_type            VARCHAR,
    age                  VARCHAR,
    sex                  VARCHAR,
    treatment            VARCHAR,
    treatment_response   VARCHAR,
    FOREIGN KEY (gsm_id) REFERENCES geo_samples(gsm_id)
);

CREATE TABLE IF NOT EXISTS geo_qualification (
    gse_id           VARCHAR PRIMARY KEY,
    qualification    VARCHAR,
    is_human         BOOLEAN,
    is_single_cell   BOOLEAN,
    is_ibd_related   BOOLEAN,
    disease          VARCHAR,
    modality         VARCHAR,
    tissue           VARCHAR,
    confidence       VARCHAR,
    evidence         VARCHAR,
    uncertainties    VARCHAR,
    qualified_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (gse_id) REFERENCES geo_datasets(gse_id)
);

CREATE TABLE IF NOT EXISTS geo_sample_qualification (
    gsm_id              VARCHAR PRIMARY KEY,
    qualification       VARCHAR NOT NULL CHECK (qualification IN ('include', 'exclude', 'uncertain')),
    tissue_source       VARCHAR,
    disease_status      VARCHAR,
    modality_evidence   VARCHAR,
    evidence            VARCHAR,
    uncertainties       VARCHAR,
    qualified_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (gsm_id) REFERENCES geo_samples(gsm_id)
);

CREATE TABLE IF NOT EXISTS geo_anndata_import_runs (
    run_id                  VARCHAR PRIMARY KEY,
    gse_id                  VARCHAR NOT NULL,
    schema_id               VARCHAR NOT NULL,
    schema_version          VARCHAR NOT NULL,
    schema_sha256           VARCHAR NOT NULL,
    status                  VARCHAR NOT NULL,
    iteration_count         INTEGER NOT NULL DEFAULT 0,
    max_iterations          INTEGER NOT NULL DEFAULT 10,
    output_directory        VARCHAR,
    summary_json            JSON,
    last_error              VARCHAR,
    started_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at            TIMESTAMP,
    FOREIGN KEY (gse_id) REFERENCES geo_datasets(gse_id)
);

CREATE TABLE IF NOT EXISTS geo_anndata_import_files (
    run_id                  VARCHAR NOT NULL,
    source_relative_path    VARCHAR NOT NULL,
    source_sha256           VARCHAR,
    format_guess            VARCHAR,
    adapter_name            VARCHAR,
    status                  VARCHAR NOT NULL,
    attempt_count           INTEGER NOT NULL DEFAULT 0,
    error_category          VARCHAR,
    last_error              VARCHAR,
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, source_relative_path),
    FOREIGN KEY (run_id) REFERENCES geo_anndata_import_runs(run_id)
);

CREATE TABLE IF NOT EXISTS geo_anndata_import_attempts (
    run_id                  VARCHAR NOT NULL,
    iteration_number        INTEGER NOT NULL,
    source_relative_path    VARCHAR NOT NULL,
    gsm_id                  VARCHAR NOT NULL DEFAULT '',
    adapter_name            VARCHAR NOT NULL,
    status                  VARCHAR NOT NULL,
    error_category          VARCHAR,
    error_message           VARCHAR,
    adapter_path            VARCHAR,
    started_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at            TIMESTAMP,
    PRIMARY KEY (run_id, iteration_number, source_relative_path, gsm_id, adapter_name),
    FOREIGN KEY (run_id) REFERENCES geo_anndata_import_runs(run_id)
);

CREATE TABLE IF NOT EXISTS geo_anndata_import_samples (
    run_id                  VARCHAR NOT NULL,
    gsm_id                  VARCHAR NOT NULL,
    status                  VARCHAR NOT NULL,
    adapter_name            VARCHAR,
    output_path             VARCHAR,
    n_obs                   BIGINT,
    n_vars                  BIGINT,
    error_category          VARCHAR,
    last_error              VARCHAR,
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, gsm_id),
    FOREIGN KEY (run_id) REFERENCES geo_anndata_import_runs(run_id),
    FOREIGN KEY (gsm_id) REFERENCES geo_samples(gsm_id)
);
