# Protocol and eligibility screening

## The protocol

The protocol states the research scope and the eligibility criteria. It
belongs to the researcher. GEODESIC ships an empty template and never drafts
criteria or infers them from a workbook.

    geodesic protocol template protocol.yaml      # write the empty template
    geodesic protocol load protocol.yaml          # store it as a new provisional version
    geodesic protocol show
    geodesic protocol confirm
    geodesic protocol history
    geodesic protocol export protocol-v3.yaml

`load` and `confirm` need a reviewer name (`--reviewer` or
`GEODESIC_REVIEWER`). The same can be done on the dashboard page
"1 Protocol".

The protocol holds: research question; target systems; interventions;
comparators; outcomes and measurement types; study designs; date limits;
language limits; the treatment of preprints, reviews, conference abstracts
and other publication types (`include`, `exclude`, `flag_for_review`);
inclusion and exclusion criteria, each with an identifier, text, the stages
it applies to and optional match terms; the role of supporting evidence from
outside the primary scope; and which automated rules, if any, are authorised.

### Provisional and confirmed

Every project starts with an empty **provisional** protocol. While the
current version is provisional:

- imports, exploratory searches and draft screening suggestions work;
- searches are labelled exploratory, with the protocol version;
- screening decisions are stored with finality `provisional`;
- a decision requested as final (`--final`) is refused;
- automated decisions are refused.

`protocol confirm` is refused until the protocol is complete: a research
question, at least one criterion, text for every criterion, unique criterion
identifiers, a decided treatment for each publication type, and a stated
role for supporting evidence. The refusal lists what is missing.

### Versions

Every load of changed content is a new version with a hash, the list of
parts that changed, and who loaded it. A new version of a confirmed protocol
is provisional until it is confirmed again. Earlier versions are kept
unchanged. Every search run and every screening decision stores the protocol
version it was made under and whether that version was confirmed at the
time.

## Screening stages

Title/abstract screening and full-text screening are separate stages with
separate decisions. A paper reaches full-text screening when its
title/abstract outcome is `include` or `uncertain`. Decisions are recorded on
the canonical record of a duplicate group.

Eligibility is kept apart from three other assessments, each recorded with
`geodesic assess` and its own rationale:

| Dimension | Values |
|---|---|
| `primary_relevance` | `direct`, `supporting`, `not_relevant`, `unclear` |
| `quantitative_comparison` | `suitable`, `limited`, `not_suitable`, `unclear` |
| `mathematical_modeling` | `suitable`, `limited`, `not_suitable`, `unclear` |

## Suggestions

    geodesic screen suggest --stage title_abstract

Suggestions are drafts made by documented rules from the current protocol.
They are stored in their own table and are never counted as decisions. The
rules, in order:

1. **Publication type.** A record whose source-supplied publication type
   matches a `type_terms` entry of a type the protocol excludes gets an
   exclusion draft. A type marked `flag_for_review` adds a note and makes the
   draft `uncertain`.
2. **Date limits.** A stated year outside the limits gets an exclusion
   draft.
3. **Language.** A stated language outside the protocol's list gets an
   exclusion draft.
4. **Exclusion criteria with match terms.** A term found in the title,
   abstract or (at full-text stage) stored document text gets an exclusion
   draft citing the place it was found.
5. **Inclusion criteria with match terms**, if no exclusion applied. A term
   found for every such criterion gives `include`. A criterion with no term
   found gives `uncertain` with reason `insufficient_information`.

Nothing is suggested from information a record lacks. A missing year,
language, publication type, abstract or term never produces an exclusion.
Criteria without match terms are named in the draft as not evaluated.

Term matching is case-insensitive on whole words or phrases. A trailing `*`
matches any word ending (`review*` matches "reviews"). The terms are yours;
the toolkit supplies none.

## Decisions

    geodesic screen queue --stage title_abstract [--mine] [--search-run RUN]
    geodesic screen decide PAPER --stage S --decision include|exclude|uncertain
            [--reason-kind …] [--criterion ID] [--reason TEXT]
            [--support TEXT --support-field title|abstract] [--final]
    geodesic screen accept SUGGESTION [--final]
    geodesic screen dismiss SUGGESTION
    geodesic screen history PAPER

A decision records: paper, stage, decision, reason kind, criterion, written
reason, reviewer, whether the decider was human, automated or simulated,
time, protocol version and its status, finality, and optional supporting
text with its location. Supporting text is checked against the stored title,
abstract or document text when the decision is recorded.

Rules enforced when a decision is recorded:

- A decision needs a named reviewer. A rule-based component cannot decide,
  except under authorised automation (below).
- **`criterion_not_met` and `insufficient_information` are different
  reasons.** An exclusion for `criterion_not_met` must name the criterion.
- **A paper cannot be excluded at title/abstract stage for
  `insufficient_information`.** An abstract that omits a detail is
  `uncertain` and goes on to full-text screening.
- A full-text decision is refused while the title/abstract outcome is
  `exclude` or an unresolved conflict.
- A suggestion can be accepted only under the protocol version it was
  generated from.
- Under a confirmed protocol a decision is `final`; under a provisional one
  it is `provisional`.

### Changing a decision, disagreement, adjudication

Decisions are never edited or deleted. A reviewer who changes their mind
records a new decision, which supersedes their earlier one; the earlier one
stays in the history. Different reviewers decide independently. When their
current decisions differ, the paper's outcome at that stage is `conflict`
until someone records an adjudication:

    geodesic screen conflicts
    geodesic screen adjudicate PAPER --stage S --decision … --reason "…"

The adjudication is stored as its own decision with role `adjudication`. The
independent decisions stay in the history.

### Authorised automation

    geodesic screen automate --stage title_abstract

Automated decisions are off by default. They run only when the **confirmed**
protocol sets `automation.<stage>.authorized: true` and lists rules in
`allowed_rules`. The rules that can be authorised are `publication_type`,
`date_limit` and `language`. Only open exclusion suggestions from those rules
are applied, and only to papers with no current decision. Each is stored
with decider kind `automated` and counted separately from human decisions
everywhere.

## Protocol changes and re-review

A protocol change never rewrites a decision. When a new version is stored,
each current decision is compared with the parts that changed:

- An exclusion citing a criterion or rule is flagged when that criterion or
  rule changed.
- An inclusion or `uncertain` decision is flagged when any eligibility part
  changed, because it depends on the criteria as a whole.
- Changes to notes or to automation settings flag nothing.

When a protocol is confirmed, decisions made while it was provisional stay
provisional and are flagged. They are not promoted.

    geodesic screen rereview                     # list flagged decisions with the reason
    geodesic screen rereview --reaffirm DECISION_ID --note "…"

A flagged decision is cleared by recording a new decision or by reaffirming
it under the current version. Both are new rows; the flagged row stays. A
reviewer reaffirms their own decisions (or an automated one); another
reviewer's decision stays flagged until that reviewer looks at it again.

## Flow summary

`geodesic screen summary` and the report's flow table and figure count:
records retrieved per source type, duplicates removed, records without a
decision, and at each stage the papers included, excluded (by reason) and
uncertain, conflicts, and provisional versus final outcomes. Human,
automated and simulated decisions are counted separately. Suggestions are
not counted as decisions. The layout follows records from identification
through screening in the manner of a PRISMA flow diagram; no claim of
compliance with a reporting guideline is made.
