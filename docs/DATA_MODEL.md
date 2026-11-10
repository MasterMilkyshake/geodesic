# Data model

The database is one SQLite file per project (`geodesic.sqlite`). The complete
definition, with every column, check constraint and trigger, is
`src/geodesic/db/schema_v1.py`. This document describes how the tables fit
together and the conventions they share.

## Conventions

- **Identifiers.** Text primary keys are a type prefix plus 80 random bits in
  hex, for example `pap_…` (paper), `fnd_…` (finding), `prj_…` (project),
  `stg_…` (stage). They never encode content or position, survive re-import,
  and appear unchanged in exports.
- **Timestamps** are ISO 8601 in UTC with an explicit `+00:00` offset. Dates
  of integrity checks are UTC dates.
- **Spans** are 0-based, half-open `[start, end)` offsets in Unicode code
  points into the stored text they cite. See `SOURCE_LOCATIONS.md`.
- **No hard deletes.** A trigger blocks `DELETE` on every table listed below.
  Removal is a state change: `record_status`, `status` or `active`.
- **Append-only tables** also block `UPDATE`: `audit_event`,
  `finding_version`, `source_cell`, `search_raw_response`, `text_unit`,
  `doc_table`, `doc_table_cell`, `integrity_check`, `validation_run`,
  `validation_issue`, `comparison_set`.
- **Immutable columns.** On other tables the columns that carry identity,
  provenance or the content of a decision cannot be updated, for example
  `project.data_class`, the content and hash of a `protocol_version`, the
  file hash of a `workbook_version` or `document`, every content column of a
  `screening_decision`, and the address and excerpt of a `source_location`.
  A confirmed protocol version cannot return to provisional.
- **Original and normalized terms.** Categorical fields hold the original
  term as written. Normalized terms live in `vocabulary_term` and are applied
  only when tables are grouped.

## Tables

### Project and protocol

| Table | Content |
|---|---|
| `project` | One row: name, data class (`synthetic` or `real`, immutable), creator, application version |
| `stage` | Project-defined top-level categories loaded from `project.yaml`. A stage removed from the configuration is deactivated, not deleted |
| `protocol_version` | Each version of the protocol: content, content hash, status (`provisional` or `confirmed`), what changed, who confirmed it and when |
| `vocabulary_term` | Reviewer-made mapping of one original term of one field to a normalized term |

### Workbook import

| Table | Content |
|---|---|
| `workbook_version` | One row per imported workbook file: key, version number, original file name, SHA-256, size, stored path, declared data class |
| `source_cell` | Every non-empty cell of each version: sheet, cell reference, text, hyperlink, comment, merge membership, basic formatting |
| `import_run` | One row per import: mapping hash and content, counts, status |
| `workbook_row` | Evidence rows with their paper and unmapped fields |
| `unparsed_content` | Content the importer could not interpret, with the reason |
| `parse_failure` | Refused transcriptions and parser failures, with context |

### Papers

| Table | Content |
|---|---|
| `paper` | One bibliographic record. `resolution_status` (`resolved`, `unresolved`), `identity_status` (`unverified`, `identifier_validated`, `reviewer_verified`, `conflict`), `publication_status` (`preprint`, `published`, `unknown`), `fulltext_availability`, `origin` (`workbook`, `search`, `file_import`, `manual`), and `canonical_id`, which is set while the record is grouped under another record |
| `paper_identifier` | Each identifier with its scheme, source and `validation_status` (`unvalidated`, `validated`, `invalid_syntax`, `not_found`, `conflict`) |
| `metadata_conflict` | Incoming metadata that differs from stored metadata. Nothing is overwritten; a reviewer resolves it |
| `integrity_check` | Correction and retraction information: source, date checked, result (`none_found`, `correction`, `retraction`, `expression_of_concern`, `other_notice`) |
| `citation_candidate` | Registry records proposed for an unresolved citation, awaiting a reviewer |
| `paper_relation` | Relations between records: `duplicate`, `preprint_published`, `same_study_report`, `identifier_conflict`, each `candidate`, `confirmed` or `rejected`, with score and components |
| `paper_merge` | Reversible groupings of a member record under a canonical record |

Three separate statuses answer three separate questions: whether a record
carries a syntactically valid identifier (`resolution_status`), whether its
bibliographic identity was checked (`identity_status`), and whether each
identifier was validated (`paper_identifier.validation_status`). None of them
says anything about whether findings attributed to the paper are correct.

### Search

| Table | Content |
|---|---|
| `search_run` | Provider, interface, mode (`live`, `fixture`, `file_import`), executed query, concept groups, filters, limits, counts, cursor, protocol version and status at the time, declared data class |
| `search_raw_response` | One row per stored raw response: file path, SHA-256, size, request description without credentials |
| `search_result` | Each retrieved record in order: provider record ID, record hash, the record as supplied, the paper it was linked to and how. Membership survives deduplication |

### Documents

| Table | Content |
|---|---|
| `document` | A file attached to a paper: format, role (`main`, `supplementary`), SHA-256, stored path, source, licence, declared data class, status |
| `document_snapshot` | One parse of a document: parser and version, status, text hash |
| `text_unit` | Text units of a snapshot (PDF pages, XML elements) |
| `doc_table`, `doc_table_cell` | Tables of a snapshot with addressable cells |

### Hierarchy (sparse)

| Table | Content |
|---|---|
| `study` | A study, described by a reviewer |
| `study_report` | Which papers report a study |
| `experiment` | An experiment within a study |
| `comparison_group` | A group within an experiment |

Every level above the finding is optional. An unassigned finding has `NULL`
in `study_id`, `experiment_id` and `comparison_group_id`; no placeholder
records are created.

### Findings

| Table | Content |
|---|---|
| `finding` | Current state of a finding. See below |
| `finding_version` | Immutable snapshot of every state a finding has had, with change kind, author and actor kind |
| `source_location` | Locations owned by a finding, a paper or a screening decision |
| `finding_stage`, `paper_stage` | Stage assignments (many-to-many) with source (`imported`, `reviewer`, `suggested`) and status |
| `finding_relation` | Relations between findings, such as possible repeats, dependence, ambiguous identity after re-import |
| `derived_value`, `derived_value_input` | Normalized and derived values with their rule, formula and inputs |
| `numeric_candidate` | Candidate numbers detected in document text. Pointers, not findings |

A `finding` row holds:

- **Links**: `paper_id` (required), optional hierarchy links.
- **Kind and origin**: `kind` (`measurement`, `narrative`, `limitation`),
  `origin` (`workbook_import`, `manual_extraction`, `candidate_promotion`),
  `source_scope` (`workbook_summary`, `full_text`, `abstract_only`,
  `supplementary`).
- **Statuses**: `record_status` (`active`, `absent_from_latest`, `removed`),
  `review_status` (`draft`, `unreviewed`, `verified`, `rejected`, `stale`),
  `grounding_status` (`grounded`, `unsupported`), `comparison_eligibility`
  (`not_assessed`, `descriptive_only`, `quantitative_candidate`).
- **Content**: `source_wording` (exactly as in the source), `statement_type`
  (`exact`, `approximate`, `range`, `bound`, `qualitative`, `unparsed`),
  context fields (system, experimental context, intervention, comparator,
  outcome, measurement method), `observation_type`.
- **Value**: `value_literal` as written; `value_num` for a point value only;
  `value_low` and `value_high` for a range or the limit of a bound;
  `bound_type`; `unit_original`; `relative_semantics` and
  `reference_definition` for relative quantities.
- **Other transcribed details**: timepoint, observation kind, summary
  statistic, sample size, replicate type, uncertainty type and values,
  confidence level, denominator, normalization, qualifiers, interpretation
  limits.
- **`field_status_json`**: for an empty field, why it is empty:
  `not_reported`, `not_applicable`, `not_yet_extracted` or
  `could_not_be_parsed`.
- **`spans_json`**: for each transcribed literal, its span inside the cited
  text.

### Screening and review

| Table | Content |
|---|---|
| `screening_suggestion` | Draft suggestions generated from the protocol. Never counted as decisions |
| `screening_decision` | Every decision: stage, decision, reason kind, criterion, reviewer, decider kind, role, finality, protocol version and its status at the time, supporting text, what it supersedes. Rows are never edited |
| `paper_assessment` | Assessments kept separate from eligibility: `primary_relevance`, `quantitative_comparison`, `mathematical_modeling` |
| `extraction_review` | Reviewer actions on a finding version: verify, reject, edit, comment, reopen, with the checks made |
| `validation_run`, `validation_issue` | Each validation run and the issues it found |
| `comparison_set` | Saved comparison sets with their compatibility assessment |

### Audit and search indexes

| Table | Content |
|---|---|
| `audit_event` | Sequence number, time, actor, actor kind, action, entity, details, hash of the previous event, own hash |
| `finding_fts`, `paper_fts` | FTS5 full-text indexes kept in step with `finding` and `paper` by triggers |

## Audit log

Every state change made through the application writes an audit event in the
same transaction. An event's hash is the SHA-256 of the previous event's hash
and its own fields. `geodesic audit verify` (and `geodesic status`) recompute
the chain and report the first broken link. `geodesic audit list` filters by
entity. The log shows who did what, when, and as what kind of actor.

## Migrations and backups

The schema version is stored in the database. Opening a project applies
pending migrations in order, each in its own transaction. Before the first
migration changes an existing database, a consistent copy is written to
`<project>/backups/geodesic-pre-migration-v<N>-<timestamp>.sqlite`. A failed
migration is rolled back and the backup is kept. A database with a newer
schema than the application is refused with a message saying what to do.
