# Architecture

GEODESIC is a Python package (`src/geodesic`) with two front ends, a
command-line interface and a Streamlit dashboard. Both call the same public
functions. All data-processing logic is independent of the dashboard, and no
module outside `geodesic.dashboard` imports Streamlit.

    CLI (argparse) ----+
                       +--> geodesic.* public functions --> SQLite database
    Streamlit pages ---+                                    + project data directory

## Layers and modules

| Layer | Modules | Responsibility |
|---|---|---|
| Foundation | `db`, `audit`, `project`, `provenance`, `config`, `logging_utils`, `util`, `errors` | Connections, transactions, migrations with backups; append-only audit log; project directories; declared synthetic/real class; configuration and credentials; redacted structured logs |
| Workbook | `workbook.reader`, `workbook.profiler`, `workbook.mapping`, `workbook.extract`, `workbook.importer`, `textparse` | Read-only workbook reading, profile and starter mapping, mapping validation, deterministic parsing of narrative cells, versioned import and reconciliation |
| Papers | `papers`, `identifiers`, `dedup` | Paper records, identifier normalisation and status, metadata conflicts, integrity checks, duplicate detection and reversible grouping |
| Search | `search.http`, `search.europepmc`, `search.runs`, `search.snapshot`, `search.fileimport`, `search.crossref`, `search.smoke` | Provider client, Europe PMC connector, stored search runs, snapshot verification, RIS/CSV import, Crossref validation, bounded live smoke test |
| Protocol and screening | `protocol`, `screening` | Versioned protocol, suggestions, decisions, adjudication, re-review, flow counts, separate assessments |
| Documents | `documents.parsers`, `documents.store`, `documents.openaccess` | PDF and XML text extraction, immutable snapshots, document-to-paper links, open-access retrieval |
| Evidence | `locations`, `findings`, `extraction.candidates`, `extraction.manual`, `extraction.hierarchy`, `extraction.validation`, `extraction.derived`, `review`, `vocabulary`, `quantities.units` | Source locations and grounding, versioned findings, candidate detection, manual extraction, sparse hierarchy, validation runs, normalized and derived values, reviewer actions, term grouping, unit table |
| Analysis and output | `query`, `comparison`, `reports.maps`, `reports.missing`, `reports.flow`, `reports.figures`, `reports.html`, `reports.build`, `export` | Filtering and search, compatibility assessment, evidence maps, missing-information summaries, flow summary, figures, HTML report, table exports |
| Front ends | `cli`, `cli_more`, `cli_docs`, `cli_review`, `cli_reports`, `dashboard.*` | Argument handling and printing; Streamlit pages |
| Synthetic | `synthetic.workbook`, `synthetic.search`, `synthetic.documents`, `synthetic.protocol`, `synthetic.scale`, `demo` | Generated fixtures, the offline demonstration, the scale driver |

The CLI and dashboard modules contain argument handling, formatting and
widgets only. Reports, evidence maps and exports produced from the dashboard
come from the same functions as those produced from the CLI
(`reports.build.build_report`, `reports.maps.evidence_map`,
`export.export_tables`), so the two cannot disagree about a count.

## Project data directory

One project is one directory, separate from the source tree:

    <project>/
      project.yaml            exportable configuration (stages, parser, dedup, search)
      local.yaml              optional, per machine (reviewer, contact address); never exported
      credentials.env         optional, untracked
      geodesic.sqlite         the linked evidence database
      sources/workbooks/      byte-identical workbook copies, named by SHA-256
      sources/documents/      byte-identical document copies, named by SHA-256
      sources/search_files/   byte-identical imported search-result files
      raw_responses/          stored provider responses (credentials excluded)
      backups/                automatic pre-migration database backups
      exports/                generated exports
      logs/                   structured logs (JSON lines)

A project is created with a data class, `synthetic` or `real`, which cannot be
changed. A real project is refused inside a version-controlled directory or
the source tree. All paths stored in the database are relative to the project
directory, so a project can be moved.

## Opening and writing

`open_project(dir)` connects, applies pending migrations (after writing a
backup), refreshes planner statistics, and loads `project.yaml`. Every write
runs inside `db.transaction(conn)`. Every state change records an audit event
in the same transaction, so the evidence and its history cannot diverge.

## Actors

Every action carries an `Actor(name, kind)`. Kinds are `human` (a named
reviewer), `automated` (a documented rule), `system` (the application itself,
for example creating a project) and `simulated` (scripted review actions of the
demo and the scale test, permitted only in synthetic projects). Review actions
require a human or, in a synthetic project, a simulated actor. Counts keep the
kinds apart.

## Untrusted content

Workbook cells, document text and provider responses are stored and displayed
as data. Nothing in them is executed, evaluated or followed as an instruction.
XML is parsed with entity declarations and external references refused. HTML
output escapes every such value and forbids scripts by Content-Security-Policy.
Excel output writes all values as text.

## Extending

- A new workbook layout needs a mapping file, not code.
- A new search connector would implement the same steps as
  `search/europepmc.py`: translate the concept groups, fetch pages through
  `search.http.HttpClient`, and hand raw pages and parsed records to
  `search.runs`. Provider terms must be read first and recorded in
  `DECISIONS.md`.
- A schema change is a new module `db/schema_vN.py` with an `upgrade(conn)`
  function, added to the migration list in `db/__init__.py`.
