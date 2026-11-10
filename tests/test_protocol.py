"""Protocol: empty template, provisional status, versioning and confirmation rules."""

from __future__ import annotations

import sqlite3

import pytest

from geodesic import audit, protocol as P
from geodesic.errors import ProtocolError
from geodesic.synthetic.protocol import example_protocol
from geodesic.util import SYSTEM, Actor


def test_shipped_template_is_empty_and_invents_no_criteria(tmp_path):
    path = tmp_path / "protocol.yaml"
    path.write_text(P.template_text(), encoding="utf-8")
    proto = P.load_protocol_file(path)
    assert proto == P.Protocol()                           # the template carries no content of its own
    assert proto.research_question == "" and proto.criteria() == []
    assert all(rule.treatment == "undecided" for rule in proto.publication_types.values())
    assert not any(a.authorized for a in proto.automation.values())
    problems = proto.completeness_problems()
    assert "research_question is empty" in problems
    assert "no inclusion or exclusion criteria are defined" in problems
    assert sum("treatment is 'undecided'" in p for p in problems) == 4


def test_new_project_has_an_empty_provisional_protocol(project, reviewer_a):
    conn = project.conn
    pv = P.current(conn)
    assert pv.version == 1 and pv.status == "provisional" and not pv.is_confirmed
    assert pv.label == "v1 (PROVISIONAL)"
    assert pv.protocol == P.Protocol()
    with pytest.raises(ProtocolError, match="cannot be confirmed yet") as err:
        P.confirm(conn, reviewer_a, project.data_class)
    assert "research_question is empty" in str(err.value)
    assert P.current(conn).status == "provisional"


def test_versions_are_kept_and_confirmation_is_explicit(project, reviewer_a):
    conn = project.conn
    v2 = P.save_new_version(conn, example_protocol(), reviewer_a, "synthetic example criteria")
    assert v2.version == 2 and v2.status == "provisional"
    assert P.save_new_version(conn, example_protocol(), reviewer_a).id == v2.id        # identical content: no-op

    with pytest.raises(ProtocolError, match="named reviewer"):
        P.confirm(conn, SYSTEM, project.data_class)
    with pytest.raises(ProtocolError, match="named reviewer"):
        P.confirm(conn, Actor("rule-engine", "automated"), project.data_class)
    with pytest.raises(ProtocolError, match="Simulated reviewers cannot confirm"):
        P.confirm(conn, Actor("Simulated Reviewer", "simulated"), "real")

    confirmed = P.confirm(conn, reviewer_a, project.data_class)
    assert confirmed.is_confirmed and confirmed.confirmed_by == reviewer_a.name
    assert confirmed.confirmed_at.endswith("+00:00") and confirmed.label == "v2 (confirmed)"

    # a confirmed version is frozen: it cannot be un-confirmed or rewritten
    with pytest.raises(sqlite3.DatabaseError, match="cannot change status"):
        conn.execute("UPDATE protocol_version SET status = 'provisional' WHERE id = ?", (confirmed.id,))
    with pytest.raises(sqlite3.DatabaseError, match="immutable column"):
        conn.execute("UPDATE protocol_version SET content_json = '{}' WHERE id = ?", (confirmed.id,))

    # editing criteria creates a new provisional version; earlier versions stay readable
    edited = example_protocol()
    edited.exclusion_criteria[1].text = "Tracer is reactive or degrades during the run."
    v3 = P.save_new_version(conn, edited, reviewer_a, "tightened E2")
    assert v3.version == 3 and v3.status == "provisional"
    versions = P.list_versions(conn)
    assert [(v.version, v.status) for v in versions] == [(1, "provisional"), (2, "confirmed"), (3, "provisional")]
    assert versions[1].protocol.exclusion_criteria[1].text == "Tracer is reactive rather than inert."
    assert P.changed_parts(versions[1].protocol, versions[2].protocol) == ["criterion:E2"]
    assert P.get_version(conn, v2.id).content_sha256 == v2.content_sha256

    actions = [e["action"] for e in audit.history(conn, entity_type="protocol_version", limit=20)]
    assert actions.count("protocol.create_version") == 3 and actions.count("protocol.confirm") == 1


def test_protocol_file_round_trip_and_validation(tmp_path):
    path = tmp_path / "example.yaml"
    path.write_text(P.dump_protocol(example_protocol()), encoding="utf-8")
    assert P.load_protocol_file(path) == example_protocol()

    path.write_text("research_question: x\nunknown_section: 1\n", encoding="utf-8")
    with pytest.raises(ProtocolError, match="Invalid protocol file"):
        P.load_protocol_file(path)
    path.write_text("automation:\n  abstract_only: {authorized: true}\n", encoding="utf-8")
    with pytest.raises(ProtocolError, match="unknown screening stage"):
        P.load_protocol_file(path)
    with pytest.raises(ProtocolError, match="not found"):
        P.load_protocol_file(tmp_path / "missing.yaml")


def test_incomplete_protocols_name_what_is_missing():
    proto = example_protocol()
    proto.publication_types["preprints"].treatment = "undecided"
    proto.supporting_evidence_role = " "
    proto.inclusion_criteria.append(P.Criterion(id="I1", text=""))
    problems = proto.completeness_problems()
    assert "publication_types.preprints.treatment is 'undecided'" in problems
    assert "criterion ids are not unique" in problems
    assert "criterion I1 has no text" in problems
    assert any(p.startswith("supporting_evidence_role is empty") for p in problems)
