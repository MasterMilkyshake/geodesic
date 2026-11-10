"""Human-editable, versioned research protocol.

The protocol belongs to the researcher. The application ships an empty template
and never drafts criteria. Until a version is confirmed by a reviewer it is
*provisional*: imports, exploratory searches and draft screening suggestions
are permitted, but eligibility decisions cannot be finalised and automated
decisions are disabled.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import audit
from .db import transaction
from .errors import ProtocolError
from .util import Actor, canonical_json, new_id, sha256_text, utc_now

SCREENING_STAGES = ("title_abstract", "full_text")
PUBLICATION_TYPE_KEYS = ("preprints", "reviews", "conference_abstracts", "other")
AUTOMATABLE_RULES = ("publication_type", "date_limit", "language")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Criterion(_Model):
    """One eligibility criterion.

    ``match_terms`` is optional and only drives *suggestions*: the terms are the
    researcher's own, and their absence from an abstract is treated as
    insufficient information, never as grounds for exclusion.
    """

    id: str
    text: str
    applies_to: list[Literal["title_abstract", "full_text"]] = Field(
        default_factory=lambda: ["title_abstract", "full_text"])
    match_terms: list[str] = Field(default_factory=list)


class PublicationTypeRule(_Model):
    treatment: Literal["include", "exclude", "flag_for_review", "undecided"] = "undecided"
    note: str = ""
    # terms that identify this type in a record's source-supplied publication type (original terms)
    type_terms: list[str] = Field(default_factory=list)


class DateLimits(_Model):
    from_year: int | None = None
    to_year: int | None = None


class StageAutomation(_Model):
    """Explicit authorisation for automated decisions at one screening stage."""

    authorized: bool = False
    allowed_rules: list[Literal["publication_type", "date_limit", "language"]] = Field(default_factory=list)


class Protocol(_Model):
    """Research scope and eligibility criteria."""

    research_question: str = ""
    target_systems: list[str] = Field(default_factory=list)
    interventions: list[str] = Field(default_factory=list)       # interventions, materials or exposures
    comparators: list[str] = Field(default_factory=list)
    outcomes: list[str] = Field(default_factory=list)
    measurement_types: list[str] = Field(default_factory=list)
    study_designs: list[str] = Field(default_factory=list)
    date_limits: DateLimits = Field(default_factory=DateLimits)
    languages: list[str] = Field(default_factory=list)            # empty list = no language limit requested
    publication_types: dict[str, PublicationTypeRule] = Field(
        default_factory=lambda: {k: PublicationTypeRule() for k in PUBLICATION_TYPE_KEYS})
    inclusion_criteria: list[Criterion] = Field(default_factory=list)
    exclusion_criteria: list[Criterion] = Field(default_factory=list)
    supporting_evidence_role: str = ""    # role of supporting evidence from systems outside the primary scope
    automation: dict[str, StageAutomation] = Field(
        default_factory=lambda: {s: StageAutomation() for s in SCREENING_STAGES})
    notes: str = ""

    @field_validator("publication_types")
    @classmethod
    def _pub_keys(cls, v: dict[str, PublicationTypeRule]) -> dict[str, PublicationTypeRule]:
        for key in PUBLICATION_TYPE_KEYS:
            v.setdefault(key, PublicationTypeRule())
        return v

    @field_validator("automation")
    @classmethod
    def _auto_keys(cls, v: dict[str, StageAutomation]) -> dict[str, StageAutomation]:
        unknown = set(v) - set(SCREENING_STAGES)
        if unknown:
            raise ValueError(f"unknown screening stage(s) in automation: {sorted(unknown)}")
        for stage in SCREENING_STAGES:
            v.setdefault(stage, StageAutomation())
        return v

    def criteria(self) -> list[Criterion]:
        return [*self.inclusion_criteria, *self.exclusion_criteria]

    def completeness_problems(self) -> list[str]:
        """What must be filled in before this protocol can be confirmed."""
        problems: list[str] = []
        if not self.research_question.strip():
            problems.append("research_question is empty")
        if not self.inclusion_criteria and not self.exclusion_criteria:
            problems.append("no inclusion or exclusion criteria are defined")
        ids = [c.id for c in self.criteria()]
        if len(ids) != len(set(ids)):
            problems.append("criterion ids are not unique")
        for c in self.criteria():
            if not c.text.strip():
                problems.append(f"criterion {c.id} has no text")
        for key in PUBLICATION_TYPE_KEYS:
            if self.publication_types[key].treatment == "undecided":
                problems.append(f"publication_types.{key}.treatment is 'undecided'")
        if not self.supporting_evidence_role.strip():
            problems.append("supporting_evidence_role is empty (state how out-of-scope supporting evidence is used)")
        return problems


@dataclass(frozen=True)
class ProtocolVersion:
    id: str
    version: int
    status: str
    protocol: Protocol
    content_sha256: str
    change_summary: str
    created_at: str
    created_by: str
    confirmed_at: str | None
    confirmed_by: str | None

    @property
    def is_confirmed(self) -> bool:
        return self.status == "confirmed"

    @property
    def label(self) -> str:
        """Label used in the database, dashboard and exports, e.g. ``v3 (PROVISIONAL)``."""
        return f"v{self.version} ({'confirmed' if self.is_confirmed else 'PROVISIONAL'})"


def _from_row(row: sqlite3.Row) -> ProtocolVersion:
    return ProtocolVersion(
        id=row["id"], version=row["version"], status=row["status"],
        protocol=Protocol.model_validate_json(row["content_json"]),
        content_sha256=row["content_sha256"], change_summary=row["change_summary"],
        created_at=row["created_at"], created_by=row["created_by"],
        confirmed_at=row["confirmed_at"], confirmed_by=row["confirmed_by"],
    )


def _content(protocol: Protocol) -> tuple[str, str]:
    content = canonical_json(protocol.model_dump(mode="json"))
    return content, sha256_text(content)


def current(conn: sqlite3.Connection) -> ProtocolVersion:
    """The latest protocol version (every project has at least an empty provisional one)."""
    row = conn.execute("SELECT * FROM protocol_version ORDER BY version DESC LIMIT 1").fetchone()
    if row is None:
        raise ProtocolError("Project has no protocol version; the database was not initialised correctly.")
    return _from_row(row)


def get_version(conn: sqlite3.Connection, version_id: str) -> ProtocolVersion:
    row = conn.execute("SELECT * FROM protocol_version WHERE id = ?", (version_id,)).fetchone()
    if row is None:
        raise ProtocolError(f"Unknown protocol version id {version_id}")
    return _from_row(row)


def list_versions(conn: sqlite3.Connection) -> list[ProtocolVersion]:
    return [_from_row(r) for r in conn.execute("SELECT * FROM protocol_version ORDER BY version")]


def changed_parts(old: Protocol, new: Protocol) -> list[str]:
    """Names of protocol parts that differ, e.g. ``criterion:E2``, ``date_limits``, ``publication_types.reviews``."""
    parts: list[str] = []
    o, n = old.model_dump(mode="json"), new.model_dump(mode="json")
    for key in ("research_question", "target_systems", "interventions", "comparators", "outcomes",
                "measurement_types", "study_designs", "date_limits", "languages", "supporting_evidence_role",
                "notes"):
        if o[key] != n[key]:
            parts.append(key)
    for key in PUBLICATION_TYPE_KEYS:
        if o["publication_types"].get(key) != n["publication_types"].get(key):
            parts.append(f"publication_types.{key}")
    for stage in SCREENING_STAGES:
        if o["automation"].get(stage) != n["automation"].get(stage):
            parts.append(f"automation.{stage}")
    old_c = {c.id: c.model_dump() for c in old.criteria()}
    new_c = {c.id: c.model_dump() for c in new.criteria()}
    for cid in sorted(set(old_c) | set(new_c)):
        if old_c.get(cid) != new_c.get(cid):
            parts.append(f"criterion:{cid}")
    old_kind = {c.id: "inclusion" for c in old.inclusion_criteria} | {c.id: "exclusion" for c in old.exclusion_criteria}
    new_kind = {c.id: "inclusion" for c in new.inclusion_criteria} | {c.id: "exclusion" for c in new.exclusion_criteria}
    for cid in sorted(set(old_kind) & set(new_kind)):
        if old_kind[cid] != new_kind[cid] and f"criterion:{cid}" not in parts:
            parts.append(f"criterion:{cid}")
    return parts


#: Parts whose change cannot alter an eligibility decision.
_NON_ELIGIBILITY_PARTS = ("notes", "automation.")


def _affects_decision(parts: list[str], criterion_id: str | None, decision: str) -> str | None:
    """Return the reason a decision must be re-reviewed after ``parts`` changed, else ``None``."""
    relevant = [p for p in parts if not p.startswith(_NON_ELIGIBILITY_PARTS)]
    if not relevant:
        return None
    if decision == "exclude" and criterion_id:
        # rule-based exclusions cite pseudo-criteria: date_limit, language, publication_type:<key>
        keys = {f"criterion:{criterion_id}",
                {"date_limit": "date_limits", "language": "languages"}.get(criterion_id, criterion_id)}
        if criterion_id.startswith("publication_type:"):
            keys.add("publication_types." + criterion_id.split(":", 1)[1])
        hit = sorted(keys & set(relevant))
        return f"cited criterion changed: {', '.join(hit)}" if hit else None
    # include / uncertain decisions (and exclusions without a cited criterion) depend on the criteria as a whole
    return "eligibility criteria changed: " + ", ".join(relevant)


def initialise(conn: sqlite3.Connection, actor: Actor) -> ProtocolVersion:
    """Create version 1: the empty, provisional protocol."""
    proto = Protocol()
    content, digest = _content(proto)
    pid = new_id("prt")
    with transaction(conn):
        conn.execute(
            "INSERT INTO protocol_version (id, version, status, content_json, content_sha256, change_summary,"
            " created_at, created_by) VALUES (?,?,?,?,?,?,?,?)",
            (pid, 1, "provisional", content, digest, "Empty provisional protocol created with the project",
             utc_now(), actor.name),
        )
        audit.record(conn, actor, "protocol.create_version", "protocol_version", pid,
                     {"version": 1, "status": "provisional", "content_sha256": digest})
    return current(conn)


def save_new_version(conn: sqlite3.Connection, protocol: Protocol, actor: Actor,
                     change_summary: str = "") -> ProtocolVersion:
    """Store edited criteria as a new *provisional* version.

    Earlier screening decisions are never replaced. Current decisions whose
    governing criteria changed are flagged ``needs_rereview`` with the reason.
    Saving identical content is a no-op and returns the current version.
    """
    prev = current(conn)
    content, digest = _content(protocol)
    if digest == prev.content_sha256:
        return prev
    parts = changed_parts(prev.protocol, protocol)
    pid = new_id("prt")
    with transaction(conn):
        conn.execute(
            "INSERT INTO protocol_version (id, version, status, content_json, content_sha256, change_summary,"
            " changed_parts_json, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, prev.version + 1, "provisional", content, digest, change_summary, canonical_json(parts),
             utc_now(), actor.name),
        )
        flagged = 0
        for row in conn.execute(
            "SELECT id, criterion_id, decision FROM screening_decision WHERE is_current = 1"
        ).fetchall():
            reason = _affects_decision(parts, row["criterion_id"], row["decision"])
            if reason:
                conn.execute(
                    "UPDATE screening_decision SET needs_rereview = 1, rereview_reason = ? WHERE id = ?",
                    (f"protocol v{prev.version} -> v{prev.version + 1}: {reason}", row["id"]),
                )
                flagged += 1
        audit.record(conn, actor, "protocol.create_version", "protocol_version", pid,
                     {"version": prev.version + 1, "status": "provisional", "content_sha256": digest,
                      "changed_parts": parts, "decisions_flagged_for_rereview": flagged,
                      "change_summary": change_summary})
    return current(conn)


def confirm(conn: sqlite3.Connection, actor: Actor, project_data_class: str) -> ProtocolVersion:
    """Confirm the current version. Requires a reviewer and a complete protocol.

    Simulated actors may confirm only in synthetic projects (for the offline
    demonstration). Decisions recorded while this version was provisional stay
    provisional and are flagged for re-review; they are not promoted automatically.
    """
    pv = current(conn)
    if pv.is_confirmed:
        return pv
    if actor.kind == "simulated" and project_data_class != "synthetic":
        raise ProtocolError("Simulated reviewers cannot confirm a protocol in a real project.")
    if actor.kind not in ("human", "simulated"):
        raise ProtocolError("A protocol can only be confirmed by a named reviewer, not by an automated component.")
    problems = pv.protocol.completeness_problems()
    if problems:
        raise ProtocolError(
            "The protocol cannot be confirmed yet. Edit the protocol file and save a new version first:\n  - "
            + "\n  - ".join(problems)
        )
    now = utc_now()
    with transaction(conn):
        conn.execute(
            "UPDATE protocol_version SET status = 'confirmed', confirmed_at = ?, confirmed_by = ? WHERE id = ?",
            (now, actor.name, pv.id),
        )
        cur = conn.execute(
            "UPDATE screening_decision SET needs_rereview = 1, rereview_reason = ?"
            " WHERE is_current = 1 AND finality = 'provisional'",
            (f"recorded under a provisional protocol; protocol v{pv.version} is now confirmed",),
        )
        audit.record(conn, actor, "protocol.confirm", "protocol_version", pv.id,
                     {"version": pv.version, "provisional_decisions_flagged": cur.rowcount})
    return current(conn)


def load_protocol_file(path: Path) -> Protocol:
    """Read a protocol YAML file edited by the researcher."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError as exc:
        raise ProtocolError(f"Protocol file not found: {path}") from exc
    try:
        return Protocol.model_validate(data)
    except Exception as exc:
        raise ProtocolError(f"Invalid protocol file {path}:\n{exc}") from exc


def dump_protocol(protocol: Protocol) -> str:
    return yaml.safe_dump(protocol.model_dump(mode="json"), sort_keys=False, allow_unicode=True, width=100)


def template_text() -> str:
    """The empty protocol template shipped with the application."""
    from importlib import resources

    return resources.files("geodesic").joinpath("templates/protocol_template.yaml").read_text(encoding="utf-8")
