# Literature search, identifier validation and open-access full text

What was read in each provider's documentation, when, and what was verified
live is recorded in `DECISIONS.md` section 5 and `docs/TESTING.md`.

No connector claims exhaustive coverage. A search run is what one provider
returned for one query at one time.

## Search concepts and query translation

Search concepts come from `search.concepts` in `project.yaml`. GEODESIC ships
none.

    search:
      concepts:
        - label: concept_a
          terms: ["first term", "second term"]
        - label: concept_b
          terms: ["third term"]
      provider_field: {europepmc: TITLE_ABS}     # optional field prefix
      provider_query_override: {}                # provider -> exact query, used verbatim

Synonyms within a group are joined with OR, groups with AND. Multi-word terms
are quoted. `geodesic search translate` prints the query without running it.
`geodesic search run --query '…'` runs an exact source-specific query as
written. The executed query string is always stored with the run.

## Running a search

    geodesic search run [--query Q] [--year-from Y] [--year-to Y] [--open-access]
                        [--max-records N] [--page-size N]
    geodesic search resume RUN_ID
    geodesic search list
    geodesic search show RUN_ID
    geodesic search verify [RUN_ID]

The default and only search connector is **Europe PMC**
(`GET /search`, `format=json`, `resultType=core`, cursor pagination). It needs
no credential.

A run stores:

- provider, interface, mode (`live`, `fixture` or `file_import`), the executed
  query, concept groups, filters, page size and retrieval limit;
- the provider's reported total, the number retrieved, the number of pages,
  and whether the retrieval limit was reached;
- every raw response as received, with its SHA-256 and size, under
  `raw_responses/`, and a description of the request without credentials or
  contact address;
- each record in the order received, with its hash and the paper record it
  was linked to;
- the protocol version and its status at the time. A run made under a
  provisional protocol is labelled **exploratory**;
- the client identification that was sent: tool name and version, and
  whether a contact address was supplied (never the address itself).

Run status:

| Status | Meaning |
|---|---|
| `complete` | Every record the provider reported was retrieved |
| `truncated` | The retrieval limit was reached and the provider reported more records. The cursor is stored |
| `incomplete` | Retrieval stopped on an error after the configured retries, or pagination ended before the reported total. The errors are stored with the run, and the cursor where there is one |
| `failed` | The run was refused, for example because a response's declared data class contradicts the project's |

`search resume` continues a truncated or incomplete run from its stored
cursor, in the same run.

### Request behaviour

- A pause of `search.min_interval_seconds` (default 0.5 s) separates requests.
- Transport errors and HTTP 429, 500, 502, 503 and 504 are retried up to
  `search.max_retries` times with exponential backoff; `Retry-After` is
  honoured.
- The client sends a configurable tool name (`search.tool_name`). A contact
  address is sent only when you configure one (`contact_email` in
  `local.yaml`, or `GEODESIC_CONTACT_EMAIL`). It is masked in logs and never
  stored in the database, raw-response archive or exports.
- Live requests are refused in a synthetic project. A synthetic project
  replays stored fixture responses (`--fixture-dir`) through the same code.

### Linking records to papers

1. `exact_duplicate_record`: the identical provider record was imported
   before (same hash) and maps to the same paper.
2. `validated_identifier_match`: the record's identifier equals a validated
   identifier of exactly one existing paper and the titles do not clearly
   differ. Differing metadata is recorded as a conflict, never overwritten.
3. `new_paper` otherwise. Possible duplicates are then proposed for review
   (`docs/DEDUPLICATION.md`).

Membership of a record in a run survives deduplication: `search_result`
always says which runs returned a paper.

### Reproducing a run

A run is reproduced by re-reading its stored snapshot, not by asking the
provider again. `geodesic search verify` checks that every stored raw
response still has its recorded SHA-256, parses the stored bytes again, and
checks that the records, in order, are exactly those recorded. Running the
same query later is a new run and may return different records.

## Importing results from other tools

    geodesic import-search-file FILE [--format ris|csv] [--source-label TEXT] --declare real

The file is preserved byte for byte under `sources/search_files/` and
recorded as a search run with mode `file_import`. Identifiers read from a
file are **unvalidated**, because a file can be edited by hand: a matching
DOI makes a duplicate candidate for review, not an automatic grouping.

**RIS**: UTF-8. A field line is a two-character tag, two spaces, a hyphen
and the value. `TY` starts a record and `ER` ends it. Tags used: `TY`, `TI`
or `T1`, `AU` or `A1` (repeatable), `PY` or `Y1` (first four digits), `JO`,
`JF`, `T2` or `JA`, `DO`, `AB` or `N2`, `UR`, `LA`, `AN` or `ID`. Other tags
are kept with the stored record and not interpreted.

**CSV**: UTF-8, comma-separated, one header row. Columns, matched without
regard to case: `title`, `authors` (separated by `;`), `year`, `journal`,
`doi`, `pmid`, `pmcid`, `abstract`, `url`, `publication type`, `language`,
`record id`. Other columns are kept and not interpreted. A row with no
title, DOI, PMID or PMCID is skipped and counted.

## Crossref: identifier validation and enrichment

    geodesic crossref validate [--paper ID] [--limit N]
    geodesic crossref propose PAPER_ID
    geodesic crossref decide CANDIDATE_ID accepted|rejected

Crossref is not used as a search source.

- **`validate`** looks up each unvalidated DOI (`GET /works/{doi}`). A 200
  marks the identifier `validated`, fills empty metadata fields and records
  differing values as conflicts. A clearly different registry title is
  flagged as a possible misassigned identifier and nothing is filled. A
  paper known only by a short author-year citation ("Arden 2021 · Fig. 2")
  shares no title words with the registry; for such a paper the check is
  instead whether the citation names the registry's first author and a year
  within one of the registry year, and the basis used is recorded with the
  lookup (`agreement_basis`). A 404
  marks the identifier `not_found` in Crossref on that date.
- The work record's `updated-by` field is stored as a dated **integrity
  check**: the notices listed, or "none found in Crossref on <date>". That is
  a statement about one source on one date, not a statement that a paper was
  never corrected or retracted.
- **`propose`** sends the citation text of an unresolved paper to
  `query.bibliographic` and stores the answers as proposed candidates.
  Nothing is attached to the paper until a reviewer accepts one with
  **`decide`**.

Validation shows that an identifier exists and what the registry holds for
it. It verifies nothing a workbook says about the paper's content.

## Open-access full text

    geodesic documents fetch-oa --candidates        # list what would be attempted
    geodesic documents fetch-oa [PAPER_ID] [--limit N]

One route is implemented: Europe PMC `GET /{PMCID}/fullTextXML`. A retrieval
is attempted only when a search source reported the record as open access
and the record has a PMCID. The retrieved XML is preserved, parsed and
attached as a document, with the licence the search source reported and the
licence stated in the XML.

Nothing else is tried: no publisher sites, no link resolvers, no scraping, no
route around access controls. A failed retrieval is recorded as
`retrieval_failed` with the reason. It is never presented as "no full text
exists". Any other document you are entitled to use is attached by hand with
`geodesic documents add`, and a paper for which you could not obtain full
text is marked with `geodesic documents unavailable`.

## Bounded live check

    geodesic live-smoke

Runs a small query from configuration (`search.smoke_query`,
`search.smoke_max_records`, `search.smoke_page_size`) in a throw-away real
project in the system temporary directory, which is deleted afterwards. It
checks retrieval mechanics, counts and response schema only: pagination and
the retrieval limit, the stored snapshot, the year and open-access filters,
one Crossref lookup, and one full-text XML retrieval. Its report contains
counts, status codes, field-presence tallies and hashes. It does not print,
summarise or analyse the retrieved records. A check that could not be made
is reported as unverified, not as passed.
