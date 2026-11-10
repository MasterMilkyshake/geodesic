# Source locations and grounding

Every piece of evidence points at stored text through one source-location
system (`src/geodesic/locations.py`). A location is an address plus the
excerpt that was found there. All addresses resolve against text held in the
project database, so a location can be re-checked at any time without the
original file.

## Location kinds

| Kind | Address |
|---|---|
| `workbook_cell` | `workbook_version_id` + sheet name + cell reference in A1 style + character span in the stored cell text |
| `pdf_page` | `snapshot_id` (one parse of one document version) + `unit_key` = page index + character span in the stored page text |
| `xml_element` | `snapshot_id` + `unit_key` = structural path of the element, for example `body[1]/sec[2]/sec[1]/p[3]` + character span |
| `table_cell` | `snapshot_id` + `table_key` + `row_idx` + `col_idx` + character span in the stored cell text |
| `html_element` | `snapshot_id` + `unit_key` + character span. The kind is defined, but HTML ingestion is not implemented, so no such location can be created yet |

A screening decision made from a bibliographic record uses one further
address, `record_field`: the paper record, the field (`title` or `abstract`)
and a character span in the stored field text. It is stored with the decision
and checked under the same span rules. Full-text screening decisions use the
document kinds above.

## Indexing conventions

- **Character spans** are 0-based and half-open, `[start, end)`, counted in
  Unicode code points of the stored text. `start = 4, end = 9` addresses five
  characters. A location without a span addresses the whole stored text of
  the unit.
- **PDF page indices are 0-based**: `0` is the first page of the file,
  whatever page number is printed on it.
- **Table rows and columns are 0-based.**
- **Workbook cells** follow the spreadsheet: `Sheet 'Release'`, cell `C7`.
- **Stored text** is the text as the reader returned it, with line endings
  normalised and NUL characters removed. Spans refer to this stored text, not
  to byte offsets in the file.

## Resolving

A location **resolves** when the stored text at the address equals the
excerpt recorded with the location. The result is stored with the location
and is recomputed by every validation run (`geodesic validate run`).

Resolving shows that the excerpt is really there. It does not show that the
evidence was interpreted correctly; that is a reviewer's judgement.

Locations are never rewritten. A re-import or an edit adds new locations for
the new finding version and marks the earlier ones as not current. A document
parsed again gets a new snapshot, and locations made against the earlier
snapshot keep resolving against it.

## Location roles

A finding has one location with role `supporting_excerpt`. It may have
further locations with other roles, for example `limitation` or
`context:outcome`, when part of its content comes from another place in the
same source.

## Grounding

A finding is **grounded** when all of the following hold
(`findings.grounding_problems`):

1. It has a supporting-excerpt location.
2. Every one of its locations resolves.
3. Each transcribed literal (`value_literal`, `unit_original`,
   `uncertainty_literal`, `timepoint_literal`, `sample_size_literal`) has a
   recorded span, and the stored text at that span is exactly the literal.
4. No literal's span cuts a longer number or word. "25" taken out of "250",
   "8.5" out of "18.5" or "m" out of "min" are present as characters but are
   not what the source states.

A finding that fails any of these is `unsupported`. An unsupported finding
cannot be verified. When a rule-based component or a manual extraction
proposes a literal that is absent from its span, the proposal is refused and
recorded in `parse_failure` with the claimed string and the span.

For a manual extraction, the finding's source wording is not typed in: it is
the stored text at the span the reviewer chose.

## Finding a location

    geodesic documents find <snapshot-or-document-id> "text to find"

prints each occurrence with its unit key and character offsets.
`geodesic extract add --snapshot … --unit … --quote "…"` accepts the exact
excerpt instead of offsets when the excerpt occurs once in the unit. The
dashboard pages "7 Extraction" and "8 Review findings" show the stored text
of the unit with the excerpt and literals marked.

## In exports

Each exported finding carries its locations in the `source_locations` table:
kind, address fields, span, excerpt, whether it resolves, and a readable
address such as `document snapshot snp_… page index 2 chars 118-164`.
Excerpts in exports are short quotations, cut at 600 characters, never whole
documents.
