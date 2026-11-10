"""Eligibility screening: suggestions vs decisions, provisional protocol rules, history, reviewers, counts."""

from __future__ import annotations

import json

import pytest

from conftest import OPERATOR, REVIEWER_A, REVIEWER_B, paper_id
from geodesic import audit, dedup
from geodesic import protocol as P
from geodesic import screening as S
from geodesic.errors import (GroundingError, ProtocolError, ProvisionalProtocolError, ReviewError,
                             ValidationError)
from geodesic.synthetic.protocol import example_protocol
from geodesic.util import Actor

TA, FT = "title_abstract", "full_text"
T = {
    "release": "Tracer release from a stirred source tank",          # suggestion: include
    "conduit": "Dye-pulse dispersion in a baffled conduit.",         # suggestion: include
    "review": "A review of inert tracer methods for engineered reservoirs",   # exclude: publication type
    "old": "Tracer washout from a stirred tank",                     # exclude: date limit (2012)
    "reactive": "Reactive tracer decay in a weir pool",              # exclude: E2
    "pond": "Thermal stratification of a storage pond",              # uncertain: no term found
    "numerical": "Numerical study of conduit flow between tanks",    # exclude: E1
    "sediment": "Sediment sampling protocol",                        # uncertain: no abstract
}


def pid(project, key):
    return paper_id(project, T[key])


def _count(conn, table, where="1=1", *params):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]


def _suggestion(conn, paper, stage=TA, status="open"):
    rows = S.list_suggestions(conn, stage=stage, status=status, paper_id=paper)["rows"]
    assert len(rows) == 1, rows
    return rows[0]


@pytest.fixture
def provisional(searched):
    """Search fixture imported; synthetic example protocol saved but NOT confirmed."""
    P.save_new_version(searched.conn, example_protocol(), OPERATOR, "synthetic example protocol")
    assert not P.current(searched.conn).is_confirmed
    return searched


@pytest.fixture
def confirmed(provisional):
    P.confirm(provisional.conn, REVIEWER_A, provisional.data_class)
    return provisional


@pytest.fixture
def automated(searched):
    """Confirmed protocol that authorises automation of two rules at the title/abstract stage."""
    P.save_new_version(searched.conn, example_protocol(authorize_automation=True), OPERATOR, "with automation")
    P.confirm(searched.conn, REVIEWER_A, searched.data_class)
    return searched


# ------------------------------------------------------------------ suggestions are not decisions
def test_suggestions_are_stored_separately_and_are_not_decisions(provisional):
    conn = provisional.conn
    stats = S.suggest(conn, TA)
    assert stats["created"] == 10 and stats["by_suggestion"] == {"include": 3, "exclude": 4, "uncertain": 3}
    assert stats["protocol_status"] == "provisional" and stats["generator"] == S.GENERATOR
    assert _count(conn, "screening_suggestion") == 10 and _count(conn, "screening_decision") == 0
    summary = S.summary(conn)["stages"][TA]
    assert (summary["screened"], summary["awaiting_decision"], summary["open_suggestions"]) == (0, 10, 10)
    assert summary["outcomes"] == {"include": 0, "exclude": 0, "uncertain": 0, "conflict": 0}
    assert S.status(conn, pid(provisional, "review"))[TA]["outcome"] == "unscreened"
    assert S.queue(conn, TA)["total"] == 10
    # every suggestion records its generator, protocol version and that the protocol was provisional
    sug = _suggestion(conn, pid(provisional, "review"))
    assert (sug["suggested_decision"], sug["reason_kind"], sug["criterion_id"]) == \
        ("exclude", "criterion_not_met", "publication_type:reviews")
    assert sug["protocol_status_at_suggestion"] == "provisional" and sug["generator"] == S.GENERATOR
    assert sug["decision_id"] is None
    # running again changes nothing
    again = S.suggest(conn, TA)
    assert (again["created"], again["unchanged"], again["superseded"]) == (0, 10, 0)
    assert _count(conn, "screening_suggestion") == 10


def test_missing_detail_is_uncertain_never_a_suggested_exclusion(provisional):
    conn = provisional.conn
    S.suggest(conn, TA)
    no_terms = _suggestion(conn, pid(provisional, "pond"))
    assert (no_terms["suggested_decision"], no_terms["reason_kind"]) == ("uncertain", "insufficient_information")
    assert "absence of a term is not grounds for e" in no_terms["rationale"]
    no_abstract = _suggestion(conn, pid(provisional, "sediment"))
    assert (no_abstract["suggested_decision"], no_abstract["reason_kind"]) == \
        ("uncertain", "insufficient_information")
    assert "no abstract is available" in no_abstract["rationale"]
    # exclusions are only ever suggested for something the record states
    for s in S.list_suggestions(conn, stage=TA)["rows"]:
        if s["suggested_decision"] == "exclude":
            assert s["reason_kind"] == "criterion_not_met" and s["criterion_id"] and s["supporting_text"]


def test_suggestions_from_an_empty_protocol_are_all_uncertain(searched):
    stats = S.suggest(searched.conn, TA)
    assert stats["by_suggestion"] == {"include": 0, "exclude": 0, "uncertain": 10}


def test_new_protocol_version_supersedes_open_suggestions(provisional):
    conn = provisional.conn
    S.suggest(conn, TA)
    old = _suggestion(conn, pid(provisional, "review"))
    changed = example_protocol()
    changed.publication_types["reviews"].treatment = "include"
    P.save_new_version(conn, changed, OPERATOR, "reviews are now included")
    with pytest.raises(ReviewError, match="earlier protocol version"):
        S.accept_suggestion(conn, old["id"], REVIEWER_A)
    stats = S.suggest(conn, TA)
    assert stats["superseded"] >= 1
    row = conn.execute("SELECT status FROM screening_suggestion WHERE id = ?", (old["id"],)).fetchone()
    assert row["status"] == "superseded"                       # kept as history, not deleted
    assert _suggestion(conn, pid(provisional, "review"))["suggested_decision"] != "exclude"


# ------------------------------------------------------------------ provisional protocol
def test_decisions_under_a_provisional_protocol_are_drafts_and_cannot_be_final(provisional):
    conn = provisional.conn
    paper = pid(provisional, "release")
    with pytest.raises(ProvisionalProtocolError, match="cannot be finalised while the protocol is provisional"):
        S.decide(conn, paper, TA, "include", REVIEWER_A, require_final=True)
    assert _count(conn, "screening_decision") == 0
    out = S.decide(conn, paper, TA, "include", REVIEWER_A)
    assert out["finality"] == "provisional" and out["protocol_version"] == P.current(conn).version
    row = dict(conn.execute("SELECT * FROM screening_decision WHERE id = ?", (out["decision_id"],)).fetchone())
    assert (row["finality"], row["protocol_status_at_decision"], row["protocol_version_id"]) == \
        ("provisional", "provisional", P.current(conn).id)
    stage = S.summary(conn)["stages"][TA]
    assert stage["papers_by_finality"] == {"final": 0, "provisional": 1}
    assert "provisional" in S.summary(conn)["notes"][0]
    S.suggest(conn, TA)
    sug = _suggestion(conn, pid(provisional, "review"))
    with pytest.raises(ProvisionalProtocolError):
        S.accept_suggestion(conn, sug["id"], REVIEWER_A, require_final=True)
    assert _suggestion(conn, pid(provisional, "review"))["status"] == "open"


def test_automated_exclusions_are_refused_under_a_provisional_protocol(searched):
    conn = searched.conn
    # even a protocol that asks for automation enables none while it is unconfirmed
    P.save_new_version(conn, example_protocol(authorize_automation=True), OPERATOR, "with automation")
    S.suggest(conn, TA)
    with pytest.raises(ProvisionalProtocolError, match="not enabled while the protocol is provisional"):
        S.apply_automation(conn, TA)
    with pytest.raises(ProvisionalProtocolError):
        S.decide(conn, pid(searched, "review"), TA, "exclude", S.RULES_ACTOR, reason_kind="criterion_not_met",
                 criterion_id="publication_type:reviews", _automation=True)
    assert _count(conn, "screening_decision") == 0


def test_confirmation_flags_provisional_decisions_instead_of_promoting_them(provisional):
    conn = provisional.conn
    first = S.decide(conn, pid(provisional, "release"), TA, "include", REVIEWER_A)
    P.confirm(conn, REVIEWER_A, provisional.data_class)
    row = dict(conn.execute("SELECT * FROM screening_decision WHERE id = ?", (first["decision_id"],)).fetchone())
    assert row["finality"] == "provisional" and row["needs_rereview"] == 1
    assert "recorded under a provisional protocol" in row["rereview_reason"]
    flagged = S.rereview_queue(conn)
    assert [d["id"] for d in flagged] == [first["decision_id"]]
    with pytest.raises(ReviewError, match="made by"):
        S.reaffirm(conn, first["decision_id"], REVIEWER_B)
    kept = S.reaffirm(conn, first["decision_id"], REVIEWER_A, note="criteria unchanged")
    assert kept["finality"] == "final" and kept["supersedes"] == first["decision_id"]
    assert S.rereview_queue(conn) == []
    hist = S.history(conn, pid(provisional, "release"), TA)["decisions"]
    assert [(d["finality"], d["is_current"]) for d in hist] == [("provisional", 0), ("final", 1)]
    assert "re-reviewed and kept: criteria unchanged" in hist[1]["reason_text"]
    with pytest.raises(ReviewError, match="superseded"):
        S.reaffirm(conn, first["decision_id"], REVIEWER_A)


def test_decisions_under_a_confirmed_protocol_are_final_and_record_its_version(confirmed):
    conn = confirmed.conn
    out = S.decide(conn, pid(confirmed, "release"), TA, "include", REVIEWER_A, require_final=True)
    pv = P.current(conn)
    assert out["finality"] == "final" and out["protocol_version"] == pv.version
    d = S.status(conn, pid(confirmed, "release"))[TA]["decisions"][0]
    assert (d["protocol_version"], d["protocol_status_at_decision"], d["reviewer"], d["decider_kind"]) == \
        (pv.version, "confirmed", REVIEWER_A.name, "human")
    assert d["decided_at"].endswith("+00:00")


def test_changed_criterion_flags_the_decisions_that_relied_on_it(confirmed):
    conn = confirmed.conn
    excl = S.decide(conn, pid(confirmed, "reactive"), TA, "exclude", REVIEWER_A, reason_kind="criterion_not_met",
                    criterion_id="E2")
    other = S.decide(conn, pid(confirmed, "numerical"), TA, "exclude", REVIEWER_A, reason_kind="criterion_not_met",
                     criterion_id="E1")
    changed = example_protocol()
    changed.exclusion_criteria[1].text = "Tracer is reactive or volatile rather than inert."
    pv = P.save_new_version(conn, changed, OPERATOR, "E2 widened")
    assert not pv.is_confirmed
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM screening_decision")}
    assert rows[excl["decision_id"]]["needs_rereview"] == 1
    assert "E2" in rows[excl["decision_id"]]["rereview_reason"]
    assert rows[other["decision_id"]]["needs_rereview"] == 0
    # the earlier decision is flagged, not replaced: it is still the current one with its original content
    assert rows[excl["decision_id"]]["is_current"] == 1 and rows[excl["decision_id"]]["decision"] == "exclude"
    assert S.summary(conn)["stages"][TA]["flagged_for_rereview"] == 1
    assert [d["id"] for d in S.rereview_queue(conn, stage=TA)] == [excl["decision_id"]]


# ------------------------------------------------------------------ accepting / dismissing suggestions
def test_accepting_a_suggestion_records_the_reviewers_own_decision(confirmed):
    conn = confirmed.conn
    S.suggest(conn, TA)
    paper = pid(confirmed, "reactive")
    sug = _suggestion(conn, paper)
    with pytest.raises(ReviewError, match="named reviewer"):
        S.accept_suggestion(conn, sug["id"], S.RULES_ACTOR)
    out = S.accept_suggestion(conn, sug["id"], REVIEWER_A, note="agreed")
    d = S.status(conn, paper)[TA]["decisions"][0]
    assert (d["decision"], d["reason_kind"], d["criterion_id"], d["reviewer"], d["decider_kind"]) == \
        ("exclude", "criterion_not_met", "E2", REVIEWER_A.name, "human")
    assert d["suggestion_id"] == sug["id"] and "reviewer note: agreed" in d["reason_text"]
    # the suggestion's supporting text is kept with a location that resolves against the stored record
    assert d["supporting_text"] == "Reactive tracer"
    assert d["supporting_location"] == {"kind": "record_field", "field": "title", "char_start": 0, "char_end": 15}
    assert "title" in d["supporting_location_text"]
    stored = dict(conn.execute("SELECT * FROM screening_suggestion WHERE id = ?", (sug["id"],)).fetchone())
    assert (stored["status"], stored["resolved_by"], stored["decision_id"]) == \
        ("accepted", REVIEWER_A.name, out["decision_id"])
    # the suggestion row itself is unchanged in content
    assert stored["rationale"] == sug["rationale"] and stored["generator"] == S.GENERATOR


def test_dismissing_a_suggestion_records_no_decision(confirmed):
    conn = confirmed.conn
    S.suggest(conn, TA)
    paper = pid(confirmed, "numerical")
    sug = _suggestion(conn, paper)
    S.dismiss_suggestion(conn, sug["id"], REVIEWER_A, note="abstract describes a rig as well")
    assert _count(conn, "screening_decision") == 0
    assert S.status(conn, paper)[TA]["outcome"] == "unscreened"
    assert _suggestion(conn, paper, status="dismissed")["resolved_by"] == REVIEWER_A.name
    with pytest.raises(ReviewError, match="already 'dismissed'"):
        S.dismiss_suggestion(conn, sug["id"], REVIEWER_A)
    with pytest.raises(ReviewError, match="can no longer be accepted"):
        S.accept_suggestion(conn, sug["id"], REVIEWER_A)
    actions = [e["action"] for e in audit.history(conn, entity_type="paper", entity_id=paper)]
    assert "screening.suggestion_dismissed" in actions and "screening.decide" not in actions


# ------------------------------------------------------------------ automation
def test_automation_needs_explicit_authorisation_in_the_confirmed_protocol(confirmed):
    conn = confirmed.conn
    S.suggest(conn, TA)
    with pytest.raises(ProtocolError, match="does not authorise automated decisions"):
        S.apply_automation(conn, TA)
    assert _count(conn, "screening_decision") == 0


def test_authorised_automation_applies_only_the_allowed_rules_and_is_counted_separately(automated):
    conn = automated.conn
    S.suggest(conn, TA)
    human = S.decide(conn, pid(automated, "release"), TA, "include", REVIEWER_A)
    stats = S.apply_automation(conn, TA)
    assert stats["applied"] == 2 and stats["by_rule"] == {"publication_type": 1, "date_limit": 1}
    assert stats["skipped_rule_not_authorised"] == 2          # the E1 and E2 term matches stay suggestions
    assert stats["allowed_rules"] == ["publication_type", "date_limit"]
    for key, rule in (("review", "publication_type:reviews"), ("old", "date_limit")):
        st = S.status(conn, pid(automated, key))[TA]
        d = st["decisions"][0]
        assert (st["outcome"], d["decider_kind"], d["reviewer"], d["criterion_id"], d["finality"]) == \
            ("exclude", "automated", S.RULES_ACTOR.name, rule, "final")
        assert d["reason_text"].startswith("automated rule")
    for key in ("reactive", "numerical"):
        assert S.status(conn, pid(automated, key))[TA]["outcome"] == "unscreened"
        assert _suggestion(conn, pid(automated, key))["status"] == "open"
    stage = S.summary(conn)["stages"][TA]
    assert stage["current_decisions_by_decider"] == {"human": 1, "automated": 2, "simulated": 0}
    assert stage["papers_decided_only_by_automation"] == 2
    assert {(r["criterion_id"], r["decider_kind"], r["papers"]) for r in stage["exclusion_reasons"]} == \
        {("publication_type:reviews", "automated", 1), ("date_limit", "automated", 1)}
    assert human["finality"] == "final"
    # a second pass applies nothing more
    assert S.apply_automation(conn, TA)["applied"] == 0
    # only exclusions by an allowed rule can ever be automated
    with pytest.raises(ProtocolError):
        S.decide(conn, pid(automated, "reactive"), TA, "exclude", S.RULES_ACTOR, reason_kind="criterion_not_met",
                 criterion_id="E2", _automation=True)
    with pytest.raises(ProtocolError):
        S.decide(conn, pid(automated, "conduit"), TA, "include", S.RULES_ACTOR, _automation=True)
    # an automated component cannot record a decision through the reviewer path
    with pytest.raises(ReviewError, match="named reviewer"):
        S.decide(conn, pid(automated, "conduit"), TA, "include", S.RULES_ACTOR)


def test_a_reviewer_overrides_an_automated_decision_without_erasing_it(automated):
    conn = automated.conn
    S.suggest(conn, TA)
    S.apply_automation(conn, TA)
    paper = pid(automated, "review")
    auto = S.status(conn, paper)[TA]["decisions"][0]
    out = S.decide(conn, paper, TA, "include", REVIEWER_A)
    assert out["supersedes"] == auto["id"] and out["outcome"] == "include"
    hist = S.history(conn, paper, TA)["decisions"]
    assert [(d["decider_kind"], d["decision"], d["is_current"]) for d in hist] == \
        [("automated", "exclude", 0), ("human", "include", 1)]
    stage = S.summary(conn)["stages"][TA]
    assert stage["current_decisions_by_decider"] == {"human": 1, "automated": 1, "simulated": 0}
    assert stage["papers_decided_only_by_automation"] == 1 and stage["superseded_decisions"] == 1
    event = audit.history(conn, entity_type="paper", entity_id=paper)[0]
    assert json.loads(event["details_json"])["overrides_automated"] == [auto["id"]]
    # automation does not decide a paper that a reviewer has decided
    S.suggest(conn, TA, include_decided=True)
    assert S.apply_automation(conn, TA)["applied"] == 0
    assert S.status(conn, paper)[TA]["outcome"] == "include"


# ------------------------------------------------------------------ reasons
def test_criterion_not_met_is_distinct_from_insufficient_information(confirmed):
    conn = confirmed.conn
    paper = pid(confirmed, "pond")
    with pytest.raises(ReviewError, match="not excluded at the title/abstract stage because the abstract omits"):
        S.decide(conn, paper, TA, "exclude", REVIEWER_A, reason_kind="insufficient_information",
                 reason_text="abstract does not say which tracer")
    with pytest.raises(ValidationError, match="needs a reason kind"):
        S.decide(conn, paper, TA, "exclude", REVIEWER_A)
    with pytest.raises(ValidationError, match="must name the criterion"):
        S.decide(conn, paper, TA, "exclude", REVIEWER_A, reason_kind="criterion_not_met")
    with pytest.raises(ValidationError, match="is not a criterion of protocol version"):
        S.decide(conn, paper, TA, "exclude", REVIEWER_A, reason_kind="criterion_not_met", criterion_id="E9")
    with pytest.raises(ValidationError, match="applies to the title_abstract stage"):
        S.decide(conn, paper, TA, "exclude", REVIEWER_A, reason_kind="criterion_not_met", criterion_id="I3")
    with pytest.raises(ValidationError, match="needs a written reason"):
        S.decide(conn, paper, TA, "exclude", REVIEWER_A, reason_kind="other")
    with pytest.raises(ValidationError, match="inclusion cannot have the reason"):
        S.decide(conn, paper, TA, "include", REVIEWER_A, reason_kind="criterion_not_met", criterion_id="I1")
    with pytest.raises(ValidationError):
        S.decide(conn, paper, TA, "maybe", REVIEWER_A)
    with pytest.raises(ValidationError):
        S.decide(conn, paper, "abstract", "include", REVIEWER_A)
    assert _count(conn, "screening_decision") == 0
    out = S.decide(conn, paper, TA, "uncertain", REVIEWER_A)
    assert out["reason_kind"] == "insufficient_information"
    # the uncertain paper goes on to the full-text stage; there, lack of information can be an exclusion reason
    assert paper in [r["id"] for r in S.queue(conn, FT)["rows"]]
    ft = S.decide(conn, paper, FT, "exclude", REVIEWER_A, reason_kind="insufficient_information",
                  reason_text="full text reports no tracer measurement")
    assert ft["outcome"] == "exclude"
    reasons = S.summary(conn)["stages"][FT]["exclusion_reasons"]
    assert [(r["reason_kind"], r["criterion_id"], r["papers"]) for r in reasons] == \
        [("insufficient_information", None, 1)]


# ------------------------------------------------------------------ history
def test_a_revised_decision_supersedes_the_earlier_one_which_stays_in_the_history(confirmed):
    conn = confirmed.conn
    paper = pid(confirmed, "conduit")
    first = S.decide(conn, paper, TA, "uncertain", REVIEWER_A)
    second = S.decide(conn, paper, TA, "include", REVIEWER_A, reason_text="abstract names the rig")
    assert second["supersedes"] == first["decision_id"]
    hist = S.history(conn, paper, TA)["decisions"]
    assert [(d["id"], d["decision"], d["is_current"]) for d in hist] == \
        [(first["decision_id"], "uncertain", 0), (second["decision_id"], "include", 1)]
    assert hist[1]["supersedes_id"] == first["decision_id"]
    st = S.status(conn, paper)[TA]
    assert (st["outcome"], st["n_decisions"]) == ("include", 1)
    # decisions cannot be edited or deleted behind the history's back
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE screening_decision SET decision = 'exclude' WHERE id = ?", (first["decision_id"],))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM screening_decision WHERE id = ?", (first["decision_id"],))
    assert S.suggest(conn, TA, paper_ids=[paper], include_decided=True)["created"] == 1
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM screening_suggestion")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE screening_suggestion SET suggested_decision = 'exclude'")
    events = [json.loads(e["details_json"]) for e in audit.history(conn, entity_type="paper", entity_id=paper)
              if e["action"] == "screening.decide"]
    assert [e["decision"] for e in events] == ["include", "uncertain"]
    assert events[0]["supersedes"] == first["decision_id"]
    assert audit.verify_chain(conn)[0] is True


# ------------------------------------------------------------------ several reviewers
def test_reviewers_decide_independently_and_disagreements_are_recorded(confirmed):
    conn = confirmed.conn
    paper = pid(confirmed, "pond")
    S.decide(conn, paper, TA, "include", REVIEWER_A)
    # reviewer B's queue still holds the paper; the shared queue does not
    assert paper in [r["id"] for r in S.queue(conn, TA, reviewer=REVIEWER_B.name)["rows"]]
    assert paper not in [r["id"] for r in S.queue(conn, TA)["rows"]]
    with pytest.raises(ReviewError, match="no recorded disagreement"):
        S.adjudicate(conn, paper, TA, "include", REVIEWER_B)
    out = S.decide(conn, paper, TA, "uncertain", REVIEWER_B)
    assert out["outcome"] == "conflict" and out["supersedes"] is None
    st = S.status(conn, paper)[TA]
    assert st["outcome"] == "conflict" and {d["reviewer"] for d in st["decisions"]} == \
        {REVIEWER_A.name, REVIEWER_B.name}
    listed = S.conflicts(conn, TA)
    assert [c["paper_id"] for c in listed] == [paper] and len(listed[0]["decisions"]) == 2
    assert S.summary(conn)["stages"][TA]["outcomes"]["conflict"] == 1
    # a conflict blocks the full-text stage until it is adjudicated
    with pytest.raises(ReviewError, match="Adjudicate the disagreement first"):
        S.decide(conn, paper, FT, "include", REVIEWER_A)
    adj = S.adjudicate(conn, paper, TA, "include", REVIEWER_A, reason_text="discussed; rig is described")
    assert adj["outcome"] == "include"
    hist = S.history(conn, paper, TA)["decisions"]
    assert [(d["reviewer"], d["decision_role"], d["decision"], d["is_current"]) for d in hist] == [
        (REVIEWER_A.name, "independent", "include", 1), (REVIEWER_B.name, "independent", "uncertain", 1),
        (REVIEWER_A.name, "adjudication", "include", 1)]
    assert S.conflicts(conn) == []
    actions = [e["action"] for e in audit.history(conn, entity_type="paper", entity_id=paper)]
    assert actions.count("screening.adjudicate") == 1 and actions.count("screening.decide") == 2


def test_simulated_reviewers_are_labelled_and_only_allowed_in_synthetic_projects(confirmed, real_project):
    from geodesic import papers

    sim = Actor("Simulated reviewer", "simulated")
    conn = confirmed.conn
    S.decide(conn, pid(confirmed, "release"), TA, "include", sim)
    assert S.status(conn, pid(confirmed, "release"))[TA]["decisions"][0]["decider_kind"] == "simulated"
    assert S.summary(conn)["stages"][TA]["current_decisions_by_decider"] == \
        {"human": 0, "automated": 0, "simulated": 1}
    generated = papers.create_paper(real_project.conn, origin="manual", resolution_status="resolved",
                                    title="Generated test record")
    with pytest.raises(ReviewError, match="only permitted in synthetic projects"):
        S.decide(real_project.conn, generated, TA, "include", sim)


# ------------------------------------------------------------------ supporting text and location
def test_supporting_text_must_be_at_the_cited_location(confirmed):
    conn = confirmed.conn
    paper = pid(confirmed, "reactive")
    abstract = conn.execute("SELECT abstract FROM paper WHERE id = ?", (paper,)).fetchone()[0]
    phrase = "reactive tracer"
    start = abstract.index(phrase)
    good = {"kind": "record_field", "field": "abstract", "char_start": start, "char_end": start + len(phrase)}
    with pytest.raises(GroundingError, match="not at the cited location"):
        S.decide(conn, paper, TA, "exclude", REVIEWER_A, reason_kind="criterion_not_met", criterion_id="E2",
                 supporting_text="inert tracer", supporting_location=good)
    with pytest.raises(GroundingError, match="not at the cited location"):
        S.decide(conn, paper, TA, "exclude", REVIEWER_A, reason_kind="criterion_not_met", criterion_id="E2",
                 supporting_text=phrase, supporting_location={**good, "char_start": start + 3})
    with pytest.raises(GroundingError, match="must name the field"):
        S.decide(conn, paper, TA, "exclude", REVIEWER_A, reason_kind="criterion_not_met", criterion_id="E2",
                 supporting_text=phrase, supporting_location={**good, "field": "journal"})
    with pytest.raises(GroundingError, match="Unsupported supporting-location kind"):
        S.decide(conn, paper, TA, "exclude", REVIEWER_A, reason_kind="criterion_not_met", criterion_id="E2",
                 supporting_text=phrase, supporting_location={"kind": "memory"})
    with pytest.raises(GroundingError, match="cite the bibliographic record"):
        S.decide(conn, paper, TA, "exclude", REVIEWER_A, reason_kind="criterion_not_met", criterion_id="E2",
                 supporting_text=phrase, supporting_location={"kind": "pdf_page", "snapshot_id": "x", "page": 1})
    with pytest.raises(ValidationError, match="limited to"):
        S.decide(conn, paper, TA, "exclude", REVIEWER_A, reason_kind="criterion_not_met", criterion_id="E2",
                 supporting_text="x" * (S.SUPPORT_MAX_CHARS + 1))
    assert _count(conn, "screening_decision") == 0
    S.decide(conn, paper, TA, "exclude", REVIEWER_A, reason_kind="criterion_not_met", criterion_id="E2",
             supporting_text=phrase, supporting_location=good)
    d = S.status(conn, paper)[TA]["decisions"][0]
    assert d["supporting_text"] == phrase and d["supporting_location"] == good
    assert abstract[d["supporting_location"]["char_start"]:d["supporting_location"]["char_end"]] == phrase
    # a location given without text stores the text found there
    other = pid(confirmed, "numerical")
    S.decide(conn, other, TA, "exclude", REVIEWER_A, reason_kind="criterion_not_met", criterion_id="E1",
             supporting_location={"kind": "record_field", "field": "title", "char_start": 0, "char_end": 15})
    assert S.status(conn, other)[TA]["decisions"][0]["supporting_text"] == "Numerical study"


# ------------------------------------------------------------------ stages
def test_title_abstract_and_full_text_are_separate_stages(confirmed):
    conn = confirmed.conn
    incl, excl, untouched = pid(confirmed, "release"), pid(confirmed, "reactive"), pid(confirmed, "sediment")
    S.decide(conn, incl, TA, "include", REVIEWER_A)
    S.decide(conn, excl, TA, "exclude", REVIEWER_A, reason_kind="criterion_not_met", criterion_id="E2")
    assert [r["id"] for r in S.queue(conn, FT)["rows"]] == [incl]
    with pytest.raises(ReviewError, match="title/abstract outcome is 'exclude'"):
        S.decide(conn, excl, FT, "include", REVIEWER_A)
    st = S.status(conn, incl)
    assert st[TA]["outcome"] == "include" and st[FT]["outcome"] == "unscreened"
    # I3 applies to the full-text stage only
    out = S.decide(conn, incl, FT, "exclude", REVIEWER_B, reason_kind="criterion_not_met", criterion_id="I3")
    st = S.status(conn, incl)
    assert (st[TA]["outcome"], st[FT]["outcome"]) == ("include", "exclude") and out["stage"] == FT
    summary = S.summary(conn)["stages"]
    assert (summary[TA]["screened"], summary[TA]["awaiting_decision"]) == (2, 8)
    assert (summary[FT]["eligible_for_stage"], summary[FT]["screened"], summary[FT]["awaiting_decision"]) == (1, 1, 0)
    assert S.status(conn, untouched)[FT]["outcome"] == "unscreened"
    assert S.queue(conn, TA, search_run_id=confirmed.search_out["run"]["id"])["total"] == 8
    assert S.queue(conn, TA, search_run_id="srn_unknown")["total"] == 0


def test_decisions_follow_the_canonical_record_of_a_duplicate_group(confirmed):
    conn = confirmed.conn
    s2 = paper_id(confirmed, "Dye pulse dispersion in a baffled conduit", year=2022)
    s4 = pid(confirmed, "conduit")
    early = S.decide(conn, s4, TA, "uncertain", REVIEWER_B)           # decided before the records were grouped
    rel = dedup.existing_relation(conn, s2, s4, "duplicate")[0]
    dedup.decide_relation(conn, rel["id"], REVIEWER_A, "confirmed")
    assert S.queue(conn, TA)["total"] == 9 and s4 not in [r["id"] for r in S.queue(conn, TA)["rows"]]
    # the group has no decision yet; the member's earlier decision is history only
    assert S.status(conn, s4)[TA]["outcome"] == "unscreened"
    hist = S.history(conn, s2, TA)["decisions"]
    assert [(d["id"], d["on_grouped_member"]) for d in hist] == [(early["decision_id"], True)]
    out = S.decide(conn, s4, TA, "include", REVIEWER_A)               # addressed to the member, recorded on the group
    assert out["paper_id"] == s2
    assert S.status(conn, s4)[TA]["outcome"] == S.status(conn, s2)[TA]["outcome"] == "include"
    assert S.summary(conn)["identification"]["grouped_as_duplicates"] == 1
    assert S.summary(conn)["identification"]["distinct_records_after_grouping"] == 9


# ------------------------------------------------------------------ other assessments
def test_assessments_are_kept_apart_from_eligibility(confirmed):
    conn = confirmed.conn
    paper = pid(confirmed, "release")
    with pytest.raises(ReviewError, match="named reviewer"):
        S.assess(conn, paper, "primary_relevance", "direct", S.RULES_ACTOR)
    with pytest.raises(ValidationError, match="dimension must be one of"):
        S.assess(conn, paper, "overall_confidence", "high", REVIEWER_A)
    with pytest.raises(ValidationError, match="value for mathematical_modeling"):
        S.assess(conn, paper, "mathematical_modeling", "direct", REVIEWER_A)
    S.assess(conn, paper, "primary_relevance", "direct", REVIEWER_A, rationale="reports release from the rig")
    S.assess(conn, paper, "quantitative_comparison", "limited", REVIEWER_A)
    S.assess(conn, paper, "mathematical_modeling", "not_suitable", REVIEWER_A)
    S.assess(conn, paper, "primary_relevance", "supporting", REVIEWER_A, rationale="on reflection")
    S.assess(conn, paper, "primary_relevance", "direct", REVIEWER_B)
    current = S.assessments(conn, paper)
    assert sorted((a["dimension"], a["reviewer"], a["value"]) for a in current) == sorted([
        ("mathematical_modeling", REVIEWER_A.name, "not_suitable"),
        ("primary_relevance", REVIEWER_A.name, "supporting"),
        ("primary_relevance", REVIEWER_B.name, "direct"),
        ("quantitative_comparison", REVIEWER_A.name, "limited")])
    assert len(S.assessments(conn, paper, current_only=False)) == 5       # the revised one stays in the history
    assert all(a["protocol_version_id"] == P.current(conn).id for a in current)
    # an assessment is not an eligibility decision, and no combined score exists
    assert S.status(conn, paper)[TA]["outcome"] == "unscreened" and _count(conn, "screening_decision") == 0
    columns = {r[1] for r in conn.execute("PRAGMA table_info(paper)")} | \
        {r[1] for r in conn.execute("PRAGMA table_info(paper_assessment)")}
    assert not {c for c in columns if "confidence" in c or "score" in c}
    assert set(S.ASSESSMENT_VALUES) == {"primary_relevance", "quantitative_comparison", "mathematical_modeling"}
