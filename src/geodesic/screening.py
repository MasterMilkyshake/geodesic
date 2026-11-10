"""Eligibility screening: suggestions, decisions, history and counts.

Rules this module enforces
--------------------------
* Title/abstract and full-text screening are separate stages with separate decisions.
* A *suggestion* is a draft produced by documented rules from the protocol. It is
  stored in its own table and never counts as a decision. It becomes a decision
  only when a named reviewer accepts it, or when the **confirmed** protocol
  explicitly authorises automation of that rule at that stage
  (:func:`apply_automation`).
* While the protocol is provisional every decision is recorded as ``provisional``
  (a draft), automated decisions are refused, and nothing is final.
* Decisions are never edited or deleted. A reviewer who changes their mind records
  a new decision that supersedes their earlier one; the earlier one stays in the
  history. Different reviewers decide independently; a disagreement shows as
  outcome ``conflict`` until someone records an adjudication.
* ``criterion_not_met`` (the record states something that fails the cited
  criterion: an inclusion criterion is contradicted or an exclusion criterion
  applies) is distinct from ``insufficient_information``. At the title/abstract
  stage a paper cannot be excluded for insufficient information: an abstract that
  omits a detail is ``uncertain`` and goes on to full-text screening.
* Decisions are recorded on the canonical record of a duplicate group.

Term matching used for suggestions (``match_terms``, ``type_terms``): case-insensitive,
whole words or phrases; a trailing ``*`` matches any word ending (``reservoir*``
matches "reservoirs"). Terms are the researcher's own; the toolkit supplies none.

Supporting locations
--------------------
Title/abstract decisions cite the stored bibliographic record:
``{"kind": "record_field", "field": "title"|"abstract", "char_start", "char_end"}``
(0-based, half-open, in the paper's stored field text). Full-text decisions cite
document text through the source-location system of :mod:`geodesic.locations`.
A cited location is checked against the stored text when the decision is recorded.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from . import audit, locations, papers
from . import protocol as P
from .db import transaction
from .errors import (GroundingError, NotFoundError, ProtocolError, ProvisionalProtocolError, ReviewError,
                     ValidationError)
from .query import IN_SEARCH_RUN_SQL, SCREENING_STATUS_SQL
from .util import Actor, canonical_json, loads, new_id, utc_now

STAGES = P.SCREENING_STAGES
DECISIONS = ("include", "exclude", "uncertain")
REASON_KINDS = ("meets_criteria", "criterion_not_met", "insufficient_information", "other")
GENERATOR = "protocol-rules/1"
RULES_ACTOR = Actor("geodesic-screening-rules", "automated")
ASSESSMENT_VALUES = {
    "primary_relevance": ("direct", "supporting", "not_relevant", "unclear"),
    "quantitative_comparison": ("suitable", "limited", "not_suitable", "unclear"),
    "mathematical_modeling": ("suitable", "limited", "not_suitable", "unclear"),
}
SUPPORT_MAX_CHARS = 600


# ------------------------------------------------------------------ term matching
def term_pattern(term: str) -> re.Pattern[str]:
    """Regular expression for one researcher-supplied term (see module docstring)."""
    t = term.strip()
    prefix = t.endswith("*")
    words = [re.escape(w) for w in t.rstrip("*").split()]
    if not words:
        raise ValidationError("Empty match term.")
    body = r"\s+".join(words)
    return re.compile(rf"(?<!\w){body}" + (r"\w*" if prefix else r"(?!\w)"), re.IGNORECASE)


def find_term(terms: Iterable[str], texts: list[tuple[dict, str]]) -> tuple[str, dict, str] | None:
    """First occurrence of any term: ``(term, location, sentence_like_context)``."""
    for term in terms:
        if not term or not term.strip("* "):
            continue
        pat = term_pattern(term)
        for loc, text in texts:
            m = pat.search(text or "")
            if m:
                return term, {**loc, "char_start": m.start(), "char_end": m.end()}, text[m.start():m.end()]
    return None


def rule_of(criterion_id: str | None) -> str | None:
    """Automatable rule a pseudo-criterion belongs to (``None`` for researcher-defined criteria)."""
    if criterion_id in ("date_limit", "language"):
        return criterion_id
    if criterion_id and criterion_id.startswith("publication_type:"):
        return "publication_type"
    return None


def known_criterion_ids(protocol: P.Protocol, stage: str) -> set[str]:
    ids = {c.id for c in protocol.criteria() if stage in c.applies_to}
    ids |= {"date_limit", "language"} | {f"publication_type:{k}" for k in P.PUBLICATION_TYPE_KEYS}
    return ids


# ------------------------------------------------------------------ texts a stage may cite
def stage_texts(conn: sqlite3.Connection, paper: sqlite3.Row | dict, stage: str) -> list[tuple[dict, str]]:
    """``(location, text)`` units that suggestions for a stage may search."""
    out: list[tuple[dict, str]] = []
    for field in ("title", "abstract"):
        if paper[field]:
            out.append(({"kind": "record_field", "field": field}, paper[field]))
    if stage == "full_text":
        out = []
        for r in conn.execute(
                "SELECT tu.snapshot_id, tu.unit_kind, tu.unit_key, tu.text, d.format FROM document d"
                " JOIN document_snapshot ds ON ds.document_id = d.id AND ds.snapshot_no ="
                "   (SELECT MAX(snapshot_no) FROM document_snapshot WHERE document_id = d.id)"
                " JOIN text_unit tu ON tu.snapshot_id = ds.id"
                " WHERE d.paper_id IN (SELECT id FROM paper WHERE id = ? OR canonical_id = ?)"
                " AND d.record_status = 'active' AND d.content_scope = 'full_text' AND ds.status IN ('ok','partial')"
                " AND tu.extraction_status = 'ok' ORDER BY d.role, d.id, tu.seq", (paper["id"], paper["id"])):
            kind = "pdf_page" if r["unit_kind"] == "page" else ("html_element" if r["format"] == "html"
                                                                else "xml_element")
            out.append(({"kind": kind, "snapshot_id": r["snapshot_id"], "unit_key": r["unit_key"]}, r["text"]))
    return out


def describe_location(loc: dict | None) -> str:
    if not loc:
        return ""
    if loc.get("kind") == "record_field":
        return f"{loc['field']} chars {loc.get('char_start')}-{loc.get('char_end')}"
    return locations.describe(loc)


# ------------------------------------------------------------------ rule evaluation (pure)
@dataclass
class Draft:
    """A draft suggestion before it is stored."""

    decision: str
    reason_kind: str
    criterion_id: str | None
    rationale: str
    supporting_text: str | None = None
    location: dict | None = None


def evaluate(protocol: P.Protocol, paper: sqlite3.Row | dict, stage: str,
             texts: list[tuple[dict, str]]) -> list[Draft]:
    """Draft suggestions for one paper at one stage. Pure function of its arguments.

    Every rule-based exclusion is listed; otherwise exactly one include/uncertain
    draft is produced. Nothing is suggested from information the record lacks:
    a missing year, language, publication type or term never produces an exclusion.
    """
    drafts: list[Draft] = []
    flags: list[str] = []
    # publication types (original source terms; never normalised silently)
    ptype = paper["publication_type"] or ""
    for key in P.PUBLICATION_TYPE_KEYS:
        rule = protocol.publication_types[key]
        hit = find_term(rule.type_terms, [({}, ptype)]) if ptype else None
        if hit is None and key == "preprints" and paper["publication_status"] == "preprint":
            hit = ("publication status 'preprint'", {}, "preprint")
        if hit is None:
            continue
        said = f"the source gives the publication type as '{ptype}'" if ptype else \
            "the source marks the record as a preprint"
        if rule.treatment == "exclude":
            drafts.append(Draft("exclude", "criterion_not_met", f"publication_type:{key}",
                                f"{said}; the protocol excludes {key.replace('_', ' ')} (matched: {hit[0]})",
                                ptype or "preprint"))
        elif rule.treatment == "flag_for_review":
            flags.append(f"{said}; the protocol flags {key.replace('_', ' ')} for review")
    # date limits
    lim = protocol.date_limits
    year = paper["year"]
    if year is not None and ((lim.from_year is not None and year < lim.from_year)
                             or (lim.to_year is not None and year > lim.to_year)):
        span = f"{lim.from_year if lim.from_year is not None else '…'}–{lim.to_year if lim.to_year is not None else '…'}"
        drafts.append(Draft("exclude", "criterion_not_met", "date_limit",
                            f"publication year {year} is outside the protocol's date limits ({span})", str(year)))
    # language
    lang = (paper["language"] or "").strip()
    if protocol.languages and lang and lang.casefold() not in {x.casefold() for x in protocol.languages}:
        drafts.append(Draft("exclude", "criterion_not_met", "language",
                            f"the source gives the language as '{lang}'; the protocol is limited to "
                            f"{', '.join(protocol.languages)}", lang))
    # researcher-defined exclusion criteria
    for c in protocol.exclusion_criteria:
        if stage in c.applies_to and c.match_terms:
            hit = find_term(c.match_terms, texts)
            if hit:
                drafts.append(Draft("exclude", "criterion_not_met", c.id,
                                    f"exclusion criterion {c.id} ({c.text}) — its term '{hit[0]}' occurs in "
                                    f"{describe_location(hit[1])}", hit[2], hit[1]))
    if drafts:
        return drafts
    # inclusion criteria
    matched, missing, unevaluated = [], [], []
    first_hit: tuple[str, dict, str] | None = None
    for c in protocol.inclusion_criteria:
        if stage not in c.applies_to:
            continue
        if not c.match_terms:
            unevaluated.append(c.id)
            continue
        hit = find_term(c.match_terms, texts)
        if hit:
            matched.append(f"{c.id} ('{hit[0]}' in {describe_location(hit[1])})")
            first_hit = first_hit or hit
        else:
            missing.append(c.id)
    what = "abstract" if stage == "title_abstract" else "stored full text"
    notes = ""
    if unevaluated:
        notes += f" Criteria without match terms were not evaluated: {', '.join(unevaluated)}."
    if flags:
        notes += " " + "; ".join(flags) + "."
    if not texts or (stage == "title_abstract" and not paper["abstract"]):
        return [Draft("uncertain", "insufficient_information", None,
                      f"no {what} is available for this record, so the criteria cannot be judged from it." + notes)]
    if missing:
        return [Draft("uncertain", "insufficient_information", missing[0],
                      f"no term for {', '.join(missing)} occurs in the title or {what}; absence of a term is not "
                      "grounds for exclusion."
                      + (f" Terms found for: {'; '.join(matched)}." if matched else "") + notes)]
    if flags:
        return [Draft("uncertain", "other", None, "; ".join(flags) + "."
                      + (f" Terms found for: {'; '.join(matched)}." if matched else ""))]
    if matched:
        return [Draft("include", "meets_criteria", None,
                      f"terms found for every inclusion criterion that has match terms: {'; '.join(matched)}; no "
                      "exclusion rule applies." + notes, first_hit[2] if first_hit else None,
                      first_hit[1] if first_hit else None)]
    return [Draft("uncertain", "other", None,
                  "the protocol defines no match terms for this stage, so no rule-based suggestion can be made."
                  + notes)]


# ------------------------------------------------------------------ helpers
def _stage(stage: str) -> str:
    if stage not in STAGES:
        raise ValidationError(f"stage must be one of {STAGES}, got {stage!r}")
    return stage


def _is_synthetic(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT data_class FROM project").fetchone()["data_class"] == "synthetic"


def _require_reviewer(conn: sqlite3.Connection, actor: Actor, what: str) -> None:
    if actor.kind not in ("human", "simulated"):
        raise ReviewError(f"{what} needs a named reviewer, not an automated component.")
    if actor.kind == "simulated" and not _is_synthetic(conn):
        raise ReviewError("Simulated review actions are only permitted in synthetic projects.")


def _paper(conn: sqlite3.Connection, paper_id: str) -> sqlite3.Row:
    """The canonical, active paper record for ``paper_id``."""
    row = conn.execute("SELECT * FROM paper WHERE id = ?", (papers.canonical_id(conn, paper_id),)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown paper {paper_id}")
    if row["record_status"] == "removed":
        raise ReviewError("This paper record has been removed and cannot be screened.")
    return row


def _outcome(conn: sqlite3.Connection, paper_id: str, stage: str) -> dict | None:
    row = conn.execute(f"SELECT * FROM ({SCREENING_STATUS_SQL}) WHERE paper_id = ? AND stage = ?",
                       (paper_id, stage)).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------ suggestions
def suggest(conn: sqlite3.Connection, stage: str, actor: Actor = RULES_ACTOR, *,
            paper_ids: list[str] | None = None, include_decided: bool = False) -> dict[str, Any]:
    """Generate draft suggestions from the current protocol. Never records a decision.

    Papers that already have a current decision at the stage are skipped unless
    ``include_decided``. Re-running is idempotent: an identical open suggestion is
    kept, a changed one supersedes the earlier draft.
    """
    _stage(stage)
    pv = P.current(conn)
    if paper_ids is None:
        sql = "SELECT * FROM paper p WHERE p.record_status != 'removed' AND p.canonical_id IS NULL"
        if stage == "full_text":
            sql += (f" AND EXISTS (SELECT 1 FROM ({SCREENING_STATUS_SQL}) ss WHERE ss.paper_id = p.id AND"
                    " ss.stage = 'title_abstract' AND ss.outcome IN ('include','uncertain'))")
        rows = conn.execute(sql + " ORDER BY p.pk").fetchall()
    else:
        rows = [_paper(conn, pid) for pid in dict.fromkeys(paper_ids)]
    stats: dict[str, Any] = {"stage": stage, "papers_considered": 0, "created": 0, "unchanged": 0, "superseded": 0,
                             "skipped_already_decided": 0, "skipped_no_text": 0,
                             "by_suggestion": {"include": 0, "exclude": 0, "uncertain": 0},
                             "protocol_version": pv.version, "protocol_status": pv.status, "generator": GENERATOR}
    now = utc_now()
    with transaction(conn):
        for paper in rows:
            if not include_decided and conn.execute(
                    "SELECT 1 FROM screening_decision WHERE paper_id = ? AND stage = ? AND is_current = 1",
                    (paper["id"], stage)).fetchone():
                stats["skipped_already_decided"] += 1
                continue
            texts = stage_texts(conn, paper, stage)
            if stage == "full_text" and not texts:
                stats["skipped_no_text"] += 1
                continue
            stats["papers_considered"] += 1
            drafts = evaluate(pv.protocol, paper, stage, texts)
            wanted = {(d.decision, d.reason_kind, d.criterion_id, d.rationale, d.supporting_text): d for d in drafts}
            for old in conn.execute(
                    "SELECT * FROM screening_suggestion WHERE paper_id = ? AND stage = ? AND status = 'open' AND"
                    " generator = ?", (paper["id"], stage, GENERATOR)).fetchall():
                key = (old["suggested_decision"], old["reason_kind"], old["criterion_id"], old["rationale"],
                       old["supporting_text"])
                if key in wanted and old["protocol_version_id"] == pv.id:
                    del wanted[key]
                    stats["unchanged"] += 1
                else:
                    conn.execute("UPDATE screening_suggestion SET status = 'superseded', resolved_at = ?,"
                                 " resolved_by = ? WHERE id = ?", (now, actor.name, old["id"]))
                    stats["superseded"] += 1
            for d in wanted.values():
                conn.execute(
                    "INSERT INTO screening_suggestion (id, paper_id, stage, suggested_decision, reason_kind,"
                    " criterion_id, rationale, supporting_text, generator, protocol_version_id,"
                    " protocol_status_at_suggestion, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (new_id("sug"), paper["id"], stage, d.decision, d.reason_kind, d.criterion_id, d.rationale,
                     d.supporting_text, GENERATOR, pv.id, pv.status, now))
                stats["created"] += 1
                stats["by_suggestion"][d.decision] += 1
        if stats["created"] or stats["superseded"]:
            audit.record(conn, actor, "screening.suggest", "project", None,
                         {k: stats[k] for k in ("stage", "created", "superseded", "unchanged", "by_suggestion",
                                                "protocol_version", "protocol_status", "generator")})
    return stats


def list_suggestions(conn: sqlite3.Connection, *, stage: str | None = None, status: str | None = "open",
                     paper_id: str | None = None, suggested: str | None = None, limit: int = 50,
                     offset: int = 0) -> dict[str, Any]:
    where, params = ["1=1"], []
    for col, val in (("s.stage", stage), ("s.status", status), ("s.suggested_decision", suggested)):
        if val is not None:
            where.append(f"{col} = ?")
            params.append(val)
    if paper_id is not None:
        where.append("s.paper_id = ?")
        params.append(papers.canonical_id(conn, paper_id))
    w = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM screening_suggestion s WHERE {w}", params).fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        "SELECT s.*, p.title, p.year, p.citation_text, pv.version AS protocol_version FROM screening_suggestion s"
        " JOIN paper p ON p.id = s.paper_id JOIN protocol_version pv ON pv.id = s.protocol_version_id"
        f" WHERE {w} ORDER BY p.pk, s.created_at, s.id LIMIT ? OFFSET ?", (*params, limit, offset))]
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


# ------------------------------------------------------------------ decisions
def _check_support(conn: sqlite3.Connection, paper: sqlite3.Row, stage: str, text: str | None,
                   loc: dict | None) -> tuple[str | None, dict | None]:
    """Validate a cited supporting text/location against stored text. Raises :class:`GroundingError`."""
    if text is not None:
        text = text.strip() or None
    if text and len(text) > SUPPORT_MAX_CHARS:
        raise ValidationError(f"Supporting text is limited to {SUPPORT_MAX_CHARS} characters; cite the key passage.")
    if not loc:
        return text, None
    loc = {k: v for k, v in loc.items() if v is not None}
    kind = loc.get("kind")
    if kind == "record_field":
        field = loc.get("field")
        if field not in ("title", "abstract"):
            raise GroundingError("A record_field location must name the field 'title' or 'abstract'.")
        ok, problem = locations.check_span(paper[field], loc.get("char_start"), loc.get("char_end"), text)
        if not ok:
            raise GroundingError(f"The supporting text is not at the cited location ({field}): {problem}.")
        s, e = loc.get("char_start"), loc.get("char_end")
        return (paper[field] if s is None else paper[field][s:e]), loc
    if kind not in locations.DOCUMENT_KINDS:
        raise GroundingError(f"Unsupported supporting-location kind {kind!r}.")
    if stage != "full_text":
        raise GroundingError("Title/abstract decisions cite the bibliographic record (kind 'record_field'); "
                             "document text is cited at the full-text stage.")
    owner = conn.execute("SELECT d.paper_id FROM document_snapshot ds JOIN document d ON d.id = ds.document_id"
                         " WHERE ds.id = ?", (loc.get("snapshot_id"),)).fetchone()
    if owner is None or papers.canonical_id(conn, owner["paper_id"]) != paper["id"]:
        raise GroundingError("The cited document text does not belong to a document of this paper.")
    ok, found, problem = locations.resolve(conn, {**loc, "excerpt": text})
    if not ok:
        raise GroundingError(f"The supporting text is not at the cited location: {problem}.")
    return found, loc


def decide(conn: sqlite3.Connection, paper_id: str, stage: str, decision: str, actor: Actor, *,
           reason_kind: str | None = None, criterion_id: str | None = None, reason_text: str | None = None,
           supporting_text: str | None = None, supporting_location: dict | None = None,
           suggestion_id: str | None = None, require_final: bool = False, _role: str = "independent",
           _automation: bool = False) -> dict[str, Any]:
    """Record a screening decision. Returns a small result dict.

    Under a provisional protocol the decision is stored with finality
    ``provisional``; with ``require_final`` it is refused instead.
    """
    _stage(stage)
    if decision not in DECISIONS:
        raise ValidationError(f"decision must be one of {DECISIONS}, got {decision!r}")
    pv = P.current(conn)
    if _automation:
        auto = pv.protocol.automation[stage]
        if not pv.is_confirmed:
            raise ProvisionalProtocolError("Automated decisions are not enabled while the protocol is provisional.")
        if not auto.authorized or rule_of(criterion_id) not in auto.allowed_rules or decision != "exclude":
            raise ProtocolError("The confirmed protocol does not authorise this automated decision.")
    else:
        _require_reviewer(conn, actor, "A screening decision")
    if require_final and not pv.is_confirmed:
        raise ProvisionalProtocolError(
            f"Eligibility decisions cannot be finalised while the protocol is provisional ({pv.label}). "
            "Confirm the protocol first, or record the decision as a provisional draft.")
    paper = _paper(conn, paper_id)
    pid = paper["id"]
    reason_kind = reason_kind or {"include": "meets_criteria", "uncertain": "insufficient_information"}.get(decision)
    if reason_kind not in REASON_KINDS:
        raise ValidationError("An exclusion needs a reason kind: 'criterion_not_met' (name the criterion) or 'other'"
                              " (give the reason)." if reason_kind is None else
                              f"reason_kind must be one of {REASON_KINDS}, got {reason_kind!r}")
    reason_text = (reason_text or "").strip() or None
    if decision == "include" and reason_kind in ("criterion_not_met", "insufficient_information"):
        raise ValidationError(f"An inclusion cannot have the reason '{reason_kind}'.")
    if decision == "exclude":
        if reason_kind == "meets_criteria":
            raise ValidationError("An exclusion cannot have the reason 'meets_criteria'.")
        if reason_kind == "insufficient_information" and stage == "title_abstract":
            raise ReviewError(
                "A paper is not excluded at the title/abstract stage because the abstract omits a detail. Record "
                "'uncertain' with reason 'insufficient_information' so the paper goes on to full-text screening.")
        if reason_kind == "criterion_not_met" and not criterion_id:
            raise ValidationError("An exclusion for 'criterion_not_met' must name the criterion it relies on.")
        if reason_kind in ("other", "insufficient_information") and not reason_text:
            raise ValidationError(f"An exclusion with reason '{reason_kind}' needs a written reason.")
    if criterion_id is not None and criterion_id not in known_criterion_ids(pv.protocol, stage):
        raise ValidationError(f"'{criterion_id}' is not a criterion of protocol version {pv.version} that applies "
                              f"to the {stage} stage.")
    if stage == "full_text":
        ta = _outcome(conn, pid, "title_abstract")
        if ta and ta["outcome"] in ("exclude", "conflict"):
            raise ReviewError(
                f"This paper's title/abstract outcome is '{ta['outcome']}'. "
                + ("Revise that decision first if the paper should go to full-text screening."
                   if ta["outcome"] == "exclude" else "Adjudicate the disagreement first."))
    supporting_text, loc = _check_support(conn, paper, stage, supporting_text, supporting_location)
    sug = None
    if suggestion_id:
        sug = conn.execute("SELECT * FROM screening_suggestion WHERE id = ?", (suggestion_id,)).fetchone()
        if sug is None:
            raise NotFoundError(f"Unknown suggestion {suggestion_id}")
        if sug["paper_id"] != pid or sug["stage"] != stage:
            raise ValidationError("The suggestion belongs to a different paper or stage.")
        if sug["status"] not in ("open", "accepted"):
            raise ReviewError(f"This suggestion is '{sug['status']}' and can no longer be accepted.")
        if sug["protocol_version_id"] != pv.id:
            raise ReviewError("This suggestion was generated under an earlier protocol version. Generate "
                              "suggestions again before accepting one.")
    now = utc_now()
    did = new_id("scd")
    kind = "automated" if _automation else actor.kind
    with transaction(conn):
        if _role == "adjudication":
            prior = conn.execute("SELECT id FROM screening_decision WHERE paper_id = ? AND stage = ? AND"
                                 " is_current = 1 AND decision_role = 'adjudication'", (pid, stage)).fetchall()
        else:
            prior = conn.execute(
                "SELECT id FROM screening_decision WHERE paper_id = ? AND stage = ? AND is_current = 1 AND"
                " decision_role = 'independent' AND reviewer = ? ORDER BY decided_at", (pid, stage, actor.name)
            ).fetchall()
        overridden: list[str] = []
        if not _automation and _role == "independent":
            overridden = [r["id"] for r in conn.execute(
                "SELECT id FROM screening_decision WHERE paper_id = ? AND stage = ? AND is_current = 1 AND"
                " decider_kind = 'automated'", (pid, stage))]
        supersedes = prior[-1]["id"] if prior else (overridden[-1] if overridden else None)
        for old in {*(r["id"] for r in prior), *overridden}:
            conn.execute("UPDATE screening_decision SET is_current = 0 WHERE id = ?", (old,))
        conn.execute(
            "INSERT INTO screening_decision (id, paper_id, stage, decision, reason_kind, criterion_id, reason_text,"
            " reviewer, decider_kind, decision_role, finality, decided_at, protocol_version_id,"
            " protocol_status_at_decision, supporting_text, supporting_location_json, suggestion_id, supersedes_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, pid, stage, decision, reason_kind, criterion_id, reason_text, actor.name, kind, _role,
             "final" if pv.is_confirmed else "provisional", now, pv.id, pv.status, supporting_text,
             canonical_json(loc) if loc else None, suggestion_id, supersedes))
        if sug is not None and sug["status"] == "open":
            conn.execute("UPDATE screening_suggestion SET status = 'accepted', resolved_at = ?, resolved_by = ?,"
                         " decision_id = ? WHERE id = ?", (now, actor.name, did, suggestion_id))
        audit.record(conn, actor, "screening.adjudicate" if _role == "adjudication" else "screening.decide",
                     "paper", pid,
                     {"decision_id": did, "stage": stage, "decision": decision, "reason_kind": reason_kind,
                      "criterion_id": criterion_id, "finality": "final" if pv.is_confirmed else "provisional",
                      "protocol_version": pv.version, "protocol_status": pv.status, "decider_kind": kind,
                      "supersedes": supersedes, "overrides_automated": overridden, "suggestion_id": suggestion_id})
    out = _outcome(conn, pid, stage) or {}
    return {"decision_id": did, "paper_id": pid, "stage": stage, "decision": decision, "reason_kind": reason_kind,
            "finality": "final" if pv.is_confirmed else "provisional", "protocol_version": pv.version,
            "supersedes": supersedes, "outcome": out.get("outcome")}


def _suggestion(conn: sqlite3.Connection, suggestion_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM screening_suggestion WHERE id = ?", (suggestion_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown suggestion {suggestion_id}")
    return row


def _draft_location(conn: sqlite3.Connection, sug: sqlite3.Row) -> dict | None:
    """Location of a stored suggestion's supporting text, re-derived by the same deterministic rules."""
    pv = P.get_version(conn, sug["protocol_version_id"])
    paper = conn.execute("SELECT * FROM paper WHERE id = ?", (sug["paper_id"],)).fetchone()
    for d in evaluate(pv.protocol, paper, sug["stage"], stage_texts(conn, paper, sug["stage"])):
        if (d.decision, d.criterion_id, d.rationale) == (sug["suggested_decision"], sug["criterion_id"],
                                                         sug["rationale"]):
            return d.location
    return None


def accept_suggestion(conn: sqlite3.Connection, suggestion_id: str, actor: Actor, *, note: str | None = None,
                      require_final: bool = False) -> dict[str, Any]:
    """A named reviewer turns a suggestion into their own decision."""
    _require_reviewer(conn, actor, "Accepting a suggestion")
    sug = _suggestion(conn, suggestion_id)
    loc = _draft_location(conn, sug)
    reason = f"accepted rule-based suggestion: {sug['rationale']}" + (f" | reviewer note: {note}" if note else "")
    return decide(conn, sug["paper_id"], sug["stage"], sug["suggested_decision"], actor,
                  reason_kind=sug["reason_kind"], criterion_id=sug["criterion_id"], reason_text=reason,
                  supporting_text=sug["supporting_text"] if loc else None, supporting_location=loc,
                  suggestion_id=suggestion_id, require_final=require_final)


def dismiss_suggestion(conn: sqlite3.Connection, suggestion_id: str, actor: Actor, note: str | None = None) -> None:
    """A named reviewer sets a suggestion aside. No decision is recorded."""
    _require_reviewer(conn, actor, "Dismissing a suggestion")
    sug = _suggestion(conn, suggestion_id)
    if sug["status"] != "open":
        raise ReviewError(f"This suggestion is already '{sug['status']}'.")
    with transaction(conn):
        conn.execute("UPDATE screening_suggestion SET status = 'dismissed', resolved_at = ?, resolved_by = ?"
                     " WHERE id = ?", (utc_now(), actor.name, suggestion_id))
        audit.record(conn, actor, "screening.suggestion_dismissed", "paper", sug["paper_id"],
                     {"suggestion_id": suggestion_id, "stage": sug["stage"], "note": note})


def adjudicate(conn: sqlite3.Connection, paper_id: str, stage: str, decision: str, actor: Actor,
               **kwargs: Any) -> dict[str, Any]:
    """Resolve a recorded disagreement between reviewers. The independent decisions stay in the history."""
    _require_reviewer(conn, actor, "An adjudication")
    pid = _paper(conn, paper_id)["id"]
    distinct = conn.execute(
        "SELECT COUNT(DISTINCT decision) FROM screening_decision WHERE paper_id = ? AND stage = ? AND"
        " is_current = 1 AND decision_role = 'independent'", (pid, _stage(stage))).fetchone()[0]
    if distinct < 2:
        raise ReviewError("There is no recorded disagreement to adjudicate for this paper and stage.")
    return decide(conn, pid, stage, decision, actor, _role="adjudication", **kwargs)


def reaffirm(conn: sqlite3.Connection, decision_id: str, actor: Actor, note: str | None = None) -> dict[str, Any]:
    """Re-review a flagged decision and keep it: recorded as a new decision under the current protocol."""
    _require_reviewer(conn, actor, "Re-reviewing a decision")
    old = conn.execute("SELECT * FROM screening_decision WHERE id = ?", (decision_id,)).fetchone()
    if old is None:
        raise NotFoundError(f"Unknown screening decision {decision_id}")
    if not old["is_current"]:
        raise ReviewError("This decision has already been superseded.")
    if old["decider_kind"] != "automated" and old["reviewer"] != actor.name:
        raise ReviewError(f"This decision was made by {old['reviewer']}. Record your own decision instead; "
                          "theirs stays flagged until they re-review it.")
    reason = "; ".join(x for x in (old["reason_text"], f"re-reviewed and kept: {note or 'no note'}") if x)
    return decide(conn, old["paper_id"], old["stage"], old["decision"], actor, reason_kind=old["reason_kind"],
                  criterion_id=old["criterion_id"], reason_text=reason, supporting_text=old["supporting_text"],
                  supporting_location=loads(old["supporting_location_json"]), _role=old["decision_role"])


def apply_automation(conn: sqlite3.Connection, stage: str, actor: Actor = RULES_ACTOR) -> dict[str, Any]:
    """Turn open rule-based exclusion suggestions into automated decisions, where the protocol allows it.

    Requires a confirmed protocol that authorises automation at the stage. Only
    the rules listed in ``automation.<stage>.allowed_rules`` are applied, and only
    to papers with no current decision. Automated decisions are stored with
    ``decider_kind='automated'`` and counted separately from human ones.
    """
    _stage(stage)
    pv = P.current(conn)
    if not pv.is_confirmed:
        raise ProvisionalProtocolError(
            f"Automated exclusions are not enabled while the protocol is provisional ({pv.label}).")
    auto = pv.protocol.automation[stage]
    if not auto.authorized or not auto.allowed_rules:
        raise ProtocolError(f"Protocol version {pv.version} does not authorise automated decisions at the {stage} "
                            "stage (automation.<stage>.authorized and allowed_rules).")
    stats: dict[str, Any] = {"stage": stage, "applied": 0, "by_rule": {}, "skipped_already_decided": 0,
                             "skipped_rule_not_authorised": 0, "allowed_rules": list(auto.allowed_rules),
                             "protocol_version": pv.version}
    rows = conn.execute(
        "SELECT * FROM screening_suggestion WHERE stage = ? AND status = 'open' AND generator = ? AND"
        " protocol_version_id = ? AND suggested_decision = 'exclude' AND reason_kind = 'criterion_not_met'"
        " ORDER BY created_at, id", (stage, GENERATOR, pv.id)).fetchall()
    with transaction(conn):
        for sug in rows:
            rule = rule_of(sug["criterion_id"])
            if rule not in auto.allowed_rules:
                stats["skipped_rule_not_authorised"] += 1
                continue
            if conn.execute("SELECT 1 FROM screening_decision WHERE paper_id = ? AND stage = ? AND is_current = 1",
                            (sug["paper_id"], stage)).fetchone():
                stats["skipped_already_decided"] += 1
                continue
            decide(conn, sug["paper_id"], stage, "exclude", actor, reason_kind="criterion_not_met",
                   criterion_id=sug["criterion_id"], reason_text=f"automated rule '{rule}': {sug['rationale']}",
                   suggestion_id=sug["id"], _automation=True)
            stats["applied"] += 1
            stats["by_rule"][rule] = stats["by_rule"].get(rule, 0) + 1
    return stats


# ------------------------------------------------------------------ views
def _decision_rows(conn: sqlite3.Connection, where: str, params: tuple) -> list[dict]:
    rows = []
    for r in conn.execute(
            "SELECT d.*, pv.version AS protocol_version FROM screening_decision d JOIN protocol_version pv"
            f" ON pv.id = d.protocol_version_id WHERE {where} ORDER BY d.decided_at, d.id", params):
        row = dict(r)
        row["supporting_location"] = loads(row.pop("supporting_location_json"))
        row["supporting_location_text"] = describe_location(row["supporting_location"])
        rows.append(row)
    return rows


def status(conn: sqlite3.Connection, paper_id: str) -> dict[str, dict]:
    """Per-stage screening status of a paper (``outcome`` is ``unscreened`` when no decision exists)."""
    pid = papers.canonical_id(conn, paper_id)
    out = {}
    for stage in STAGES:
        o = _outcome(conn, pid, stage) or {"paper_id": pid, "stage": stage, "outcome": "unscreened",
                                           "finality": None, "n_decisions": 0, "n_human": 0, "n_non_human": 0,
                                           "needs_rereview": 0}
        o["decisions"] = _decision_rows(conn, "d.paper_id = ? AND d.stage = ? AND d.is_current = 1", (pid, stage))
        out[stage] = o
    return out


def history(conn: sqlite3.Connection, paper_id: str, stage: str | None = None) -> dict[str, list[dict]]:
    """Every decision (including superseded ones) and every suggestion for a paper, oldest first.

    Decisions recorded on a duplicate record before it was grouped under this
    paper are listed too (``on_grouped_member``); they are history only and do not
    count towards the group's screening status.
    """
    pid = papers.canonical_id(conn, paper_id)
    group = "(SELECT id FROM paper WHERE id = ? OR canonical_id = ?)"
    params: list[Any] = [pid, pid]
    if stage:
        params.append(_stage(stage))
    sugg = [dict(r) for r in conn.execute(
        f"SELECT * FROM screening_suggestion WHERE paper_id IN {group}" + (" AND stage = ?" if stage else "")
        + " ORDER BY created_at, id", params)]
    decisions = _decision_rows(conn, f"d.paper_id IN {group}" + (" AND d.stage = ?" if stage else ""), tuple(params))
    for d in decisions:
        # decisions made on a record before it was grouped stay with that record; they do not count for the group
        d["on_grouped_member"] = d["paper_id"] != pid
    return {"decisions": decisions, "suggestions": sugg}


def queue(conn: sqlite3.Connection, stage: str, *, reviewer: str | None = None, search_run_id: str | None = None,
          limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Papers awaiting a decision at a stage.

    Title/abstract: canonical records without a current decision. Full text:
    records whose title/abstract outcome is include or uncertain and that have no
    current full-text decision. With ``reviewer``: records that reviewer has not
    decided yet (for independent second screening).
    """
    _stage(stage)
    where = ["p.record_status != 'removed'", "p.canonical_id IS NULL"]
    params: list[Any] = []
    if stage == "full_text":
        where.append(f"EXISTS (SELECT 1 FROM ({SCREENING_STATUS_SQL}) ss WHERE ss.paper_id = p.id AND"
                     " ss.stage = 'title_abstract' AND ss.outcome IN ('include','uncertain'))")
    mine = " AND d.reviewer = ?" if reviewer else ""
    where.append("NOT EXISTS (SELECT 1 FROM screening_decision d WHERE d.paper_id = p.id AND d.stage = ? AND"
                 f" d.is_current = 1{mine})")
    params.append(stage)
    if reviewer:
        params.append(reviewer)
    if search_run_id:
        where.append(IN_SEARCH_RUN_SQL)
        params += [search_run_id, search_run_id]
    w = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM paper p WHERE {w}", params).fetchone()[0]
    rows = []
    for r in conn.execute(
            "SELECT p.id, p.title, p.citation_text, p.authors_json, p.year, p.journal, p.publication_type,"
            " p.publication_status, p.language, p.abstract, p.doi, p.origin, p.fulltext_availability"
            f" FROM paper p WHERE {w} ORDER BY p.pk LIMIT ? OFFSET ?", (*params, limit, offset)):
        row = dict(r)
        row["authors"] = loads(row.pop("authors_json"), [])
        row["suggestions"] = [dict(s) for s in conn.execute(
            "SELECT id, suggested_decision, reason_kind, criterion_id, rationale, supporting_text, generator"
            " FROM screening_suggestion WHERE paper_id = ? AND stage = ? AND status = 'open' ORDER BY created_at, id",
            (r["id"], stage))]
        row["other_decisions"] = conn.execute(
            "SELECT COUNT(*) FROM screening_decision WHERE paper_id = ? AND stage = ? AND is_current = 1",
            (r["id"], stage)).fetchone()[0]
        rows.append(row)
    return {"rows": rows, "total": total, "limit": limit, "offset": offset, "stage": stage}


def conflicts(conn: sqlite3.Connection, stage: str | None = None) -> list[dict]:
    """Papers whose reviewers currently disagree and that have not been adjudicated."""
    out = []
    for r in conn.execute(
            f"SELECT ss.paper_id, ss.stage, p.title, p.citation_text, p.year FROM ({SCREENING_STATUS_SQL}) ss"
            " JOIN paper p ON p.id = ss.paper_id WHERE ss.outcome = 'conflict'"
            + (" AND ss.stage = ?" if stage else "") + " ORDER BY p.pk, ss.stage", (stage,) if stage else ()):
        row = dict(r)
        row["decisions"] = _decision_rows(conn, "d.paper_id = ? AND d.stage = ? AND d.is_current = 1",
                                          (r["paper_id"], r["stage"]))
        out.append(row)
    return out


def rereview_queue(conn: sqlite3.Connection, stage: str | None = None, reviewer: str | None = None) -> list[dict]:
    """Current decisions flagged for re-review after a protocol change or confirmation."""
    where, params = "d.is_current = 1 AND d.needs_rereview = 1", []
    for col, val in (("d.stage", stage), ("d.reviewer", reviewer)):
        if val:
            where += f" AND {col} = ?"
            params.append(val)
    rows = _decision_rows(conn, where, tuple(params))
    for row in rows:
        p = conn.execute("SELECT title, citation_text, year FROM paper WHERE id = ?", (row["paper_id"],)).fetchone()
        row.update(title=p["title"], citation_text=p["citation_text"], year=p["year"])
    return rows


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts for the search and screening flow summary. Human, automated and simulated decisions are separate."""
    pv = P.current(conn)

    def one(sql: str, *params: Any) -> int:
        return conn.execute(sql, params).fetchone()[0] or 0

    canonical = "p.record_status != 'removed' AND p.canonical_id IS NULL"
    ident = {
        "search_runs": {r["mode"]: r["n"] for r in conn.execute(
            "SELECT mode, COUNT(*) AS n FROM search_run WHERE status != 'failed' GROUP BY mode")},
        "records_retrieved": {r["mode"]: r["n"] for r in conn.execute(
            "SELECT r.mode, COUNT(*) AS n FROM search_result s JOIN search_run r ON r.id = s.search_run_id"
            " GROUP BY r.mode")},
        "runs_not_complete": one("SELECT COUNT(*) FROM search_run WHERE status IN ('truncated','incomplete')"),
        "paper_records": one("SELECT COUNT(*) FROM paper WHERE record_status != 'removed'"),
        "paper_records_by_origin": {r["origin"]: r["n"] for r in conn.execute(
            "SELECT origin, COUNT(*) AS n FROM paper WHERE record_status != 'removed' GROUP BY origin")},
        "grouped_as_duplicates": one("SELECT COUNT(*) FROM paper WHERE record_status != 'removed' AND"
                                     " canonical_id IS NOT NULL"),
        "distinct_records_after_grouping": one(f"SELECT COUNT(*) FROM paper p WHERE {canonical}"),
        "duplicate_candidates_awaiting_review": one(
            "SELECT COUNT(*) FROM paper_relation WHERE status = 'candidate' AND relation_type = 'duplicate'"),
    }
    stages: dict[str, Any] = {}
    for stage in STAGES:
        eligible_sql = f"SELECT p.id FROM paper p WHERE {canonical}"
        if stage == "full_text":
            eligible_sql += (f" AND EXISTS (SELECT 1 FROM ({SCREENING_STATUS_SQL}) t WHERE t.paper_id = p.id AND"
                             " t.stage = 'title_abstract' AND t.outcome IN ('include','uncertain'))")
        st = f"SELECT * FROM ({SCREENING_STATUS_SQL}) WHERE stage = '{stage}'"
        outcomes = {k: 0 for k in ("include", "exclude", "uncertain", "conflict")}
        outcomes.update({r["outcome"]: r["n"] for r in conn.execute(
            f"SELECT outcome, COUNT(*) AS n FROM ({st}) GROUP BY outcome")})
        finality = {"final": 0, "provisional": 0}
        finality.update({r["finality"]: r["n"] for r in conn.execute(
            f"SELECT finality, COUNT(*) AS n FROM ({st}) GROUP BY finality")})
        kinds = {"human": 0, "automated": 0, "simulated": 0}
        kinds.update({r["decider_kind"]: r["n"] for r in conn.execute(
            "SELECT decider_kind, COUNT(*) AS n FROM screening_decision WHERE stage = ? AND is_current = 1"
            " GROUP BY decider_kind", (stage,))})
        eligible = one(f"SELECT COUNT(*) FROM ({eligible_sql})")
        screened = one(f"SELECT COUNT(*) FROM ({st})")
        stages[stage] = {
            "eligible_for_stage": eligible,
            "screened": screened,
            "awaiting_decision": one(
                f"SELECT COUNT(*) FROM ({eligible_sql}) e WHERE NOT EXISTS (SELECT 1 FROM screening_decision d"
                " WHERE d.paper_id = e.id AND d.stage = ? AND d.is_current = 1)", stage),
            "outcomes": outcomes,
            "papers_by_finality": finality,
            "current_decisions_by_decider": kinds,
            "papers_decided_only_by_automation": one(
                f"SELECT COUNT(*) FROM ({st}) WHERE n_human = 0 AND n_non_human > 0 AND paper_id NOT IN (SELECT"
                " paper_id FROM screening_decision WHERE stage = ? AND is_current = 1 AND"
                " decider_kind != 'automated')", stage),
            "exclusion_reasons": [dict(r) for r in conn.execute(
                "SELECT d.reason_kind, d.criterion_id, d.decider_kind, COUNT(DISTINCT d.paper_id) AS papers"
                f" FROM screening_decision d JOIN ({st}) s ON s.paper_id = d.paper_id WHERE d.stage = ? AND"
                " d.is_current = 1 AND d.decision = 'exclude' AND s.outcome = 'exclude'"
                " GROUP BY d.reason_kind, d.criterion_id, d.decider_kind ORDER BY papers DESC, d.criterion_id",
                (stage,))],
            "superseded_decisions": one("SELECT COUNT(*) FROM screening_decision WHERE stage = ? AND is_current = 0",
                                        stage),
            "flagged_for_rereview": one("SELECT COUNT(*) FROM screening_decision WHERE stage = ? AND is_current = 1"
                                        " AND needs_rereview = 1", stage),
            "open_suggestions": one("SELECT COUNT(*) FROM screening_suggestion WHERE stage = ? AND status = 'open'",
                                    stage),
        }
    notes = ["Counts describe the records in this project only; no search is exhaustive.",
             "A paper counted under two exclusion reasons was excluded by decisions citing different reasons."]
    if not pv.is_confirmed:
        notes.insert(0, f"The protocol is provisional ({pv.label}): every decision is a provisional draft and "
                        "no eligibility decision is final.")
    return {"protocol": {"version": pv.version, "status": pv.status, "label": pv.label},
            "identification": ident, "stages": stages, "notes": notes}


# ------------------------------------------------------------------ other paper-level assessments
def assess(conn: sqlite3.Connection, paper_id: str, dimension: str, value: str, actor: Actor,
           rationale: str | None = None) -> str:
    """Record a reviewer's assessment on one dimension that is kept separate from eligibility.

    Dimensions: relevance to the primary question, suitability for quantitative
    comparison, suitability for mathematical modelling. Earlier assessments by the
    same reviewer stay in the history.
    """
    _require_reviewer(conn, actor, "An assessment")
    if dimension not in ASSESSMENT_VALUES:
        raise ValidationError(f"dimension must be one of {tuple(ASSESSMENT_VALUES)}, got {dimension!r}")
    if value not in ASSESSMENT_VALUES[dimension]:
        raise ValidationError(f"value for {dimension} must be one of {ASSESSMENT_VALUES[dimension]}, got {value!r}")
    pid = _paper(conn, paper_id)["id"]
    pv = P.current(conn)
    aid = new_id("pas")
    with transaction(conn):
        conn.execute("UPDATE paper_assessment SET is_current = 0 WHERE paper_id = ? AND dimension = ? AND"
                     " reviewer = ? AND is_current = 1", (pid, dimension, actor.name))
        conn.execute("INSERT INTO paper_assessment (id, paper_id, dimension, value, rationale, reviewer,"
                     " decider_kind, protocol_version_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (aid, pid, dimension, value, rationale, actor.name, actor.kind, pv.id, utc_now()))
        audit.record(conn, actor, "paper.assess", "paper", pid,
                     {"assessment_id": aid, "dimension": dimension, "value": value,
                      "protocol_version": pv.version})
    return aid


def assessments(conn: sqlite3.Connection, paper_id: str, current_only: bool = True) -> list[dict]:
    pid = papers.canonical_id(conn, paper_id)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM paper_assessment WHERE paper_id = ?" + (" AND is_current = 1" if current_only else "")
        + " ORDER BY dimension, created_at", (pid,))]
