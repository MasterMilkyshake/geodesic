# Decisions

Non-obvious choices made while building GEODESIC, with the reason for each.
Dates are given in UTC unless stated. Where a decision rests on something that
was read or run, the entry says what was read or run and when.

## 1. Scope and priorities

Choices were made in this order of priority: accurate representation of source
evidence, provenance, transparent screening and human review, reproducibility,
maintainability, then feature breadth and visual polish.

- **One researcher, one machine.** No accounts, no concurrent editing. Several
  named reviewers can work one after another on the same installation; each
  action is recorded under the name it was made with.
- **Optional features were not built.** LLM-assisted extraction, OCR, figure
  digitization and HTML ingestion are unimplemented. The application works
  offline without any external AI service. See `docs/LIMITATIONS.md`.
- **Mathematical modelling is an inventory only.** `geodesic modeling` lists
  what the recorded findings contain. Nothing is fitted.

## 2. Storage

- **SQLite through the standard-library `sqlite3` module, no ORM.** The schema
  is fixed, the application is local and single-user, and plain SQL keeps every
  query visible. The connection runs in autocommit mode and all writes go
  through an explicit `transaction()` context. WAL journal mode is used.
- **One project is one directory** holding one database, the preserved source
  files and the project configuration. Moving or backing up a project is
  copying a directory.
- **No hard deletes, enforced in the database.** `DELETE` is blocked by a
  trigger on every evidence, decision, review and audit table. Removal is a
  recorded state change (`record_status`, `status`, `active`). Fully
  append-only tables also block `UPDATE`, and selected columns of other tables
  are immutable by trigger (for example `project.data_class`, the content of a
  protocol version, the content of a screening decision, the address of a
  source location).
- **Append-only audit log with a hash chain.** Each event stores the SHA-256
  of the previous event's hash and its own fields. `geodesic audit verify`
  recomputes the chain. This detects edits or truncation made outside the
  application; it is not protection against someone who rewrites the whole
  database file.
- **Stable identifiers** are a type prefix plus 80 random bits in hex
  (`pap_…`, `fnd_…`). They never encode position or content, survive
  re-import, and appear unchanged in exports.
- **Timestamps** are ISO 8601 in UTC with an explicit `+00:00` offset.
- **Migrations** are numbered modules. Opening a project applies pending
  migrations, and a consistent backup (SQLite online backup API) is written to
  `backups/` before the first one runs. A database newer than the application
  is refused. Only schema version 1 exists so far; the backup path is tested
  with a synthetic second migration.
- **Every state of a finding is kept.** `finding` holds the current content
  and `finding_version` an immutable snapshot of each state. This is the main
  contributor to database size (about 7 KB per finding in the scale test). It
  was kept because it is what lets a reviewer correction or a re-import leave
  the original extraction intact.
- **Planner statistics.** `PRAGMA optimize` is run when a project is opened
  and closed and after bulk imports. Without statistics SQLite chose
  low-selectivity indexes for some filters at 100,000 findings. Statistics are
  not evidence and are not audited. Stage and search-run filters are written
  as uncorrelated `IN (SELECT …)` subqueries for the same reason, and exports
  iterate one ordered query instead of paging with `OFFSET`.

## 3. Synthetic and real data

- **Declared provenance only.** A project is created `synthetic` or `real` and
  the flag is immutable. Bundled fixtures carry an embedded tag
  (`geodesic-data-class: synthetic`): an XLSX custom document property, PDF
  document information, or a marker in the first 4 KB of a text file. An
  untagged import must be declared with `--declare`. A tagged file cannot be
  declared real. The effective class must equal the project's class.
  The application does not and cannot detect undisclosed synthetic content.
- **Live provider calls are refused in synthetic projects**, because live
  responses describe real publications. Synthetic projects replay fixture
  responses through the same code path.
- **A real project is refused inside a version-controlled directory or the
  source tree.** `.gitignore` also excludes databases, `local.yaml` and
  `credentials.env`.
- **Every export from a synthetic project is labelled**: file names start with
  `SYNTHETIC_`, and the label is written inside each file, including inside
  figure images.
- **Simulated reviewers.** The demo and the scale test need review actions
  that only a person can legitimately make. They are made by actors of kind
  `simulated`, which are permitted only in synthetic projects, are stored as
  simulated, and are counted separately from human and automated actions
  everywhere.
- **The synthetic fixture is non-biological**: an invented inert tracer moving
  among engineered reservoirs. Fixture DOIs use the `10.5555` prefix, which is
  reserved for examples.

## 4. Workbook import

- **Profile first, then a mapping a person confirms.** The profiler infers
  from header text only and marks every inferred entry `confirmed: false`. The
  importer refuses to run while a required entry is unconfirmed. A new layout
  means editing a mapping file, not code.
- **The input file is never opened for writing.** It is copied byte for byte
  into `sources/workbooks/<sha256>.xlsx`, the copy is verified against the
  hash and made read-only, and the import reads the copy.
- **Imported content is imported evidence, not verified fact.** Findings from
  a workbook start `unreviewed` (or `draft` when the parser flagged an
  ambiguity) with source scope `workbook_summary`. A citation that cannot be
  resolved to an identifier becomes an explicitly `unresolved` paper record.
  No title or year is derived from citation text.
- **Identity across versions is content-based.** A finding is recognised by
  its paper, its column role and its normalised wording, never by row number,
  so moving rows changes nothing. Reworded text is matched by similarity
  within the same paper and role; an uncertain match is recorded as an
  ambiguous identity for review instead of being decided silently.
- **The narrative parser is deterministic and conservative.** A range stays a
  range, a bound stays a bound, `±` without a stated meaning stays
  `unspecified_plus_minus`, and a number with an ambiguous decimal comma is
  recorded as unparsed content instead of being guessed.
- **Summary counts are reconciled, not trusted.** Counts the mapping points to
  are compared with what was imported and reported as MATCH or MISMATCH.

## 5. Literature search providers

Provider documentation was consulted on 2026-11-09 (US Pacific; 2026-11-09 to
2026-11-10 UTC) before choosing connectors.

| Provider | What was read | Finding | Decision |
|---|---|---|---|
| Europe PMC | The published OpenAPI definition at `https://www.ebi.ac.uk/europepmc/webservices/rest/v3/api-docs`, the `fields` endpoint, and the EMBL-EBI terms of use (last revised 14 October 2026). The human-readable page `https://europepmc.org/RestfulWebService` returned HTTP 403 to the documentation fetch and could not be read. | The OpenAPI definition declares no authentication scheme. `GET /search` takes `query`, `resultType` (`idlist`, `lite`, `core`), `cursorMark`, `pageSize` (default 25, maximum 1000), `sort`, `format`. `GET /{id}/fullTextXML` exists. The terms state no numeric rate limit; use that prevents service to others leads to blocking; access must go through the service interfaces. | **Default search connector**, and the only route for automatic full-text retrieval. |
| OpenAlex | `https://help.openalex.org/how-to-use-the-api/rate-limits-and-authentication` (last updated 3 November 2026) | Basic queries work without a key, but cursor paging beyond the first page requires a free key tied to a verified e-mail address. | **Not implemented.** It cannot be a credential-free default. |
| Crossref | REST API "Access and authentication" (last updated 16 October 2025) and "Tips" (last updated 24 October 2025) | No registration needed. Supplying `mailto` (polite pool) is recommended. A 429 means back off. Metadata Plus uses a `crossref-api-key` header. | **Used for identifier validation and metadata enrichment only**, not as a search source. |
| NCBI E-utilities | NLM knowledge article KA-05317. The usage-guidelines page (NBK25497) returned a browser check and could not be read. | 3 requests per second without a key, 10 with a key. | **Not implemented.** A keyless default connector already exists, and the usage guidelines could not be read. |

Consequences in the code:

- Requests are rate-limited on the client side (`search.min_interval_seconds`,
  default 0.5 s), retried a bounded number of times with exponential backoff
  on transport errors and HTTP 429/500/502/503/504, and honour `Retry-After`.
- The client identifies itself with a configurable tool name. A contact
  address is sent only when one is configured (`contact_email` in `local.yaml`
  or `GEODESIC_CONTACT_EMAIL`), only with live requests, and is masked in
  logs, stored request descriptions and raw-response archives. No address is
  hardcoded.
- A search run stores every raw response with its SHA-256. Reproducing a run
  means re-reading that snapshot. Running the same query again is a new run.
- The executed query string, filters, retrieval limit, counts, and whether the
  limit was reached are stored with the run. A run made under a provisional
  protocol is labelled exploratory with that protocol version.
- No connector claims exhaustive coverage.

### Live verification

Run on 2026-11-10 UTC (about 02:01 to 02:04) with `geodesic live-smoke` and
`python -m pytest tests -m network`. The smoke test uses a small query from
configuration and reports mechanics, counts and response schema only. It runs
in a throw-away real project in the system temporary directory, which is
deleted afterwards, so no retrieved record was kept, summarised or analysed.

Verified live: Europe PMC search with cursor pagination, the retrieval limit,
re-reading the stored raw snapshot, the publication-year and open-access
filters, one Crossref work lookup, and one Europe PMC `fullTextXML` retrieval
with parsing. Results: `geodesic live-smoke` 11 checks passed, 0 failed, 0
unverified. Later runs are recorded in `docs/TESTING.md`.

Not verified live: Crossref free-text citation lookup
(`query.bibliographic`), Crossref with a Metadata Plus token, and behaviour
under real rate limiting (HTTP 429). These are tested against mocked
transports only.

## 6. Full text and documents

- **Automatic retrieval is limited to one documented route**: Europe PMC
  `fullTextXML`, attempted only when a search source reported the record as
  open access and the record has a PMCID. The licence reported by the source
  and the licence stated in the XML are recorded. Nothing else is tried. A
  failed retrieval is recorded as `retrieval_failed`, never as "no full text
  exists".
- **PDF text is taken as the PDF library returns it** (pypdf), one unit per
  page, with line endings normalised and NUL characters removed. A page with
  no text layer is recorded as `no_text`. No OCR.
- **XML is read with a hardened parser** (defusedxml). Entity declarations and
  external references are refused and the file is recorded as a failed parse.
  One text unit per text-bearing element, addressed by a structural path.
- **Document content is data.** Nothing in a document is executed or
  interpreted as an instruction. In the HTML report every value from a
  workbook, document or provider passes through `html.escape`, and a
  Content-Security-Policy forbids scripts and network access. In Excel exports
  all text is written as text, so a value starting with `=` is never a formula.
- **Snapshots are immutable.** Parsing a document again creates a new
  snapshot; locations made against an earlier snapshot keep resolving.

## 7. Evidence, grounding and review

- **One source-location system** (`docs/SOURCE_LOCATIONS.md`): five location
  kinds, all resolving against text stored in the project database, with
  0-based half-open character spans.
- **Grounding is checked by the application; correctness is judged by a
  person.** A finding is grounded when its excerpt and every transcribed
  literal are exactly the stored text at their spans. Only a grounded finding
  can be verified, and verification additionally records which aspects the
  reviewer checked. Rule-based components cannot verify.
- **A manual extraction's wording is the stored text at the chosen span.** It
  is never typed in. A proposed literal that is absent from the span is
  refused and logged as a parse failure.
- **An edit never overwrites.** It creates a new version, marks earlier
  reviews and dependent computed values stale, and returns the finding to
  `unreviewed`. This is deliberately strict: any content edit resets
  comparison eligibility too.
- **Candidates are pointers, not evidence.** The deterministic detector marks
  where numbers are. Candidates never appear in evidence tables, maps or
  reports until a reviewer promotes one into a finding.
- **Four reasons a field can be empty are kept apart**: `not_reported`,
  `not_applicable`, `not_yet_extracted`, `could_not_be_parsed`. An empty field
  nobody has classified is reported as `no_status_recorded`, not as "not
  reported".
- **Original terms are stored as written.** A reviewer may group an original
  term under a normalized term; the mapping is exact, recorded, reversible,
  and applied only when tables are grouped. Similar-looking terms are never
  merged automatically.
- **Integrity checks are dated statements about a source**: "none found in
  Crossref on <date>", never "not retracted".

## 8. Units, derived values and comparison

- **An explicit unit table instead of a units library.** Every conversion the
  application can perform is listed with its factor in
  `src/geodesic/quantities/units.py`. Nothing outside the table is converted.
  Ambiguous symbols are recognised, flagged and not converted. Relative
  quantities (percent, fold, arbitrary units) are never converted.
- **Computed values live beside the original**, in their own table, with the
  rule, formula, and for each input the finding ID, version, literal and unit.
- **Uncertainty is scaled by a unit conversion but not propagated** through
  `difference` or `ratio`, because that needs assumptions the source does not
  state.
- **Nothing is pooled, averaged or ranked.** A compatibility assessment lists
  what is equal, what differs and what is missing, and classifies a set as
  `quantitative_candidate` or `descriptive_only`. The decision to compare
  stays with the reviewer.
- **Independence counts are upper bounds.** Findings of one experiment, or
  related as dependent by a reviewer, form one unit. A finding with neither is
  its own unit but was not assessed, so counts read "at most N".

## 9. Deduplication and screening

- **Automatic grouping needs a validated identifier on both sides.** Anything
  else is a candidate for review. Similarity only ever proposes. Every
  grouping is reversible and a reversal is remembered. See
  `docs/DEDUPLICATION.md`.
- **Identifiers read from a workbook or an imported file are unvalidated**,
  because such files can be edited by hand.
- **Eligibility, relevance, suitability for quantitative comparison and
  suitability for modelling are four separate records.**
- **Suggestions are not decisions.** They live in their own table and become
  decisions only when a named reviewer accepts them, or when a confirmed
  protocol explicitly authorises a listed rule at that stage.
- **Missing information never excludes at title/abstract stage.** An abstract
  that omits a detail yields `uncertain`.
- **Protocol changes flag, never rewrite.** Each decision stores the protocol
  version it was made under. A changed criterion flags the current decisions
  it governed for re-review. Decisions made under a provisional protocol stay
  provisional after confirmation and are flagged, not promoted.

## 10. Dashboard, figures and reports

- **Streamlit, with no logic of its own.** Each page calls the public
  functions the CLI calls. The project is opened at the start of each script
  run and closed at its end.
- **matplotlib for all figures**, written as SVG, PDF and PNG at 300 DPI or
  more. Plotly was removed as an unused dependency.
- **Figures follow a documented drawing convention** (`docs/REPORTS.md`): one
  hue from light to dark for magnitude, six fixed categorical colours for the
  missing-information states, text in ink colours, counts printed on marks
  where they fit and always available as a table. Figures and the HTML report
  are drawn for a light surface only. SVG text is rendered as paths so output
  does not depend on installed fonts.
- **The search and screening flow summary is PRISMA-style** in that it follows
  records from identification through screening. No claim of compliance with
  any reporting guideline is made.
- **The HTML report is one self-contained file** with inline styles, figures
  as data URIs, and no scripts.
- **Every export directory has a manifest** with the SHA-256 of each file
  written and of each input, the project configuration and the protocol
  version.

## 11. Credentials, logging and licences

- **Credentials** are read from the environment or an untracked
  `credentials.env`. They are never written to the database, exports,
  raw-response archives or logs. Logs are JSON lines; registered secrets and
  common credential patterns are masked.
- **Dependency licences** are recorded from installed package metadata by
  `scripts/dependency_licenses.py` into `docs/DEPENDENCY_LICENSES.md`. It is a
  record, not a legal review.
- **The project's own licence field** in `pyproject.toml` is set to MIT as a
  placeholder. The owner should confirm or replace it; no `LICENSE` file is
  shipped.

## 12. Testing

- **All fixtures are generated and synthetic**, including the PDF and XML
  documents. The PDF fixtures come from a small deterministic PDF writer in
  the package, so no third-party generator and no real paper is involved.
- **Network and scale tests are separate** (`-m network`, `-m scale`) and
  deselected by default. The standard suite replaces the HTTP transport and
  never touches the network.
- **The scale test measures and does not assert performance thresholds.** It
  asserts that nothing is lost and that every finding keeps its provenance.
  Its design: the workbook cites 60 % of the papers; 2,000 DOIs are validated
  against synthetic registry-shaped responses; every 250th search record is
  repeated; 500 screening decisions and 200 verifications are made by a
  simulated reviewer.
