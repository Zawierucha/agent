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