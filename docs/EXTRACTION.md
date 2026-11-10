# Documents, extraction, review, comparison and validation

This document covers the path from a full-text document to a reviewed,
organised finding. The location and grounding rules it relies on are in
`SOURCE_LOCATIONS.md`.

Not implemented: LLM-assisted extraction, OCR, figure digitization and HTML
ingestion. The application works without any external AI service.

## Documents

    geodesic documents add PAPER_ID FILE [--role main|supplementary] [--parent DOC]
                                         [--license TEXT] [--declare real]
    geodesic documents identify FILE          # read identifiers in a file; changes nothing
    geodesic documents list | show | status | find | reparse | remove | unavailable

A document is one file attached to one paper record. The file is copied byte
for byte into `sources/documents/` (named by SHA-256) and never modified.
Supported formats are PDF and XML (`.pdf`, `.xml`, `.nxml`).

**Parsing** is deterministic and executes nothing from the document.

- **PDF**: one text unit per page, keyed by the 0-based page index. The text
  is what the PDF library (pypdf) returns, with line endings normalised and
  NUL characters removed. Nothing is re-ordered or cleaned. A page without a
  text layer is recorded as `no_text`. A document with no text at all is
  `image_only`. There is no OCR.
- **XML**: one text unit per text-bearing element, keyed by its structural
  path (`body[1]/sec[2]/p[3]`). Inline markup is flattened and runs of
  whitespace are collapsed. `table-wrap` elements become tables with
  addressable cells. Reference lists and front matter other than title and
  abstract are not stored. Entity declarations and external references are
  refused and the file is recorded as a failed parse.

**Snapshots.** Each parse creates an immutable snapshot (parser, parser
version, status, text hash). `documents reparse` creates a new snapshot;
locations made against an earlier one keep resolving.

**Linking a document to a paper** records that this file was supplied for
this record. It does not verify that the file is that publication.
Identifiers found inside the file are compared with the record. A
disagreement is refused unless the reviewer overrides it with
`--allow-identifier-mismatch`, which is recorded. For a PDF only DOIs on the
first page are read.

**Availability** of full text is recorded per paper and never guessed:
`not_checked`, `open_access_reported`, `document_attached`, `abstract_only`,
`not_available`, `retrieval_failed`. `geodesic documents unavailable` records
that you looked and could not obtain it, with a note.

## Candidate values

    geodesic candidates detect SNAPSHOT_OR_DOCUMENT
    geodesic candidates list | show | promote | dismiss

The detector runs the same deterministic parser as the workbook importer
over every text unit and every non-header table cell of a snapshot. The same
snapshot always gives the same candidates.

A candidate is a pointer: "there is a number at this location". It is not a
finding. Candidates never appear in evidence tables, maps, comparison sets or
reports. A reviewer promotes a candidate, which creates an **unreviewed**
finding through the grounded manual-extraction path, or dismisses it.

For a table cell, the unit is read from the column header only in the plain
case of a single header cell; the header is then cited as a second location.

## Manual extraction

    geodesic documents find DOC "exact text"                 # get unit key and offsets
    geodesic extract propose --snapshot S --unit U --quote "…"   # show the parser's reading; stores nothing
    geodesic extract add PAPER_ID --snapshot S --unit U --quote "…"
            [--quantity N] [--kind measurement|narrative|limitation]
            [--set FIELD=VALUE] [--span FIELD=START:END] [--status-of FIELD=STATUS]
            [--stage STAGE] [--note TEXT]

or the dashboard page "7 Extraction".

- The finding's source wording **is** the stored text at the chosen span. It
  is never typed in.
- Each transcribed literal (value, unit, uncertainty, timepoint, sample size)
  must be found at a stated position inside that span. A proposed literal
  that is not there is refused and logged in `parse_failure`.
- A numeric field may only hold a number that is written in its literal.
- A range is stored as a range, a bound as a bound, an approximation as
  approximate. `value_num` is filled for a point value only.
- The new finding is `unreviewed`, or `draft` when the parser flagged an
  ambiguity. Creating a finding does not verify it.

### What a finding records

The context of the observation (system, experimental context, intervention,
comparator, outcome, measurement method), the observation type
(`direct_measurement`, `proxy_signal` or `functional_outcome`, recorded by
the reviewer and never guessed by the parser), the value as written, unit as written, statement type, timepoint, summary
statistic, sample size and replicate type, uncertainty type and values,
denominator and normalization, relative semantics and reference definition,
qualifiers, and limits on interpretation. Fields the source does not give
stay empty.

### Why a field is empty

Four states are kept apart, set with `--status-of FIELD=STATUS` or on the
review page:

| Status | Meaning |
|---|---|
| `not_reported` | The reviewer looked; the source does not state it |
| `not_applicable` | The field does not apply to this observation |
| `not_yet_extracted` | Nobody has looked yet |
| `could_not_be_parsed` | The source states something that could not be read reliably |

An empty field with no recorded status is reported as `no_status_recorded`.
It is never shown as "not reported".

## Review

    geodesic review show FINDING
    geodesic review verify FINDING --check outcome=confirmed --check condition=confirmed
            --check timepoint=not_applicable --check denominator=confirmed --check qualifier=confirmed
    geodesic review reject | reopen | comment | edit | stages | relate

or the dashboard page "8 Review findings", which shows the stored source text
with the excerpt and literals marked, next to the fields.

- **Verification needs a grounded finding and five recorded checks**:
  outcome, condition, timepoint, denominator, qualifier, each `confirmed` or
  `not_applicable`. Grounding is necessary, not sufficient.
- A `draft` finding (open ambiguity flags) is verified only with
  `--acknowledge-flags`, and the acknowledged flags are recorded.
- **Only a human reviewer can verify.** Rule-based components cannot.
  Scripted demonstration actions are recorded as `simulated` and are refused
  in real projects.
- **An edit never overwrites.** It creates a new finding version, marks
  earlier reviews and computed values that used the finding stale, resets
  comparison eligibility, and returns the finding to `unreviewed`.
- **Reject** keeps the finding and excludes it from evidence views.
- **Relate** records that two findings are possible duplicates, dependent,
  or that one limits the other.

Review status values: `draft`, `unreviewed`, `verified`, `rejected`,
`stale`. Every view, export and report shows the status, and counts keep
verified and unverified findings apart.

## Organising

- **Stages.** A finding or paper can belong to several stages
  (`geodesic review stages`). Stage names and definitions come from
  `project.yaml`.
- **Hierarchy** (`geodesic hierarchy`): paper > study > experiment >
  comparison group > finding. Every level above the finding is optional and
  written by a reviewer. A study can be reported by several papers, which is
  how findings from different papers are known to describe the same work.
- **Vocabulary** (`geodesic vocabulary`): original terms stay as written. A
  reviewer may group one original term of one field under a normalized term.
  The mapping is exact, recorded, reversible, and applied only when tables
  are grouped, always next to the original terms. Similar-looking terms are
  never merged automatically.

## Units, normalized and derived values

    geodesic derived units                     # the explicit conversion table
    geodesic derived normalize FINDING [--to UNIT] [--field value|timepoint]
    geodesic derived compute difference|ratio FINDING_A FINDING_B
    geodesic derived trace DERIVED_ID
    geodesic derived list | withdraw

- Every conversion the application can perform is listed with its factor in
  `src/geodesic/quantities/units.py`. Nothing else is converted.
- Ambiguous unit symbols are recognised, flagged and not converted.
  Relative quantities (percent, fold, arbitrary units) are never converted.
- A computed value is stored beside the original, never over it, with its
  rule, a readable formula and, for each input, the finding ID, finding
  version, literal and unit. `derived trace` follows these back to the stored
  source text.
- A range stays a range. No midpoint is invented.
- Uncertainty is scaled by a unit conversion. It is not propagated through
  `difference` or `ratio`.
- When an input finding is edited the computed value becomes stale and must
  be recomputed explicitly.

## Comparison

    geodesic compare assess [FINDING_ID …] [filter options]     # stores nothing
    geodesic compare table | units | save | list | show
    geodesic compare eligibility not_assessed|descriptive_only|quantitative_candidate [FINDING_ID …]

A set is given as finding IDs or through the same filter options as
`geodesic findings` (`--stage`, `--outcome`, `--system`, `--review-status`
and others). A set chosen by filter holds at most 500 findings.

Nothing is pooled, averaged or ranked.

`compare assess` lists, for a set of findings, what is equal, what differs
and what is missing. **Blocking** checks: at least two findings; active
records; grounded; verified; numeric statements; the same recorded outcome;
compatible units; the same observation type, denominator and normalization.
**Caution** checks: system context, experimental context, measurement
method, intervention, comparator. A set with no failed blocking check is a
`quantitative_candidate`; otherwise it is `descriptive_only`. "Candidate"
means the recorded fields do not rule a quantitative comparison out. The
decision stays with the reviewer.

Terms are compared as written. Two different terms count as equal only when
a reviewer mapped both to one normalized term.

**Independence.** Findings of one experiment, or findings a reviewer related
as dependent or as possible duplicates, form one unit. A finding with
neither is its own unit, but its independence was not assessed. Counts are
therefore reported as "at most N independent units", with the number of
findings not assessed and the number of distinct reports.

## Validation

    geodesic validate run [--strict]
    geodesic validate show [--severity error|warning|info] [--code CODE] [--finding ID]

A run re-checks every active finding against the stored source text and a
fixed rule list (rules version 1.0) and stores the result. Runs are
append-only, so the record shows what was known when.

| Severity | Rules |
|---|---|
| error | `location_missing`, `location_unresolved`, `literal_not_at_span`, `grounding_status_inconsistent`, `value_literal_mismatch`, `verified_without_current_review`, `source_document_removed` |
| warning | `unsupported`, `range_not_ascending`, `unit_unrecognized`, `unit_ambiguous`, `relative_without_reference`, `derived_value_stale`, `assignment_inconsistent` |
| info | `unit_state_not_recorded`, `field_state_not_recorded`, `uncertainty_type_unspecified`, `open_ambiguity_flags`, `unparsed_content`, `no_stage`, `paper_unresolved`, `superseded_snapshot` |

Validation checks traceability and internal consistency. It cannot tell
whether a finding represents its source correctly, and a run without issues
does not make findings verified.
