# Limitations and unfinished work

This list states what GEODESIC 0.1.0 does not do, does only in part, or was
not verified. Test observations are in `TESTING.md`.

## What the application cannot establish

- **It cannot tell whether a finding represents its source correctly.**
  Grounding shows that an excerpt and its literals are present at a stored
  location. Validation checks traceability and internal consistency.
  Interpretation is the reviewer's judgement, recorded as a review.
- **It cannot detect undisclosed synthetic content.** The synthetic/real
  separation rests on tags the fixture generator writes and on your
  declarations. An untagged file is whatever you declare it to be.
- **Imported workbook content is imported evidence, not verified fact.**
  Summaries and citations read from a workbook stay unverified until an
  identifier is validated and a reviewer verifies the finding.
- **A search is not exhaustive.** A run records what one provider returned
  for one query on one date, up to the retrieval limit. No coverage claim is
  made.
- **Linking a document to a paper record does not verify identity.** For a
  PDF, only DOIs on the first page are compared with the record.
- **Independence of findings is not inferred.** A finding with no experiment
  and no recorded relation is counted as its own unit, with its independence
  not assessed. Counts of independent units are therefore "at most N".
- **The audit hash chain detects edits to individual events.** It does not
  protect against someone replacing the whole database file, since the chain
  can be recomputed. Keep backups outside the project directory if that
  matters.

## Not implemented

| Item | State |
|---|---|
| LLM-assisted extraction | Not implemented. No external AI service is called |
| OCR | Not implemented. Image-only pages and documents are recorded as such; `ocr_applied` is always 0 |
| Figure digitization | Not implemented |
| HTML document ingestion | Not implemented. The `html_element` location kind and the `html` document format exist in the schema only |
| OpenAlex connector | Not implemented |
| PubMed / NCBI E-utilities connector | Not implemented. Records exported from another database can be imported as RIS or CSV files |
| Meta-analysis, pooling, model fitting | Out of scope. The modelling inventory counts fields; it fits nothing |
| Multi-user access, accounts, concurrent editing, hosting | Out of scope. Several named reviewers can work one after another on one installation |

## Verified only against fixtures

- Crossref bibliographic matching (`query.bibliographic`).
- The Crossref Plus token (only its redaction is tested).
- Retry behaviour on HTTP 429 with `Retry-After` (local stub transport).
- Searches larger than the six-record smoke query.

Live checks passed on 2026-11-10 UTC. Provider interfaces can change after
that date.

## Partial

### Workbook import

- The unit in a column header is read only in the plain case of a single
  header cell.
- Papers created from workbook citation text have no structured title.
  They are never compared by similarity, and a shared identifier stays a
  candidate in the review queue until the identifier is validated
  (`geodesic crossref validate`) or a reviewer decides.
- Importing an earlier version of a workbook after a later one, under the
  same `workbook_key`, is refused.
- The narrative parser is deterministic and conservative. Wording it does
  not recognise is kept as qualitative or unparsed content for a reviewer.

### Documents and extraction

- XML: inline markup is flattened. Reference lists and front matter other
  than title and abstract are not stored as text units.
- PDF: text is taken as the PDF library returns it. Reading order in
  multi-column layouts, hyphenation and tables inside PDFs are not
  reconstructed. PDF tables are not addressable as table cells.
- A PDF that cannot be read cannot have its embedded synthetic tag read
  either, so a declaration is required.
- Removing a main document leaves its supplementary documents active.
- Automatic full-text retrieval covers Europe PMC open-access XML only.

### Review, derived values, comparison

- Any reviewer edit of a finding's content marks derived values that used
  it stale and resets its comparison eligibility, including edits to fields
  the computation did not use.
- Uncertainty is scaled by a unit conversion. It is not propagated through
  `difference` or `ratio`.
- A comparison set chosen by filter holds at most 500 findings.

### Deduplication

- Blocks larger than `max_block_size` (default 200) are skipped, so
  duplicates that share only an oversized block are not proposed. The number
  of skipped blocks is reported.

### Outputs

- The HTML report shows at most 500 findings and 500 paper records. The
  complete tables are in the CSV, Excel and JSON files.
- An evidence map shows the 12 most frequent terms and sums the rest under
  "other".
- Figures and the HTML report are drawn for a light surface only. Text in
  SVG figures is rendered as paths and is not selectable.
- A figure does not state the finding filter it was drawn under; the HTML
  report states it.
- The client identification stored with a search run is not part of the
  export tables.
- JSON exports are indented, which makes them larger and slower to write.
- Some CLI commands print results for a synthetic project without the
  synthetic banner. Files, exports, figures, reports and the dashboard
  always carry it.

### Dashboard

- The audit page shows at most the 100 most recent matching events. The full
  log is available with `geodesic audit list --limit N`.
- The document list shows at most 500 rows.
- The reviewer name is free text. The `reviewers` list in configuration is
  not used to restrict it.
- Download buttons were not exercised by automated tests, and the dashboard
  was tested with Streamlit's `AppTest`, not in a browser.

## Engineering

- Only schema version 1 exists. The migration path (backup before change,
  rollback on failure) is tested with a synthetic second migration, not with
  a real schema change.
- Storage is about 7 KB per finding: 698 MB at 104,004 findings in the scale
  test. Workbook import is the slowest step (56 s for 40,000 rows on the
  test machine).
- Installation was exercised with `uv`. Installation with plain `pip` was
  not.
- `pyproject.toml` names the MIT licence as a placeholder. No `LICENSE` file
  is included; choose a licence before distributing the code.
- Tested on macOS (arm64) with Python 3.12 only. `requires-python` allows
  3.11 and later, which was not tested. Linux and Windows were not tested.
