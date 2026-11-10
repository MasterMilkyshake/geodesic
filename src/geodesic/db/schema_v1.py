"""Schema version 1: the linked evidence database.

Conventions
-----------
* Text primary keys are stable internal IDs (type prefix + random hex). They
  survive re-import and appear unchanged in exports.
* Timestamps are ISO 8601 UTC with an explicit ``+00:00`` offset.
* Character spans are 0-based, half-open ``[start, end)`` code-point offsets
  into the *stored* text they cite. Page, row and column indices are 0-based.
* Evidence, decisions and reviews are never hard-deleted: DELETE is blocked by
  triggers, and removal is a recorded state change.
"""

from __future__ import annotations

import sqlite3

VERSION = 1
DESCRIPTION = "Initial linked evidence schema"

DDL = r"""
-- ---------------------------------------------------------------- project
CREATE TABLE project (
    id            TEXT PRIMARY KEY,
    singleton     INTEGER NOT NULL DEFAULT 1 UNIQUE CHECK (singleton = 1),
    name          TEXT NOT NULL,
    slug          TEXT NOT NULL,
    data_class    TEXT NOT NULL CHECK (data_class IN ('synthetic','real')),
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    app_version   TEXT NOT NULL
);

CREATE TABLE stage (
    id          TEXT PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    definition  TEXT NOT NULL DEFAULT '',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    position    INTEGER NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE protocol_version (
    id              TEXT PRIMARY KEY,
    version         INTEGER NOT NULL UNIQUE,
    status          TEXT NOT NULL CHECK (status IN ('provisional','confirmed')),
    content_json    TEXT NOT NULL,
    content_sha256  TEXT NOT NULL,
    change_summary  TEXT NOT NULL DEFAULT '',
    changed_parts_json TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    confirmed_at    TEXT,
    confirmed_by    TEXT
);

CREATE TABLE vocabulary_term (
    id              INTEGER PRIMARY KEY,
    field           TEXT NOT NULL,
    original_term   TEXT NOT NULL,
    normalized_term TEXT NOT NULL,
    note            TEXT,
    created_at      TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    UNIQUE (field, original_term)
);

-- ---------------------------------------------------------------- workbook
CREATE TABLE workbook_version (
    id                  TEXT PRIMARY KEY,
    workbook_key        TEXT NOT NULL,
    version_no          INTEGER NOT NULL,
    original_filename   TEXT NOT NULL,
    sha256              TEXT NOT NULL,
    size_bytes          INTEGER NOT NULL,
    stored_path         TEXT NOT NULL,
    declared_data_class TEXT NOT NULL CHECK (declared_data_class IN ('synthetic','real')),
    embedded_tag        TEXT,
    imported_at         TEXT NOT NULL,
    imported_by         TEXT NOT NULL,
    previous_version_id TEXT REFERENCES workbook_version(id),
    UNIQUE (workbook_key, sha256),
    UNIQUE (workbook_key, version_no)
);

CREATE TABLE source_cell (
    id                  INTEGER PRIMARY KEY,
    workbook_version_id TEXT NOT NULL REFERENCES workbook_version(id),
    sheet               TEXT NOT NULL,
    cell_ref            TEXT NOT NULL,
    row_idx             INTEGER NOT NULL,   -- 1-based spreadsheet row
    col_idx             INTEGER NOT NULL,   -- 1-based spreadsheet column
    raw_text            TEXT,               -- cell value exactly as stored text
    value_type          TEXT,               -- str | number | bool | date | formula | empty
    formula             TEXT,
    hyperlink           TEXT,
    comment             TEXT,
    comment_author      TEXT,
    merged_range        TEXT,               -- e.g. A3:A5 when the cell anchors/belongs to a merge
    is_merge_anchor     INTEGER NOT NULL DEFAULT 0,
    fill_color          TEXT,
    font_flags          TEXT,
    number_format       TEXT,
    UNIQUE (workbook_version_id, sheet, cell_ref)
);
CREATE INDEX ix_source_cell_row ON source_cell (workbook_version_id, sheet, row_idx);

CREATE TABLE import_run (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL,     -- workbook | search_file | document
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL CHECK (status IN ('running','complete','failed')),
    actor               TEXT NOT NULL,
    workbook_version_id TEXT REFERENCES workbook_version(id),
    mapping_sha256      TEXT,
    mapping_json        TEXT,
    parser_version      TEXT,
    stats_json          TEXT,
    notes               TEXT
);

CREATE TABLE workbook_row (
    id                  INTEGER PRIMARY KEY,
    workbook_version_id TEXT NOT NULL REFERENCES workbook_version(id),
    sheet               TEXT NOT NULL,
    row_idx             INTEGER NOT NULL,
    paper_id            TEXT REFERENCES paper(id),
    stage_keys_json     TEXT NOT NULL DEFAULT '[]',
    unknown_fields_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (workbook_version_id, sheet, row_idx)
);
CREATE INDEX ix_workbook_row_paper ON workbook_row (paper_id);

CREATE TABLE unparsed_content (
    id                  INTEGER PRIMARY KEY,
    import_run_id       TEXT REFERENCES import_run(id),
    workbook_version_id TEXT REFERENCES workbook_version(id),
    sheet               TEXT,
    cell_ref            TEXT,
    char_start          INTEGER,
    char_end            INTEGER,
    text                TEXT NOT NULL,
    reason              TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE parse_failure (
    id                  INTEGER PRIMARY KEY,
    import_run_id       TEXT REFERENCES import_run(id),
    context             TEXT NOT NULL,      -- workbook_import | manual_extraction | candidate_detection
    workbook_version_id TEXT,
    sheet               TEXT,
    cell_ref            TEXT,
    snapshot_id         TEXT,
    unit_key            TEXT,
    char_start          INTEGER,
    char_end            INTEGER,
    field               TEXT,
    claimed_literal     TEXT,
    reason              TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

-- ---------------------------------------------------------------- papers
CREATE TABLE paper (
    pk                  INTEGER PRIMARY KEY,
    id                  TEXT NOT NULL UNIQUE,
    canonical_id        TEXT REFERENCES paper(id),  -- non-NULL while grouped under a canonical record (reversible)
    record_status       TEXT NOT NULL DEFAULT 'active' CHECK (record_status IN ('active','absent_from_latest','removed')),
    resolution_status   TEXT NOT NULL CHECK (resolution_status IN ('resolved','unresolved')),
    origin              TEXT NOT NULL CHECK (origin IN ('workbook','search','file_import','manual')),
    identity_key        TEXT,
    workbook_key        TEXT,
    citation_text       TEXT,
    title               TEXT,
    title_norm          TEXT,
    authors_json        TEXT NOT NULL DEFAULT '[]',
    first_author        TEXT,
    year                INTEGER,
    journal             TEXT,
    publication_type    TEXT,     -- original term as supplied by the source
    language            TEXT,
    abstract            TEXT,
    doi                 TEXT,     -- normalized
    pmid                TEXT,
    pmcid               TEXT,
    other_ids_json      TEXT NOT NULL DEFAULT '{}',
    urls_json           TEXT NOT NULL DEFAULT '[]',
    publication_status  TEXT NOT NULL DEFAULT 'unknown' CHECK (publication_status IN ('preprint','published','unknown')),
    fulltext_availability TEXT NOT NULL DEFAULT 'not_checked'
        CHECK (fulltext_availability IN ('not_checked','open_access_reported','document_attached','abstract_only','not_available','retrieval_failed')),
    identity_status     TEXT NOT NULL DEFAULT 'unverified'
        CHECK (identity_status IN ('unverified','identifier_validated','reviewer_verified','conflict')),
    removed_reason      TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX ix_paper_doi ON paper (doi);
CREATE INDEX ix_paper_pmid ON paper (pmid);
CREATE INDEX ix_paper_identity ON paper (workbook_key, identity_key);
CREATE INDEX ix_paper_canonical ON paper (canonical_id);
CREATE INDEX ix_paper_year ON paper (year);
CREATE INDEX ix_paper_title_norm ON paper (title_norm);

CREATE TABLE paper_identifier (
    id                TEXT PRIMARY KEY,
    paper_id          TEXT NOT NULL REFERENCES paper(id),
    scheme            TEXT NOT NULL,     -- doi | pmid | pmcid | provider:<name> | other
    value_raw         TEXT NOT NULL,
    value_norm        TEXT NOT NULL,
    source            TEXT NOT NULL,     -- workbook | search_provider:<p> | file_import | metadata_lookup:<p> | reviewer
    validation_status TEXT NOT NULL DEFAULT 'unvalidated'
        CHECK (validation_status IN ('unvalidated','validated','invalid_syntax','not_found','conflict')),
    validated_via     TEXT,
    validated_at      TEXT,
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    UNIQUE (paper_id, scheme, value_norm)
);
CREATE INDEX ix_paper_identifier_value ON paper_identifier (scheme, value_norm);

CREATE TABLE metadata_conflict (
    id              TEXT PRIMARY KEY,
    paper_id        TEXT NOT NULL REFERENCES paper(id),
    field           TEXT NOT NULL,
    existing_value  TEXT,
    incoming_value  TEXT,
    incoming_source TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved_keep_existing','resolved_use_incoming','dismissed')),
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolved_by     TEXT
);
CREATE INDEX ix_metadata_conflict_paper ON metadata_conflict (paper_id);

CREATE TABLE integrity_check (
    id          TEXT PRIMARY KEY,
    paper_id    TEXT NOT NULL REFERENCES paper(id),
    source      TEXT NOT NULL,
    checked_on  TEXT NOT NULL,     -- UTC date
    result      TEXT NOT NULL CHECK (result IN ('none_found','correction','retraction','expression_of_concern','other_notice')),
    details     TEXT,
    notice_url  TEXT,
    recorded_by TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX ix_integrity_paper ON integrity_check (paper_id);

CREATE TABLE citation_candidate (
    id              TEXT PRIMARY KEY,
    paper_id        TEXT NOT NULL REFERENCES paper(id),
    provider        TEXT NOT NULL,
    looked_up_at    TEXT NOT NULL,
    query           TEXT NOT NULL,
    match_basis     TEXT NOT NULL CHECK (match_basis IN ('exact_identifier','bibliographic_similarity')),
    candidate_json  TEXT NOT NULL,
    candidate_doi   TEXT,
    score           REAL,
    status          TEXT NOT NULL CHECK (status IN ('proposed','auto_attached','accepted','rejected','not_found')),
    decided_by      TEXT,
    decided_at      TEXT,
    response_sha256 TEXT
);
CREATE INDEX ix_citation_candidate_paper ON citation_candidate (paper_id);

CREATE TABLE paper_relation (
    id                 TEXT PRIMARY KEY,
    paper_a            TEXT NOT NULL REFERENCES paper(id),
    paper_b            TEXT NOT NULL REFERENCES paper(id),
    relation_type      TEXT NOT NULL
        CHECK (relation_type IN ('duplicate','preprint_published','same_study_report','identifier_conflict')),
    status             TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate','confirmed','rejected')),
    method             TEXT NOT NULL,
    score              REAL,
    score_details_json TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL,
    created_by         TEXT NOT NULL,
    decided_at         TEXT,
    decided_by         TEXT,
    decision_note      TEXT,
    UNIQUE (paper_a, paper_b, relation_type)
);

CREATE TABLE paper_merge (
    id                 TEXT PRIMARY KEY,
    canonical_paper_id TEXT NOT NULL REFERENCES paper(id),
    member_paper_id    TEXT NOT NULL REFERENCES paper(id),
    basis              TEXT NOT NULL,   -- validated_identifier_match | reviewer_confirmed_duplicate
    relation_id        TEXT REFERENCES paper_relation(id),
    status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','reversed')),
    created_at         TEXT NOT NULL,
    created_by         TEXT NOT NULL,
    reversed_at        TEXT,
    reversed_by        TEXT,
    reversal_reason    TEXT
);
CREATE INDEX ix_paper_merge_member ON paper_merge (member_paper_id);

-- ---------------------------------------------------------------- search
CREATE TABLE search_run (
    id                    TEXT PRIMARY KEY,
    provider              TEXT NOT NULL,
    interface             TEXT NOT NULL,
    mode                  TEXT NOT NULL CHECK (mode IN ('live','fixture','file_import')),
    concept_query_json    TEXT,
    translated_query      TEXT,
    executed_query        TEXT NOT NULL,
    filters_json          TEXT NOT NULL DEFAULT '{}',
    started_at            TEXT NOT NULL,
    finished_at           TEXT,
    page_size             INTEGER,
    max_records           INTEGER,
    pages_retrieved       INTEGER NOT NULL DEFAULT 0,
    retrieved_count       INTEGER NOT NULL DEFAULT 0,
    provider_total        INTEGER,
    status                TEXT NOT NULL CHECK (status IN ('running','complete','truncated','incomplete','failed')),
    completeness_note     TEXT,
    errors_json           TEXT NOT NULL DEFAULT '[]',
    resume_cursor         TEXT,
    protocol_version_id   TEXT REFERENCES protocol_version(id),
    protocol_status_at_run TEXT,
    exploratory           INTEGER NOT NULL DEFAULT 0,
    client_ident          TEXT,
    source_file_name      TEXT,
    source_file_sha256    TEXT,
    declared_data_class   TEXT NOT NULL CHECK (declared_data_class IN ('synthetic','real')),
    actor                 TEXT NOT NULL
);

CREATE TABLE search_raw_response (
    id                  TEXT PRIMARY KEY,
    search_run_id       TEXT NOT NULL REFERENCES search_run(id),
    seq                 INTEGER NOT NULL,
    request_url         TEXT,          -- credentials and contact details redacted
    request_params_json TEXT,          -- credentials and contact details redacted
    http_status         INTEGER,
    stored_path         TEXT NOT NULL,
    sha256              TEXT NOT NULL,
    size_bytes          INTEGER NOT NULL,
    retrieved_at        TEXT NOT NULL,
    is_fixture          INTEGER NOT NULL,
    UNIQUE (search_run_id, seq)
);

CREATE TABLE search_result (
    id                 TEXT PRIMARY KEY,
    search_run_id      TEXT NOT NULL REFERENCES search_run(id),
    raw_response_id    TEXT REFERENCES search_raw_response(id),
    position           INTEGER NOT NULL,
    provider_record_id TEXT,
    record_sha256      TEXT NOT NULL,
    raw_json           TEXT NOT NULL,
    parsed_json        TEXT NOT NULL,
    paper_id           TEXT REFERENCES paper(id),
    link_method        TEXT,   -- new_paper | exact_duplicate_record | validated_identifier_match
    created_at         TEXT NOT NULL
);
CREATE INDEX ix_search_result_run ON search_result (search_run_id);
CREATE INDEX ix_search_result_paper ON search_result (paper_id);
CREATE INDEX ix_search_result_hash ON search_result (record_sha256);

-- ---------------------------------------------------------------- documents
CREATE TABLE document (
    id                  TEXT PRIMARY KEY,
    paper_id            TEXT NOT NULL REFERENCES paper(id),
    parent_document_id  TEXT REFERENCES document(id),
    role                TEXT NOT NULL CHECK (role IN ('main','supplementary')),
    format              TEXT NOT NULL CHECK (format IN ('pdf','xml','html')),
    original_filename   TEXT NOT NULL,
    sha256              TEXT NOT NULL,
    size_bytes          INTEGER NOT NULL,
    stored_path         TEXT NOT NULL,
    source              TEXT NOT NULL,   -- user_provided | open_access_api:<provider>
    source_url          TEXT,
    license             TEXT,
    retrieved_at        TEXT NOT NULL,
    declared_data_class TEXT NOT NULL CHECK (declared_data_class IN ('synthetic','real')),
    content_scope       TEXT NOT NULL DEFAULT 'full_text' CHECK (content_scope IN ('full_text','abstract_only','unknown')),
    record_status       TEXT NOT NULL DEFAULT 'active' CHECK (record_status IN ('active','removed')),
    added_by            TEXT NOT NULL,
    UNIQUE (paper_id, sha256)
);
CREATE INDEX ix_document_paper ON document (paper_id);

CREATE TABLE document_snapshot (
    id             TEXT PRIMARY KEY,
    document_id    TEXT NOT NULL REFERENCES document(id),
    snapshot_no    INTEGER NOT NULL,
    parser         TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('ok','partial','image_only','failed')),
    failure_reason TEXT,
    n_units        INTEGER NOT NULL DEFAULT 0,
    n_chars        INTEGER NOT NULL DEFAULT 0,
    n_tables       INTEGER NOT NULL DEFAULT 0,
    text_sha256    TEXT,
    ocr_applied    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (document_id, snapshot_no)
);

CREATE TABLE text_unit (
    id           INTEGER PRIMARY KEY,
    snapshot_id  TEXT NOT NULL REFERENCES document_snapshot(id),
    unit_kind    TEXT NOT NULL CHECK (unit_kind IN ('page','element')),
    unit_key     TEXT NOT NULL,    -- page index as text ("0", "1", ...) or stable element identifier
    seq          INTEGER NOT NULL,
    label        TEXT,
    section_path TEXT,
    text         TEXT NOT NULL,
    extraction_status TEXT NOT NULL DEFAULT 'ok' CHECK (extraction_status IN ('ok','no_text','failed')),
    has_images   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (snapshot_id, unit_key)
);

CREATE TABLE doc_table (
    id          INTEGER PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES document_snapshot(id),
    table_key   TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    label       TEXT,
    caption     TEXT,
    n_rows      INTEGER NOT NULL,
    n_cols      INTEGER NOT NULL,
    UNIQUE (snapshot_id, table_key)
);

CREATE TABLE doc_table_cell (
    table_id  INTEGER NOT NULL REFERENCES doc_table(id),
    row_idx   INTEGER NOT NULL,
    col_idx   INTEGER NOT NULL,
    text      TEXT NOT NULL,
    is_header INTEGER NOT NULL DEFAULT 0,
    rowspan   INTEGER NOT NULL DEFAULT 1,
    colspan   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (table_id, row_idx, col_idx)
);

-- ---------------------------------------------------------------- study hierarchy (sparse)
CREATE TABLE study (
    id            TEXT PRIMARY KEY,
    label         TEXT NOT NULL,
    description   TEXT,
    design        TEXT,
    record_status TEXT NOT NULL DEFAULT 'active' CHECK (record_status IN ('active','removed')),
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL
);

CREATE TABLE study_report (
    study_id   TEXT NOT NULL REFERENCES study(id),
    paper_id   TEXT NOT NULL REFERENCES paper(id),
    note       TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    PRIMARY KEY (study_id, paper_id)
);

CREATE TABLE experiment (
    id             TEXT PRIMARY KEY,
    paper_id       TEXT NOT NULL REFERENCES paper(id),
    study_id       TEXT REFERENCES study(id),
    label          TEXT NOT NULL,
    description    TEXT,
    system_context TEXT,
    design         TEXT,
    record_status  TEXT NOT NULL DEFAULT 'active' CHECK (record_status IN ('active','removed')),
    created_at     TEXT NOT NULL,
    created_by     TEXT NOT NULL
);

CREATE TABLE comparison_group (
    id            TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiment(id),
    label         TEXT NOT NULL,
    role          TEXT,
    description   TEXT,
    record_status TEXT NOT NULL DEFAULT 'active' CHECK (record_status IN ('active','removed')),
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL
);

-- ---------------------------------------------------------------- findings
CREATE TABLE finding (
    pk                   INTEGER PRIMARY KEY,
    id                   TEXT NOT NULL UNIQUE,
    paper_id             TEXT NOT NULL REFERENCES paper(id),
    study_id             TEXT REFERENCES study(id),                 -- NULL = explicitly unassigned
    experiment_id        TEXT REFERENCES experiment(id),            -- NULL = explicitly unassigned
    comparison_group_id  TEXT REFERENCES comparison_group(id),      -- NULL = explicitly unassigned
    kind                 TEXT NOT NULL CHECK (kind IN ('measurement','narrative','limitation')),
    origin               TEXT NOT NULL CHECK (origin IN ('workbook_import','manual_extraction','candidate_promotion')),
    extraction_method    TEXT NOT NULL,
    workbook_key         TEXT,
    match_key            TEXT,      -- re-import identity (content-derived; never row position)
    occurrence           INTEGER,
    record_status        TEXT NOT NULL DEFAULT 'active' CHECK (record_status IN ('active','absent_from_latest','removed')),
    review_status        TEXT NOT NULL DEFAULT 'unreviewed' CHECK (review_status IN ('draft','unreviewed','verified','rejected','stale')),
    grounding_status     TEXT NOT NULL CHECK (grounding_status IN ('grounded','unsupported')),
    source_scope         TEXT NOT NULL DEFAULT 'workbook_summary'
        CHECK (source_scope IN ('workbook_summary','full_text','abstract_only','supplementary')),
    review_flags_json    TEXT NOT NULL DEFAULT '[]',
    current_version      INTEGER NOT NULL DEFAULT 1,
    comparison_eligibility TEXT NOT NULL DEFAULT 'not_assessed'
        CHECK (comparison_eligibility IN ('not_assessed','descriptive_only','quantitative_candidate')),
    -- content (original wording and transcribed literals are never rewritten by normalization)
    source_wording       TEXT NOT NULL,
    statement_type       TEXT NOT NULL CHECK (statement_type IN ('exact','approximate','range','bound','qualitative','unparsed')),
    system_context       TEXT,
    experimental_context TEXT,
    intervention         TEXT,
    comparator           TEXT,
    outcome              TEXT,
    measurement_method   TEXT,
    observation_type     TEXT CHECK (observation_type IS NULL OR observation_type IN ('direct_measurement','proxy_signal','functional_outcome')),
    value_literal        TEXT,
    value_num            REAL,     -- point value only (exact or approximate); never a range midpoint or a bound
    value_low            REAL,     -- lower end of a range, or the limit of a lower bound
    value_high           REAL,     -- upper end of a range, or the limit of an upper bound
    bound_type           TEXT CHECK (bound_type IS NULL OR bound_type IN ('gt','ge','lt','le')),
    unit_original        TEXT,
    relative_semantics   TEXT CHECK (relative_semantics IS NULL OR relative_semantics IN
        ('absolute','percent_of_reference','percent_of_reference_per_mass_or_volume','fold_change',
         'percent_of_counted_population','arbitrary_units','unspecified_relative')),
    reference_definition TEXT,
    timepoint_literal    TEXT,
    timepoint_value      REAL,
    time_unit            TEXT,
    observation_kind     TEXT CHECK (observation_kind IS NULL OR observation_kind IN ('single_observation','summary_statistic')),
    summary_statistic    TEXT,
    sample_size          INTEGER,
    sample_size_literal  TEXT,
    replicate_type       TEXT,
    uncertainty_type     TEXT,     -- sd | se | ci | iqr | range | unspecified_plus_minus | other
    uncertainty_literal  TEXT,
    uncertainty_value    REAL,
    uncertainty_low      REAL,
    uncertainty_high     REAL,
    confidence_level     REAL,
    denominator          TEXT,
    normalization        TEXT,
    qualifiers_json      TEXT NOT NULL DEFAULT '[]',
    interpretation_limits TEXT,
    field_status_json    TEXT NOT NULL DEFAULT '{}',   -- field -> not_reported | not_applicable | not_yet_extracted | could_not_be_parsed
    spans_json           TEXT NOT NULL DEFAULT '{}',   -- field -> [start, end) within the cited source text unit
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    created_by           TEXT NOT NULL
);
CREATE INDEX ix_finding_paper ON finding (paper_id);
CREATE INDEX ix_finding_match ON finding (workbook_key, match_key);
CREATE INDEX ix_finding_status ON finding (record_status, review_status);
CREATE INDEX ix_finding_kind ON finding (kind, statement_type);
CREATE INDEX ix_finding_outcome ON finding (outcome);
CREATE INDEX ix_finding_system ON finding (system_context);
CREATE INDEX ix_finding_experiment ON finding (experiment_id);

CREATE TABLE finding_version (
    id             TEXT PRIMARY KEY,
    finding_id     TEXT NOT NULL REFERENCES finding(id),
    version_no     INTEGER NOT NULL,
    change_kind    TEXT NOT NULL,   -- imported | reimport_modified | review_edit | manual_create | candidate_promotion
    snapshot_json  TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    actor_kind     TEXT NOT NULL,
    note           TEXT,
    UNIQUE (finding_id, version_no)
);

CREATE TABLE source_location (
    id                  TEXT PRIMARY KEY,
    owner_type          TEXT NOT NULL CHECK (owner_type IN ('finding','paper','screening_decision')),
    owner_id            TEXT NOT NULL,
    owner_version_no    INTEGER,
    role                TEXT NOT NULL,   -- supporting_excerpt | citation | limitation | context:<field>
    kind                TEXT NOT NULL CHECK (kind IN ('workbook_cell','pdf_page','xml_element','html_element','table_cell')),
    workbook_version_id TEXT REFERENCES workbook_version(id),
    sheet               TEXT,
    cell_ref            TEXT,
    snapshot_id         TEXT REFERENCES document_snapshot(id),
    unit_key            TEXT,
    table_key           TEXT,
    row_idx             INTEGER,
    col_idx             INTEGER,
    char_start          INTEGER,
    char_end            INTEGER,
    excerpt             TEXT,
    resolves            INTEGER NOT NULL,   -- 1 when the stored text at this location equals ``excerpt``
    resolution_note     TEXT,
    is_current          INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL
);
CREATE INDEX ix_source_location_owner ON source_location (owner_type, owner_id);
CREATE INDEX ix_source_location_cell ON source_location (workbook_version_id, sheet, cell_ref);
CREATE INDEX ix_source_location_snapshot ON source_location (snapshot_id, unit_key);

CREATE TABLE finding_stage (
    id                INTEGER PRIMARY KEY,
    finding_id        TEXT NOT NULL REFERENCES finding(id),
    stage_id          TEXT NOT NULL REFERENCES stage(id),
    assignment_source TEXT NOT NULL CHECK (assignment_source IN ('imported','reviewer','suggested')),
    status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suggested','removed','dismissed')),
    assigned_by       TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (finding_id, stage_id, assignment_source)
);
CREATE INDEX ix_finding_stage_stage ON finding_stage (stage_id, status);

CREATE TABLE paper_stage (
    id                INTEGER PRIMARY KEY,
    paper_id          TEXT NOT NULL REFERENCES paper(id),
    stage_id          TEXT NOT NULL REFERENCES stage(id),
    assignment_source TEXT NOT NULL CHECK (assignment_source IN ('imported','reviewer','suggested')),
    status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suggested','removed','dismissed')),
    assigned_by       TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (paper_id, stage_id, assignment_source)
);
CREATE INDEX ix_paper_stage_stage ON paper_stage (stage_id, status);

CREATE TABLE finding_relation (
    id            TEXT PRIMARY KEY,
    finding_a     TEXT NOT NULL REFERENCES finding(id),
    finding_b     TEXT NOT NULL REFERENCES finding(id),
    relation_type TEXT NOT NULL CHECK (relation_type IN ('possible_duplicate','dependent','limited_by','ambiguous_identity')),
    status        TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate','confirmed','rejected')),
    method        TEXT NOT NULL,
    score         REAL,
    details_json  TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    decided_at    TEXT,
    decided_by    TEXT,
    UNIQUE (finding_a, finding_b, relation_type)
);
CREATE INDEX ix_finding_relation_b ON finding_relation (finding_b);

CREATE TABLE derived_value (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('normalized','derived')),
    rule_id      TEXT NOT NULL,
    formula      TEXT NOT NULL,
    inputs_json  TEXT NOT NULL,   -- [{finding_id, version_no, field, source_literal, value, unit}]
    output_value REAL,
    output_low   REAL,
    output_high  REAL,
    output_unit  TEXT NOT NULL,
    carries_json TEXT NOT NULL DEFAULT '{}',  -- statement type / qualifiers carried from the inputs
    status       TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current','stale','withdrawn')),
    stale_reason TEXT,
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL
);

CREATE TABLE derived_value_input (
    derived_value_id TEXT NOT NULL REFERENCES derived_value(id),
    finding_id       TEXT NOT NULL REFERENCES finding(id),
    PRIMARY KEY (derived_value_id, finding_id)
);
CREATE INDEX ix_derived_input_finding ON derived_value_input (finding_id);

CREATE TABLE numeric_candidate (
    id               TEXT PRIMARY KEY,
    snapshot_id      TEXT NOT NULL REFERENCES document_snapshot(id),
    paper_id         TEXT NOT NULL REFERENCES paper(id),
    location_kind    TEXT NOT NULL,
    unit_key         TEXT,
    table_key        TEXT,
    row_idx          INTEGER,
    col_idx          INTEGER,
    char_start       INTEGER NOT NULL,
    char_end         INTEGER NOT NULL,
    literal          TEXT NOT NULL,
    parsed_json      TEXT NOT NULL,
    context_start    INTEGER,
    context_end      INTEGER,
    context_excerpt  TEXT,
    detector         TEXT NOT NULL,
    detector_version TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','promoted','dismissed')),
    finding_id       TEXT REFERENCES finding(id),
    created_at       TEXT NOT NULL,
    resolved_at      TEXT,
    resolved_by      TEXT
);
CREATE INDEX ix_numeric_candidate_snapshot ON numeric_candidate (snapshot_id, status);

-- ---------------------------------------------------------------- screening and review
CREATE TABLE screening_suggestion (
    id                  TEXT PRIMARY KEY,
    paper_id            TEXT NOT NULL REFERENCES paper(id),
    stage               TEXT NOT NULL CHECK (stage IN ('title_abstract','full_text')),
    suggested_decision  TEXT NOT NULL CHECK (suggested_decision IN ('include','exclude','uncertain')),
    reason_kind         TEXT NOT NULL CHECK (reason_kind IN ('meets_criteria','criterion_not_met','insufficient_information','other')),
    criterion_id        TEXT,
    rationale           TEXT NOT NULL,
    supporting_text     TEXT,
    generator           TEXT NOT NULL,
    protocol_version_id TEXT NOT NULL REFERENCES protocol_version(id),
    protocol_status_at_suggestion TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','accepted','dismissed','superseded')),
    resolved_at         TEXT,
    resolved_by         TEXT,
    decision_id         TEXT
);
CREATE INDEX ix_screening_suggestion_paper ON screening_suggestion (paper_id, stage, status);

CREATE TABLE screening_decision (
    id                  TEXT PRIMARY KEY,
    paper_id            TEXT NOT NULL REFERENCES paper(id),
    stage               TEXT NOT NULL CHECK (stage IN ('title_abstract','full_text')),
    decision            TEXT NOT NULL CHECK (decision IN ('include','exclude','uncertain')),
    reason_kind         TEXT NOT NULL CHECK (reason_kind IN ('meets_criteria','criterion_not_met','insufficient_information','other')),
    criterion_id        TEXT,
    reason_text         TEXT,
    reviewer            TEXT NOT NULL,
    decider_kind        TEXT NOT NULL CHECK (decider_kind IN ('human','automated','simulated')),
    decision_role       TEXT NOT NULL DEFAULT 'independent' CHECK (decision_role IN ('independent','adjudication')),
    finality            TEXT NOT NULL CHECK (finality IN ('final','provisional')),
    decided_at          TEXT NOT NULL,
    protocol_version_id TEXT NOT NULL REFERENCES protocol_version(id),
    protocol_status_at_decision TEXT NOT NULL,
    supporting_text     TEXT,
    supporting_location_json TEXT,
    suggestion_id       TEXT REFERENCES screening_suggestion(id),
    supersedes_id       TEXT REFERENCES screening_decision(id),
    is_current          INTEGER NOT NULL DEFAULT 1,
    needs_rereview      INTEGER NOT NULL DEFAULT 0,
    rereview_reason     TEXT
);
CREATE INDEX ix_screening_decision_paper ON screening_decision (paper_id, stage, is_current);

CREATE TABLE paper_assessment (
    id                  TEXT PRIMARY KEY,
    paper_id            TEXT NOT NULL REFERENCES paper(id),
    dimension           TEXT NOT NULL CHECK (dimension IN ('primary_relevance','quantitative_comparison','mathematical_modeling')),
    value               TEXT NOT NULL,
    rationale           TEXT,
    reviewer            TEXT NOT NULL,
    decider_kind        TEXT NOT NULL,
    protocol_version_id TEXT REFERENCES protocol_version(id),
    created_at          TEXT NOT NULL,
    is_current          INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX ix_paper_assessment_paper ON paper_assessment (paper_id, dimension, is_current);

CREATE TABLE extraction_review (
    id                 TEXT PRIMARY KEY,
    finding_id         TEXT NOT NULL REFERENCES finding(id),
    finding_version_no INTEGER NOT NULL,
    reviewer           TEXT NOT NULL,
    reviewer_kind      TEXT NOT NULL CHECK (reviewer_kind IN ('human','automated','simulated')),
    action             TEXT NOT NULL CHECK (action IN ('verify','reject','edit','comment','reopen')),
    checks_json        TEXT NOT NULL DEFAULT '{}',
    note               TEXT,
    created_at         TEXT NOT NULL,
    is_stale           INTEGER NOT NULL DEFAULT 0,
    stale_reason       TEXT,
    stale_at           TEXT
);
CREATE INDEX ix_extraction_review_finding ON extraction_review (finding_id);

CREATE TABLE validation_run (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    actor      TEXT NOT NULL,
    n_findings INTEGER NOT NULL,
    n_issues   INTEGER NOT NULL,
    rules_version TEXT NOT NULL
);

CREATE TABLE validation_issue (
    id         INTEGER PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES validation_run(id),
    finding_id TEXT NOT NULL REFERENCES finding(id),
    code       TEXT NOT NULL,
    severity   TEXT NOT NULL CHECK (severity IN ('info','warning','error')),
    field      TEXT,
    message    TEXT NOT NULL
);
CREATE INDEX ix_validation_issue_run ON validation_issue (run_id, code);
CREATE INDEX ix_validation_issue_finding ON validation_issue (finding_id);

CREATE TABLE comparison_set (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    description      TEXT,
    finding_ids_json TEXT NOT NULL,
    assessment_json  TEXT NOT NULL,
    classification   TEXT NOT NULL CHECK (classification IN ('descriptive_only','quantitative_candidate')),
    created_at       TEXT NOT NULL,
    created_by       TEXT NOT NULL
);

-- ---------------------------------------------------------------- audit (append-only, hash-chained)
CREATE TABLE audit_event (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    actor_kind   TEXT NOT NULL,
    action       TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    entity_id    TEXT,
    details_json TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL
);
CREATE INDEX ix_audit_entity ON audit_event (entity_type, entity_id);
CREATE INDEX ix_audit_action ON audit_event (action);

-- ---------------------------------------------------------------- full-text search indexes
CREATE VIRTUAL TABLE finding_fts USING fts5(
    source_wording, outcome, system_context, experimental_context, intervention, comparator,
    measurement_method, interpretation_limits,
    content='finding', content_rowid='pk', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER finding_fts_ai AFTER INSERT ON finding BEGIN
    INSERT INTO finding_fts(rowid, source_wording, outcome, system_context, experimental_context, intervention,
                            comparator, measurement_method, interpretation_limits)
    VALUES (new.pk, new.source_wording, new.outcome, new.system_context, new.experimental_context, new.intervention,
            new.comparator, new.measurement_method, new.interpretation_limits);
END;
CREATE TRIGGER finding_fts_au AFTER UPDATE OF source_wording, outcome, system_context, experimental_context,
        intervention, comparator, measurement_method, interpretation_limits ON finding BEGIN
    INSERT INTO finding_fts(finding_fts, rowid, source_wording, outcome, system_context, experimental_context,
                            intervention, comparator, measurement_method, interpretation_limits)
    VALUES ('delete', old.pk, old.source_wording, old.outcome, old.system_context, old.experimental_context,
            old.intervention, old.comparator, old.measurement_method, old.interpretation_limits);
    INSERT INTO finding_fts(rowid, source_wording, outcome, system_context, experimental_context, intervention,
                            comparator, measurement_method, interpretation_limits)
    VALUES (new.pk, new.source_wording, new.outcome, new.system_context, new.experimental_context, new.intervention,
            new.comparator, new.measurement_method, new.interpretation_limits);
END;

CREATE VIRTUAL TABLE paper_fts USING fts5(
    title, abstract, citation_text, authors_json, journal,
    content='paper', content_rowid='pk', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER paper_fts_ai AFTER INSERT ON paper BEGIN
    INSERT INTO paper_fts(rowid, title, abstract, citation_text, authors_json, journal)
    VALUES (new.pk, new.title, new.abstract, new.citation_text, new.authors_json, new.journal);
END;
CREATE TRIGGER paper_fts_au AFTER UPDATE OF title, abstract, citation_text, authors_json, journal ON paper BEGIN
    INSERT INTO paper_fts(paper_fts, rowid, title, abstract, citation_text, authors_json, journal)
    VALUES ('delete', old.pk, old.title, old.abstract, old.citation_text, old.authors_json, old.journal);
    INSERT INTO paper_fts(rowid, title, abstract, citation_text, authors_json, journal)
    VALUES (new.pk, new.title, new.abstract, new.citation_text, new.authors_json, new.journal);
END;
"""

# Tables whose rows may never be hard-deleted.
NO_DELETE_TABLES = [
    "project", "stage", "protocol_version", "vocabulary_term",
    "workbook_version", "source_cell", "import_run", "workbook_row", "unparsed_content", "parse_failure",
    "paper", "paper_identifier", "metadata_conflict", "integrity_check", "citation_candidate",
    "paper_relation", "paper_merge",
    "search_run", "search_raw_response", "search_result",
    "document", "document_snapshot", "text_unit", "doc_table", "doc_table_cell",
    "study", "study_report", "experiment", "comparison_group",
    "finding", "finding_version", "source_location", "finding_stage", "paper_stage", "finding_relation",
    "derived_value", "derived_value_input", "numeric_candidate",
    "screening_suggestion", "screening_decision", "paper_assessment", "extraction_review",
    "validation_run", "validation_issue", "comparison_set", "audit_event",
]

# Tables that are fully append-only (no UPDATE at all).
NO_UPDATE_TABLES = [
    "audit_event", "finding_version", "source_cell", "search_raw_response", "text_unit",
    "doc_table", "doc_table_cell", "integrity_check", "validation_run", "validation_issue", "comparison_set",
]

# Columns that are immutable once written (table -> columns).
IMMUTABLE_COLUMNS = {
    "project": ["id", "data_class", "created_at", "created_by"],
    "protocol_version": ["version", "content_json", "content_sha256", "created_at", "created_by"],
    "workbook_version": ["workbook_key", "version_no", "original_filename", "sha256", "size_bytes", "stored_path",
                         "declared_data_class", "imported_at"],
    "search_result": ["search_run_id", "position", "provider_record_id", "record_sha256", "raw_json"],
    "search_run": ["provider", "interface", "mode", "executed_query", "concept_query_json", "translated_query",
                   "filters_json", "started_at", "protocol_version_id", "declared_data_class"],
    "document": ["paper_id", "format", "sha256", "stored_path", "source", "retrieved_at", "declared_data_class"],
    "document_snapshot": ["document_id", "snapshot_no", "parser", "parser_version", "created_at", "status",
                          "failure_reason", "text_sha256"],
    "screening_decision": ["paper_id", "stage", "decision", "reason_kind", "criterion_id", "reason_text", "reviewer",
                           "decider_kind", "decision_role", "finality", "decided_at", "protocol_version_id",
                           "protocol_status_at_decision", "supporting_text", "suggestion_id", "supersedes_id"],
    "screening_suggestion": ["paper_id", "stage", "suggested_decision", "reason_kind", "criterion_id", "rationale",
                             "generator", "protocol_version_id", "created_at"],
    "extraction_review": ["finding_id", "finding_version_no", "reviewer", "reviewer_kind", "action", "checks_json",
                          "note", "created_at"],
    "paper_assessment": ["paper_id", "dimension", "value", "rationale", "reviewer", "created_at"],
    "finding": ["id", "paper_id", "origin", "created_at", "created_by"],
    "paper": ["id", "origin", "created_at"],
    "source_location": ["owner_type", "owner_id", "kind", "workbook_version_id", "sheet", "cell_ref", "snapshot_id",
                        "unit_key", "table_key", "row_idx", "col_idx", "char_start", "char_end", "excerpt",
                        "created_at"],
    "derived_value": ["kind", "rule_id", "formula", "inputs_json", "output_value", "output_low", "output_high",
                      "output_unit", "created_at", "created_by"],
}


def _protective_triggers() -> list[str]:
    stmts: list[str] = []
    for table in NO_DELETE_TABLES:
        stmts.append(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, 'hard delete is not permitted on {table}; record a state change instead'); END;"
        )
    for table in NO_UPDATE_TABLES:
        stmts.append(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, '{table} is append-only'); END;"
        )
    for table, cols in IMMUTABLE_COLUMNS.items():
        stmts.append(
            f"CREATE TRIGGER {table}_immutable_cols BEFORE UPDATE OF {', '.join(cols)} ON {table} BEGIN "
            f"SELECT RAISE(ABORT, 'immutable column on {table} cannot be changed'); END;"
        )
    # A confirmed protocol version can never return to provisional.
    stmts.append(
        "CREATE TRIGGER protocol_version_no_unconfirm BEFORE UPDATE OF status ON protocol_version "
        "WHEN old.status = 'confirmed' BEGIN "
        "SELECT RAISE(ABORT, 'a confirmed protocol version cannot change status; create a new version'); END;"
    )
    return stmts


def upgrade(conn: sqlite3.Connection) -> None:
    """Create all tables, indexes and protective triggers."""
    conn.executescript(DDL)
    for stmt in _protective_triggers():
        conn.execute(stmt)
