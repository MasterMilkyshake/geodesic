# Dashboard, evidence maps, figures, exports and reports

Every number shown by the dashboard, a figure, the HTML report or an export
comes from the same public functions the CLI calls. The front ends hold no
logic of their own, so they cannot disagree about a count.

None of these outputs draws a conclusion, ranks evidence or pools values.
All values in maps and figures are counts of records in the project.

## Dashboard

    geodesic dashboard [--project DIR] [--port N] [--headless]

A local Streamlit application for one user. Pages:

| Page | What it does |
|---|---|
| Overview | Project, data class, protocol status, counts, audit-chain check |
| 1 Protocol (research scope) | View, edit, load and confirm the protocol; version history |
| 2 Workbook import | Import runs, reconciliation counts, unparsed content, parse failures |
| 3 Literature search | Run or replay a search, import a file, list runs, verify snapshots |
| 4 Duplicates | Candidates with score components side by side; confirm, reject, re-classify, ungroup |
| 5 Screening | Queue, suggestions, decisions with reasons, conflicts, re-review |
| 6 Documents (full text) | Attach and inspect documents, snapshots, text units and tables |
| 7 Extraction | Candidates, and manual extraction from stored text |
| 8 Review findings | A finding beside its marked source text; verify, reject, edit, comment |
| 9 Organise | Filters and full-text search, stages, hierarchy, vocabulary |
| 10 Compare | Compatibility assessment, comparison table, saved sets |
| 11 Evidence maps | Evidence map, missing-information summary, modelling inventory |
| 12 Validation | Run validation and browse issues |
| 13 Reports and exports | Build a report directory or an export |
| Audit trail | Audit events with filters, and chain verification |

The reviewer name is taken from `GEODESIC_REVIEWER` or `local.yaml` and can
be changed in the sidebar. A synthetic project shows its data-class banner on
every page. Review status and protocol status are always visible next to the
evidence they apply to.

## Filtering and search

`geodesic findings` and `geodesic papers`, the "9 Organise" page, exports
and reports share one filter (`src/geodesic/query.py`): full-text search
(SQLite FTS5) over wording and context, stage, kind, statement type,
quantitative or not, observation type, system, outcome and method (as
substrings), with or without timepoint or uncertainty, review status, flag,
paper, year range, and screening outcome at a stage. `geodesic papers`
filters papers by text, stage, year, resolution, origin, whether they have
findings, and screening outcome. Filters combine with AND; a filter given a
list matches any of its values. Ordering is by a stable, stated key. Nothing
is ranked by quality.

## Evidence maps

    geodesic map --by outcome [--metric findings] [filter options]

A cross-tabulation of the project's stages against the terms of one
descriptive field (`system_context`, `experimental_context`, `intervention`,
`comparator`, `outcome`, `measurement_method`). Metrics: `findings`,
`reports`, `independent_units_at_most`, `verified`, `quantitative`,
`quantitative_candidates`.

Counting rules:

- A finding in several stages is counted in each, so stage rows do not add
  up to the total. Totals count distinct findings.
- Terms are the original terms as stored. Two terms share a column only when
  a reviewer mapped both to one normalized term.
- `independent_units_at_most` is an upper bound (see `EXTRACTION.md`).
- The 12 most frequent terms are shown individually; the rest are summed
  under a label such as "other (5 terms)". Findings without a term are shown
  as "(not recorded)".

A map shows where evidence has been recorded and where none has. An empty
cell means no record in this project, not that no evidence exists.

## Missing information and the modelling inventory

    geodesic missing [filter options]
    geodesic modeling

`missing` counts, per stage and field, how many measurement findings have a
value and how many are empty for each reason: `not_reported`,
`not_applicable`, `not_yet_extracted`, `could_not_be_parsed`, and
`no_status_recorded` for empty fields nobody has classified. Unit and
uncertainty are counted only for findings stating a number, range or bound,
and the reference definition only for relative quantities.

`modeling` is an inventory, for all stages together and per stage, of the
quantitative findings: how many have a unit, are absolute quantities, have a
timepoint, uncertainty or sample size, are verified, are quantitative
candidates, and are assigned to an experiment; and how many groups of
findings of one experiment share an outcome and unit at three or more time
points. It also counts papers by the reviewers' recorded
`quantitative_comparison` and `mathematical_modeling` assessments. Nothing
is fitted and no suitability is inferred.

## Figures

    geodesic figures [--out DIR] [--formats svg,pdf,png] [--dpi 300]

| Figure | Form |
|---|---|
| Evidence map (by system context, by outcome) | Heatmap, terms in rows and stages in columns, count printed in each cell |
| Missing information | One stacked horizontal bar per field, segments by state |
| Search and screening flow | Box diagram from identification through screening |

Formats: SVG, PDF and PNG. PNG resolution defaults to 300 DPI and can be
raised.

Drawing conventions (`src/geodesic/reports/figures.py`):

- **Magnitude uses one hue, light to dark.** No rainbow scale.
- **The six missing-information states are nominal categories** and use six
  fixed colours in a fixed order. The order was checked for separation under
  colour-vision deficiency. Three of the fills have less than 3:1 contrast
  against the surface, so counts are printed on segments where they fit and
  the same numbers are always written as a table next to the figure.
- **Text is drawn in ink colours**, never in a data colour. Fills are
  separated by a 2 px gap in the surface colour, not by outlines.
- **A legend is always present** where more than one category is drawn.
- **Each figure carries the data-class label inside the image**, so a figure
  from a synthetic project stays labelled when copied out of its report.
- The label inside the image also names the project and the protocol
  version, and says when the protocol is provisional. A figure does not
  state the finding filter it was drawn under; the HTML report states it
  once, at the top.
- A filter narrows the evidence maps and the missing-information figure. The
  search and screening flow always describes the whole project.
- Layout uses the bundled DejaVu Sans face. SVG text is rendered as paths, so
  output does not depend on installed fonts; the text in an SVG is therefore
  not selectable.
- Figures are drawn for a light surface only.

## Exports

    geodesic export [--out DIR] [--formats xlsx,csv,json,ris] [filter options]

Tables: papers, findings, source locations, stages, finding relations,
unparsed content, workbook versions, documents, search runs, screening
decisions, paper assessments, studies, experiments, derived values,
comparison sets, vocabulary terms and validation issues. Every finding row
carries its stable ID, paper ID, review status, grounding status, origin,
source scope and current version. Source locations are a table of their own,
linked by finding ID.

| Format | Layout |
|---|---|
| Excel | Sheet `README` first. On each table sheet row 1 is a banner (data class and protocol status), row 2 the header, data from row 3. Columns that reference another table are internal hyperlinks. All values are written as text; a value starting with `=` is never a formula |
| CSV | UTF-8 with header row. The last column, `data_class`, repeats the project's data class on every row |
| JSON | `{"label", "project", "tables": {name: {"columns", "rows", "note"}}}` |
| RIS | One record per paper. `N1` carries the GEODESIC paper ID and status |

Excerpts in exports are short quotations (at most 600 characters), never
whole documents. Credentials, contact addresses and `local.yaml` are never
exported.

Every export directory contains a **manifest** with the SHA-256 and size of
each file written; the SHA-256 of each input (workbook versions, documents,
stored search responses); the project, the protocol version and whether it
was confirmed; row counts; and the application version.

Files from a synthetic project are prefixed `SYNTHETIC_` and carry the label
inside.

## Report directory

    geodesic report [--out DIR] [--parts html,figures,tables] [--title TEXT] [filter options]

One command writes a self-describing directory: the HTML report, the
figures, the tables as CSV, an Excel workbook, a JSON file, a RIS file, the
project configuration and the manifest. The offline demonstration writes 40
files; they are distributed in `examples/synthetic_demo/report/`.

The **HTML report** is one self-contained file: inline styles, figures
embedded as data URIs, no scripts, no external resources. Every value from a
workbook, document or provider is escaped, and a Content-Security-Policy
forbids scripts and network access. Its sections are: counts, search and
screening, full text, evidence maps, missing information, modelling
readiness, comparison sets, validation, the evidence table (findings with
review status and source locations), reports (the paper records), inputs
and audit trail, and limitations. A provisional protocol and an active
filter are stated at the top. The findings and
papers tables in the HTML file show at most 500 rows each and state how many of
the total are shown; the complete tables are in the CSV, Excel and JSON files. The
page is light-only.

Reproducing a report means running the same command on the same project
state. The manifest records the inputs it was built from. `generated_at` and
file hashes that depend on it differ between runs.
