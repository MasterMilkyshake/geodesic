# Deduplication

Duplicate handling (`src/geodesic/dedup.py`) follows one principle: the
application groups records on its own only when a validated identifier says
they are the same entity. Everything else is a candidate that a reviewer
decides. Nothing is deleted or rewritten, and every grouping can be reversed.

Detection runs after each search run, file import and identifier validation,
and on demand with `geodesic dedup run`. It is safe to repeat: a pair that
already has a relation of the same type, whatever its status, is left alone,
so a reviewer's decision is never undone.

## Pass 1: shared identifiers

Records carrying the same normalised DOI, PMID or PMCID are examined.
Identifiers marked `invalid_syntax` or `not_found` are ignored.

An identifier is **validated** when a bibliographic provider supplied it as
the record's own identifier, a registry lookup confirmed it
(`geodesic crossref validate`), or a reviewer confirmed it. Identifiers read
from a workbook or an imported file are unvalidated.

| Situation | Result |
|---|---|
| Validated on both records | Grouped automatically under the earlier record (`validated_identifier_match`), recorded as an automated action |
| Unvalidated on either record | `duplicate` candidate for review (`identifier_match_unvalidated`) |
| Same identifier, but another identifier scheme differs, or both titles are known and their similarity is below `identifier_title_conflict_below` (default 0.35) | `identifier_conflict` for review: a possible misassigned identifier. Not grouped |

An undecided identifier candidate is settled automatically once the
identifier has since been validated on both records.

## Pass 2: similarity

Similarity only ever produces candidates.

**Score.** Three components, each in [0, 1]:

| Component | Definition | Default weight |
|---|---|---|
| Title | `difflib.SequenceMatcher` ratio over the word sequences of the two titles after case, punctuation and whitespace normalisation (2 × matching words ÷ total words) | 0.70 |
| First author | 1 when the family-name keys are equal, 0 when they differ | 0.15 |
| Year | 1 when equal, 0.5 when one year apart, 0 otherwise | 0.15 |

A component missing on either record is left out and the remaining weights
are renormalised. Without both titles there is no score. The score, each
component and the weights used are stored with the candidate and shown to the
reviewer.

**Candidate rule.** A pair becomes a candidate when its score is at least
`candidate_threshold` (default 0.80) and its title component is at least
`title_floor` (default 0.60).

**Which pairs are scored.** Comparing every pair is quadratic, so records
are first placed in blocks and only pairs sharing a block are scored. A
record with a title gets up to three block keys: the first 24 characters of
its normalised title; its three longest words of five or more characters;
and its first-author key. A block with more than `max_block_size` members
(default 200) is **skipped**, and the number of skipped blocks is reported
as `blocks_skipped_too_large`. Two consequences:

- A record without a structured title is never compared by similarity. This
  includes papers created from workbook citation text.
- Duplicates that share only an oversized block (for example a very common
  first-author key) are not proposed.

All of these values are in `dedup:` in `project.yaml`.

**Preprints.** When one record of a candidate pair is a preprint and the
other is published, the candidate has type `preprint_published`. Confirming
it links the two records. They are never grouped as identical.

## Reviewer decisions

    geodesic dedup list [--status candidate|confirmed|rejected|all] [--type …]
    geodesic dedup decide RELATION confirmed|rejected [--as-type …] [--canonical PAPER] [--note …]
    geodesic dedup relate PAPER_A PAPER_B duplicate|preprint_published|same_study_report
    geodesic dedup confirm-identifier-matches [--min-title-similarity 0.9]
    geodesic dedup groups
    geodesic dedup ungroup PAPER --reason "…"

- **Confirming a `duplicate`** groups the later record under the earlier one,
  or under the record named with `--canonical`.
- **`--as-type`** re-classifies a candidate, for example a duplicate
  candidate that is really a preprint and its published version, or two
  reports of one study (`same_study_report`). The original candidate is
  rejected, the new relation is confirmed, and the records stay separate.
- **Confirming an `identifier_conflict`** marks the identity of both records
  as `conflict`.
- **`confirm-identifier-matches`** is a bulk decision made in the reviewer's
  name. It confirms only identifier candidates whose two titles are both
  known and at least the given similarity. The rest stay in the queue.
- Decisions need a named reviewer. Automated components cannot make them.

## What grouping does

Grouping sets `canonical_id` on the member record and writes a `paper_merge`
row with its basis. The member record, its identifiers, its source records
and its findings are unchanged and still point at the member. Screening and
counts use the canonical record. A paper's membership in search runs is
kept, so the flow summary can still say how many records each source
returned and how many were removed as duplicates.

## Reversal

`geodesic dedup ungroup` needs a reason. It clears `canonical_id`, marks the
grouping `reversed` and the underlying relation `rejected`. Because the pair
now has a decided relation, later detection runs do not group it again.

## Observed at scale

In the scale test (about 14,000 paper records) the similarity pass scored
about 310,000 pairs and skipped 36 oversized blocks. See `docs/TESTING.md`.
