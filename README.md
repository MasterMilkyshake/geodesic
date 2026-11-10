# GEODESIC

A traceable literature discovery and scientific evidence organization toolkit
for one researcher working locally.

GEODESIC imports an existing evidence workbook, searches the literature,
removes duplicates, supports eligibility screening, ingests accessible full
text, records findings anchored to the exact source text, supports human
review, organizes evidence under project-defined categories ("stages"), and
writes reproducible reports. Every finding links to the paper, the place in
the source it came from, and the history of who did what to it.

It is domain-neutral: stage names, search concepts, eligibility criteria and
vocabulary all come from your project configuration. Nothing in the code,
tests or examples depends on a research field.

What it does not do: draw conclusions, rank evidence, pool values, present
unreviewed extraction as verified, or claim that a search was exhaustive.
See [docs/LIMITATIONS.md](docs/LIMITATIONS.md) for what is not implemented.

## Install

Python 3.11 or later. The build and all recorded test runs used Python 3.12
with [uv](https://docs.astral.sh/uv/):

    uv venv --python 3.12 .venv
    uv pip install -e ".[dev]" --python .venv/bin/python

With plain `venv` and `pip` the equivalent is
`python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"` (this route was
not exercised during the build).

The commands below assume the environment is active (`source .venv/bin/activate`)
or that you call `.venv/bin/geodesic` and `.venv/bin/python` directly.

## See it work: the offline demonstration

    geodesic demo --dir /tmp/geodesic-demo

One command runs the complete workflow in a new **synthetic** project, without
any network access: workbook profiling and import, fixture literature search,
duplicate handling, screening, document ingestion (PDF and XML), extraction,
review, organization, comparison, validation, re-import of a changed workbook,
and a report directory. The inputs are generated fixtures about an invented
inert tracer in engineered reservoirs. Review actions in the demo are made by
*simulated* reviewers and are recorded and exported as simulated.

Then look at the result:

    geodesic dashboard --project /tmp/geodesic-demo/project

The output of one demo run is distributed in
[examples/synthetic_demo/](examples/synthetic_demo/).

## Using it on your own workbook

Real project data lives in a project data directory that you choose, outside
this source tree. A real project is refused inside a version-controlled
directory or inside the source tree. Every path is given on the command line
or through an environment variable; nothing is hardcoded.

### 1. Create a project

Copy [config/project.example.yaml](config/project.example.yaml), set the
project name and your stage names and definitions, then:

    geodesic init --project ~/geodesic-data/my-project --config my-project.yaml --data-class real

`--data-class` (`real` or `synthetic`) is fixed at creation and cannot be
changed afterwards. Set `GEODESIC_PROJECT` to the project directory to omit
`--project` from later commands.

Tell GEODESIC who you are (review actions are recorded under this name):

    export GEODESIC_REVIEWER="Your Name"

or copy [config/local.example.yaml](config/local.example.yaml) to
`<project>/local.yaml`. The same file holds the contact address that is sent
to providers that ask for one; see [Configuration](#configuration).

### 2. Profile the workbook and write a starter mapping

    geodesic profile-workbook /path/to/workbook.xlsx --out /path/to/profile --project ~/geodesic-data/my-project

This reads the workbook (it is never written to) and produces `profile.md`,
`profile.json` and `mapping.starter.yaml`. The profile describes structure
only: sheets, likely header rows, column inventory, row counts, merged cells,
hyperlinks, comments and formatting.

### 3. Check and confirm the mapping

Open `mapping.starter.yaml`. Every entry the profiler inferred is marked
`confirmed: false`. Check each one against your workbook, correct it, and set
`confirmed: true`. The file documents the available sheet roles, column roles
and stage modes in its header.

### 4. Import through the mapping

    geodesic import-workbook /path/to/workbook.xlsx --mapping /path/to/profile/mapping.starter.yaml --declare real --project ~/geodesic-data/my-project

The importer refuses to run while a required mapping entry is unconfirmed. It
copies the workbook byte for byte into the project (named by its SHA-256),
reads that copy, and prints reconciliation counts: cells read, evidence rows
per sheet, papers, findings by kind and statement type, unparsed content,
parse failures, items needing review, and a comparison with any summary counts
you mapped. Imported summaries and citations are stored as imported evidence
that needs verification, not as verified facts.

Importing a changed version of the workbook later is safe: findings are
recognised by content, not by row position, and earlier versions stay
preserved. See [docs/WORKBOOK_IMPORT.md](docs/WORKBOOK_IMPORT.md).

### 5. Write and confirm your protocol

    geodesic protocol template protocol.yaml
    # fill in protocol.yaml yourself
    geodesic protocol load protocol.yaml
    geodesic protocol confirm

GEODESIC ships an empty template and never drafts criteria. Until you confirm
a protocol the project is explicitly provisional: imports, exploratory
searches and draft screening suggestions work; eligibility decisions cannot be
finalised and automated exclusions are disabled.
See [docs/SCREENING.md](docs/SCREENING.md).

### 6. Continue in the dashboard or on the command line

    geodesic dashboard

Every workflow step has a dashboard page and a CLI command, and both call the
same functions.

| Step | Dashboard page | CLI |
|---|---|---|
| Research scope | 1 Protocol | `protocol` |
| Workbook import | 2 Workbook import | `profile-workbook`, `import-workbook` |
| Literature search | 3 Literature search | `search`, `import-search-file`, `crossref` |
| Deduplication | 4 Duplicates | `dedup` |
| Eligibility screening | 5 Screening | `screen`, `assess` |
| Full text | 6 Documents | `documents` |
| Extraction | 7 Extraction | `candidates`, `extract` |
| Review | 8 Review findings | `review` |
| Organize | 9 Organise | `hierarchy`, `vocabulary`, `findings`, `papers` |
| Compare | 10 Compare | `compare`, `derived` |
| Evidence maps | 11 Evidence maps | `map`, `missing`, `modeling` |
| Validation | 12 Validation | `validate` |
| Reports and exports | 13 Reports and exports | `report`, `figures`, `export` |
| Audit trail | Audit trail | `audit`, `status` |

`geodesic <command> --help` describes each command.

## Literature search

    geodesic search translate            # show the query built from search.concepts, without running it
    geodesic search run --max-records 200
    geodesic search list
    geodesic search verify               # re-read stored responses and check their hashes

The default connector is Europe PMC, which needs no credentials. Search
concepts come from `search.concepts` in `project.yaml`, or pass an exact
source-specific query with `--query`. Raw responses are stored with their
SHA-256; a run is reproduced by re-reading that snapshot. Results exported
from other tools can be imported as RIS or CSV with `geodesic import-search-file`.
Crossref is used for identifier validation and metadata enrichment
(`geodesic crossref`), not as a search source.

Full text is retrieved automatically only for records a source reports as
open access, through the Europe PMC `fullTextXML` API
(`geodesic documents fetch-oa`). Anything else you attach yourself with
`geodesic documents add`. GEODESIC does not contact publisher sites.

A bounded live check of the integrations, which reports mechanics, counts and
response schema only:

    geodesic live-smoke

## Configuration

| Where | What | Exported? |
|---|---|---|
| `<project>/project.yaml` | Stages, parser vocabulary, deduplication scores, search settings. After editing run `geodesic reload-config`. | Yes |
| `<project>/local.yaml` or `~/.config/geodesic/config.yaml` | Reviewer name, provider contact address | Never |
| Environment or `<project>/credentials.env` | Credentials | Never stored anywhere |

Environment variables: `GEODESIC_PROJECT` (project directory),
`GEODESIC_HOME` (directory holding projects; default `~/geodesic-data`),
`GEODESIC_REVIEWER`, `GEODESIC_CONTACT_EMAIL`, `GEODESIC_CROSSREF_PLUS_TOKEN`
(optional). The default connectors need no credential. Examples of every file
are in [config/](config/).

## Tests

    python -m pytest tests                  # standard suite: offline, synthetic fixtures only
    python -m pytest tests -m network       # live provider checks (needs network access)
    python -m pytest tests -m scale -s      # scale smoke test: about 10,000 papers and 100,000 findings

The standard suite never touches the network. The network and scale tests are
deselected unless asked for. The scale test measures and prints runtimes; its
size and working directory can be changed with `GEODESIC_SCALE_PAPERS`,
`GEODESIC_SCALE_FINDINGS` and `GEODESIC_SCALE_DIR`.

All fixtures are generated and synthetic, including the PDF and XML fixture
documents. Observed results are recorded in [docs/TESTING.md](docs/TESTING.md).

## Documentation

| Document | Content |
|---|---|
| [DECISIONS.md](DECISIONS.md) | Non-obvious design choices, provider choices and what was verified live |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Modules, layering, project directory layout |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | Tables, identifiers, statuses, audit log, migrations |
| [docs/SOURCE_LOCATIONS.md](docs/SOURCE_LOCATIONS.md) | The one source-location system and grounding |
| [docs/WORKBOOK_IMPORT.md](docs/WORKBOOK_IMPORT.md) | Profile, mapping file, import rules, re-import |
| [docs/SEARCH.md](docs/SEARCH.md) | Search runs, snapshots, file import, Crossref, open-access full text |
| [docs/DEDUPLICATION.md](docs/DEDUPLICATION.md) | Identifier matches, similarity score, reversible grouping |
| [docs/SCREENING.md](docs/SCREENING.md) | Protocol versions, suggestions, decisions, re-review |
| [docs/EXTRACTION.md](docs/EXTRACTION.md) | Documents, candidates, manual extraction, review, units, comparison, validation |
| [docs/REPORTS.md](docs/REPORTS.md) | Dashboard, evidence maps, figures, exports, report directory |
| [docs/TESTING.md](docs/TESTING.md) | Test commands and observed results, live verification, scale measurements |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | What is unimplemented, partial or unverified |
| [docs/DEPENDENCY_LICENSES.md](docs/DEPENDENCY_LICENSES.md) | Licences of installed dependencies |
