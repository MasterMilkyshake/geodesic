# Workbook import

An existing evidence workbook is brought in through three steps: profile it,
confirm a mapping, import through the mapping. Nothing about a workbook's
layout is in the code. A new layout means a new mapping file.

Imported summaries and citations are stored as **imported evidence that needs
provenance and verification**. Nothing from a workbook is treated as an
independently verified fact.

## 1. Profile

    geodesic profile-workbook WORKBOOK.xlsx --out DIR --project PROJECT

The workbook is opened read-only. The command writes three files to `DIR`:

| File | Content |
|---|---|
| `profile.md` | Readable description of the structure |
| `profile.json` | The same, machine-readable |
| `mapping.starter.yaml` | A starter mapping with every inferred entry marked `confirmed: false` |

The profile describes structure: sheets and their dimensions, likely header
rows, a column inventory with non-empty cell counts, row counts, merged ranges,
hyperlinks, comments and formatting. From cell content it copies only header
text and, for a column that looks like a stage column, the distinct terms
found there (so they can be mapped to project stages). It does not summarise
or interpret evidence.

Inference uses header text only and is a guess. The reason for each guess is
written to `inferred_by`.

## 2. The mapping file

A mapping is YAML. Its header comment lists the allowed values.

    workbook_key: main            # names the workbook lineage; later versions reuse it
    sheets:
      - sheet: <sheet name>
        role: evidence            # evidence | summary | ignore
        confirmed: false
        header_row: 1             # 1-based
        data_start_row: 2         # 1-based
        data_end_row: null        # inclusive; null = last used row
        merged_cells: fill_down   # fill_down | anchor_only
        stage:
          mode: fixed             # fixed | column | none
          stage_keys: [<stage key>]
          value_map: {}           # column mode: workbook term -> project stage key
          separator: ";"          # column mode: splits a cell naming several stages
          confirmed: false
        columns:
          - column: A
            header: <header text seen>
            role: paper_reference
            confirmed: false
    summary_counts:
      - sheet: <summary sheet>
        cell: B2
        measures: evidence_rows   # evidence_rows | distinct_papers
        stage_key: <stage key>    # or null for the whole workbook
        confirmed: false

**Column roles**: `paper_reference`, `doi`, `pmid`, `url`, `title`, `authors`,
`year`, `journal`, `stage`, `narrative_findings`, `measurements`,
`limitations`, `system_context`, `experimental_context`, `intervention`,
`comparator`, `outcome`, `measurement_method`, `timepoint`, `unmapped`.

- `narrative_findings`: free text that may hold several facts in one cell.
- `measurements`: cells that each hold one measurement.
- `limitations`: caveats and limits on interpretation.
- `unmapped`: stored cell by cell and kept with the row as an unknown field.
  Not interpreted, never dropped.

Several columns may share a content role (`narrative_findings`,
`measurements`, `limitations`, `unmapped`). Every other role may be given to
one column per sheet.

**Stage modes**: `fixed` assigns every row of the sheet to `stage_keys`.
`column` reads the stage from the column with role `stage`, translating the
workbook's own terms through `value_map`. `none` assigns no stage. Stage keys
must be stages of the project (`project.yaml`). A row may belong to several
stages.

### What must be confirmed

The importer checks the mapping against the workbook and the project and
lists every problem at once. It refuses to run while any of these is
unconfirmed:

- each sheet's role (including sheets with role `ignore`),
- each column with a role other than `unmapped` on an evidence sheet,
- each evidence sheet's stage assignment,
- each stage term in `value_map` without a stage key,
- each summary count.

It also refuses a mapping that omits a workbook sheet, names an unknown sheet
or stage key, has no evidence sheet, or has an evidence sheet without a
column identifying the paper (`paper_reference`, `doi`, `pmid` or `title`).

## 3. Import

    geodesic import-workbook WORKBOOK.xlsx --mapping MAPPING.yaml --declare real --project PROJECT

`--declare` states the provenance of a workbook that carries no embedded
synthetic tag. The declared class must equal the project's class.

What happens, in one transaction:

1. **The source is preserved.** The file is copied byte for byte to
   `sources/workbooks/<sha256>.xlsx`, the copy is verified against the hash
   and made read-only. The input file is never opened for writing. The import
   reads the preserved copy.
2. **Every cell is stored** in `source_cell`: text, hyperlink, comment, merge
   membership and basic formatting, for all sheets including ignored ones.
3. **Paper references** become paper records. A DOI or PMID is read from its
   own column, from a hyperlink or from the citation text, and stored as
   `unvalidated`. A reference without a syntactically valid identifier
   becomes an explicitly `unresolved` record that keeps the citation text as
   written. No title, author or year is derived from citation text. A
   malformed identifier is kept and marked `invalid_syntax`. Metadata that
   differs between rows naming the same paper is recorded as a metadata
   conflict; nothing is overwritten.
4. **Findings** are created from content cells by the deterministic parser
   (below). Each finding has a `workbook_cell` location with the character
   span of its wording and of each transcribed literal.
5. **Stages** are assigned from the mapping, with source `imported`.
6. **Reconciliation counts** are computed and stored with the import run.

Imported findings start as `unreviewed`, or `draft` when the parser flagged
an ambiguity. Their source scope is `workbook_summary`. None is `verified`.

### Row rules

- Identity, stage and context columns use the text that applies to the row.
  Inside a merged range that is the anchor cell (`merged_cells: fill_down`),
  and the location points at the anchor cell.
- Content columns are read only where the text is stored, so a merged cell
  yields its findings once.
- A row with content but no paper reference is not imported as evidence. Its
  cells are recorded as unparsed content (`row_without_paper_reference`).

### The narrative parser

`src/geodesic/textparse.py` is deterministic and uses no model service. It
splits a narrative cell into segments (sentences, and semicolon-separated
clauses when `parser.segment_on_semicolon` is true) and recognises numbers,
units from the explicit unit table, approximation and bound qualifiers,
ranges, `±` expressions, confidence intervals, sample sizes and timepoints.
It records the span of every literal it transcribes.

It does not:

- turn a range or a bound into a point value,
- decide that `±` means SD or SE unless the text says so (it stays
  `unspecified_plus_minus`),
- guess the reference of a percentage or the baseline of a fold change,
- decide whether an observation is a direct measurement or a proxy,
- guess a number with an ambiguous decimal comma. The segment is recorded as
  unparsed content (`ambiguous_decimal_comma`).

A measurement cell without a recognisable quantity is recorded as unparsed
content (`no_quantity_in_measurement_cell`). A stage term absent from
`value_map` is recorded as `unmapped_stage_term`. Qualifier phrases such as
"not independently verified" or "denominator unclear" are kept in the wording
and raise a review flag; add your own under `parser.extra_qualifier_phrases`.

Possible repeats of a finding within one paper (wording similarity at or
above `parser.repeat_similarity_threshold`, default 0.92) are recorded as
`possible_duplicate` relations for review. Nothing is merged.

### Reconciliation output

The import prints, and stores, with a definition for each count: cells read;
evidence rows per sheet; papers referenced, created, matched, resolved and
unresolved; invalid identifiers; metadata conflicts; findings by kind and by
statement type; unparsed content by reason; parse failures; items requiring
review (drafts, flagged, unsupported, stale, unresolved papers, possible
repeats); and for each mapped summary count the workbook's number, the
imported number, and `MATCH` or `MISMATCH`. A mismatch is reported, not
corrected.

## Importing a changed workbook

Import the new file with the same `workbook_key`. It becomes the next version
of that workbook; earlier versions and their cells stay preserved.

Identity across versions is based on content, never on row position:

1. **Exact match**: same paper, same column role, same normalised wording,
   same position among the quantities of that wording, same stages.
2. **Same paper, role and wording** with different stages or a different
   occurrence: matched, recorded as modified.
3. **Similarity** within the same paper, role and quantity position: the
   best earlier finding at or above `parser.reimport_similarity_threshold`
   (default 0.60) is matched when it leads the runner-up by at least
   `parser.reimport_ambiguity_margin` (default 0.10). Otherwise the finding
   is stored as new and an `ambiguous_identity` relation is recorded for a
   reviewer.

A paper of a workbook is recognised by its DOI, otherwise its PMID, otherwise
its normalised citation text, otherwise its normalised title. A
reference whose citation text was reworded is matched to an earlier record
of the same workbook when the two texts are at least 0.85 similar and that
match leads the runner-up by at least 0.05; otherwise a new record is made.

Outcomes:

| Count | Meaning |
|---|---|
| added | Not matched to any earlier finding |
| unchanged | Matched, identical imported content. No new finding version; the finding gains a location in the new workbook version and keeps its review status |
| modified | Matched, content or stages changed. A new finding version is stored; a `verified` or `rejected` finding becomes `stale`, and its earlier reviews and computed values are marked stale |
| absent | An earlier finding is not in this version. It is kept and marked `absent_from_latest`; a `verified` or `rejected` one becomes `stale` |
| restored | Absent in the previous version, present again |

Stable identifiers do not change. Reviews of unchanged findings are kept.

A finding a reviewer edited in the application is compared by its imported
content, not by the edit. While the source is unchanged the reviewer's
version stays current; its spans are re-checked against the new cell text,
and if they no longer resolve the finding becomes `unsupported`. When the
source wording changed, the new imported content becomes the current version,
the reviewer's version stays in the history, and the finding is flagged
`reviewer_edit_superseded_by_source_change`.

Importing the identical file with the identical mapping reports "already
imported; nothing to do". Importing the same file with a changed mapping is a
new import run against the same preserved copy. Importing a file that is an
earlier version of the same `workbook_key` is refused.
