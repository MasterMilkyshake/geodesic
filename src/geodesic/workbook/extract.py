"""Turn workbook cells into paper references and draft findings (no database access).

This is the mapping-driven, deterministic half of the importer. It reads the
in-memory workbook through a confirmed mapping and produces plain records; the
importer stores them and handles versions.

Row rules
---------
* Identity, stage and context columns use the text that applies to the row;
  inside a merged range that is the anchor cell (``merged_cells: fill_down``),
  and the source location always points at the anchor cell.
* Content columns (narrative findings, measurements, limitations) are read
  only where the text is stored, so a merged cell yields its findings once.
* A row with content but no paper reference is not imported as evidence; its
  cells are recorded as unparsed content.
* Nothing is inferred that the cells do not say: no title or year is derived
  from citation text, a range stays a range, a bound stays a bound, and ``±``
  without a stated meaning stays ``unspecified_plus_minus``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from openpyxl.utils import get_column_letter

from .. import findings as F
from ..identifiers import doi_from_url, find_doi, find_pmid, normalize_doi, normalize_pmid
from ..locations import EXCERPT_MAX_CHARS
from ..textparse import (DEFAULT_QUALIFIER_PHRASES, Quantity, TextParse, check_transcription, parse_text,
                         split_segments)
from ..util import normalize_text_key, sha256_text
from .mapping import CONTEXT_ROLES, SheetMapping, WorkbookMapping
from .reader import CellRecord, SheetData, WorkbookData

ROLE_CODES = {"narrative_findings": "N", "measurements": "M", "limitations": "L"}
#: A proposer suggests structured content for one segment: ``(cell_text, seg_start, seg_end) -> [proposal]``,
#: each proposal ``{"content": {...}, "spans": {...}}`` with cell-level spans for every literal.
Proposer = Callable[[str, int, int], list[dict[str, Any]]]

#: Flag carried by findings that came from a proposer rather than the deterministic parser (always drafts).
EXTERNAL_PROPOSAL_FLAG = "proposed_by_external_proposer"
EXTERNAL_PROPOSER_METHOD = "external_proposer"


@dataclass
class PaperRef:
    identity_key: str
    citation_text: str | None = None
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    doi: str | None = None
    pmid: str | None = None
    urls: list[str] = field(default_factory=list)
    identifiers: list[tuple[str, str]] = field(default_factory=list)       # (scheme, raw) as written
    conflicts: list[tuple[str, Any, Any, str]] = field(default_factory=list)  # field, existing, incoming, where
    locations: list[dict[str, Any]] = field(default_factory=list)
    stage_keys: set[str] = field(default_factory=set)
    rows: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class DraftFinding:
    paper_key: str
    role_code: str
    sheet: str
    row: int
    cell_ref: str
    seg_start: int
    seg_norm: str
    qidx: int
    content: dict[str, Any]
    spans: dict[str, Any]
    locations: list[dict[str, Any]]
    stage_keys: list[str]
    review_flags: list[str]
    limitation_cell: tuple[str, str] | None = None     # (sheet, cell_ref) of the limitation text applying to the row

    @property
    def review_status(self) -> str:
        return "draft" if set(self.review_flags) & F.DRAFT_FLAGS else "unreviewed"


@dataclass
class Extracted:
    papers: dict[str, PaperRef] = field(default_factory=dict)
    findings: list[DraftFinding] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    unparsed: list[dict[str, Any]] = field(default_factory=list)
    parse_failures: list[dict[str, Any]] = field(default_factory=list)
    evidence_rows_by_sheet: dict[str, int] = field(default_factory=dict)


def cell_loc(cell: CellRecord, role: str, start: int | None = None, end: int | None = None) -> dict[str, Any]:
    """Location dictionary for (part of) a workbook cell; the importer adds the workbook version."""
    text = cell.raw_text or ""
    if start is None:
        if len(text) <= EXCERPT_MAX_CHARS:
            return {"role": role, "kind": "workbook_cell", "sheet": cell.sheet, "cell_ref": cell.cell_ref,
                    "char_start": None, "char_end": None, "excerpt": text}
        start, end = 0, EXCERPT_MAX_CHARS
    return {"role": role, "kind": "workbook_cell", "sheet": cell.sheet, "cell_ref": cell.cell_ref,
            "char_start": start, "char_end": end, "excerpt": text[start:end]}


def _text(cell: CellRecord | None) -> str | None:
    if cell is None or cell.raw_text is None:
        return None
    return cell.raw_text.strip() or None


_YEAR_RE = re.compile(r"^(1[5-9]\d\d|20\d\d|21\d\d)(?:\.0)?$")


class _SheetExtractor:
    def __init__(self, sd: SheetData, sm: SheetMapping, out: Extracted, qualifier_phrases: tuple[str, ...],
                 on_semicolon: bool, proposer: Proposer | None, cit_index: dict[str, str]):
        self.sd, self.sm, self.out = sd, sm, out
        self.phrases, self.on_semicolon, self.proposer = qualifier_phrases, on_semicolon, proposer
        self.cit_index = cit_index
        self.fill = sm.merged_cells == "fill_down"
        self.col = {role: sm.column_for(role) for role in
                    ("paper_reference", "doi", "pmid", "url", "title", "authors", "year", "journal", "stage",
                     "timepoint", *CONTEXT_ROLES)}
        self.mapped_cols = {c.index for c in sm.columns if c.role != "unmapped"}
        self.value_map = {normalize_text_key(k): v for k, v in sm.stage.value_map.items()}
        self.pattern_cols = [(c.index, pf.role, pf.compiled()) for c in sm.columns for pf in c.patterns]
        #: cell_ref -> character spans of the current row's pattern matches (context, not findings)
        self._consumed: dict[str, list[tuple[int, int]]] = {}

    def eff(self, row: int, role: str) -> CellRecord | None:
        cm = self.col.get(role)
        if cm is None:
            return None
        cell = self.sd.effective(row, cm.index, self.fill)
        return cell if _text(cell) is not None or (cell is not None and cell.hyperlink) else None

    def header(self, col_idx: int) -> CellRecord | None:
        return self.sd.cells.get((self.sm.header_row, col_idx)) if self.sm.header_row else None

    # ---------------------------------------------------------------- paper identity
    def paper_ref(self, row: int) -> PaperRef | None:
        ref, doi_c, pmid_c, url_c, title_c = (self.eff(row, r) for r in
                                              ("paper_reference", "doi", "pmid", "url", "title"))
        citation, title = _text(ref), _text(title_c)
        idents: list[tuple[str, str]] = []
        conflicts: list[tuple[str, Any, Any, str]] = []
        doi: str | None = None
        doi_sources: list[tuple[str, str]] = []
        if _text(doi_c):
            idents.append(("doi", _text(doi_c)))
            norm = normalize_doi(_text(doi_c))
            if norm:
                doi_sources.append((norm, f"{self.sd.name}!{doi_c.cell_ref}"))
        if citation and (hit := find_doi(citation)):
            idents.append(("doi", hit[0]))
            doi_sources.append((hit[1], f"{self.sd.name}!{ref.cell_ref} (reference text)"))
        urls: list[str] = []
        for cell in (ref, url_c, doi_c, title_c):
            for u in ((cell.hyperlink if cell else None), (_text(cell) if cell is url_c else None)):
                if u and u not in urls:
                    urls.append(u)
                    if (norm := doi_from_url(u)):
                        idents.append(("doi", u))
                        doi_sources.append((norm, f"{self.sd.name}!{cell.cell_ref} (link)"))
        for norm, where in doi_sources:
            if doi is None:
                doi = norm
            elif norm != doi:
                conflicts.append(("doi", doi, norm, where))
        pmid = None
        if _text(pmid_c):
            idents.append(("pmid", _text(pmid_c)))
            pmid = normalize_pmid(_text(pmid_c))
        if pmid is None and citation and (found := find_pmid(citation)):
            idents.append(("pmid", found))
            pmid = found
        cit_hash = sha256_text(normalize_text_key(citation))[:32] if citation else None
        if doi:
            key = f"doi:{doi}"
        elif pmid:
            key = f"pmid:{pmid}"
        elif cit_hash:
            key = self.cit_index.get(cit_hash, f"cit:{cit_hash}")
        elif title:
            key = f"title:{sha256_text(normalize_text_key(title))[:32]}"
        else:
            return None
        if cit_hash:
            self.cit_index.setdefault(cit_hash, key)
        year = None
        if (ytxt := _text(self.eff(row, "year"))) and (m := _YEAR_RE.match(ytxt)):
            year = int(m.group(1))
        authors_txt = _text(self.eff(row, "authors"))
        incoming = {"citation_text": citation, "title": title, "year": year,
                    "journal": _text(self.eff(row, "journal")),
                    "authors": [a.strip() for a in authors_txt.split(";") if a.strip()] if authors_txt else []}
        p = self.out.papers.get(key)
        if p is None:
            p = PaperRef(identity_key=key, doi=doi, pmid=pmid, **incoming)
            for role, cell in (("citation", ref), ("identifier:doi", doi_c), ("identifier:pmid", pmid_c),
                               ("title", title_c)):
                if cell is not None and _text(cell):
                    p.locations.append(cell_loc(cell, role))
            self.out.papers[key] = p
        else:
            where = f"{self.sd.name} row {row}"
            for fld, new in incoming.items():
                old = getattr(p, fld)
                if new in (None, "", []):
                    continue
                if old in (None, "", []):
                    setattr(p, fld, new)
                elif normalize_text_key(str(old)) != normalize_text_key(str(new)):
                    conflicts.append((fld, old, new, where))
            p.pmid = p.pmid or pmid
        for item in idents:
            if item not in p.identifiers:
                p.identifiers.append(item)
        for u in urls:
            if u not in p.urls:
                p.urls.append(u)
        for c in conflicts:
            if c not in p.conflicts:
                p.conflicts.append(c)
        p.rows.append((self.sd.name, row))
        return p

    # ---------------------------------------------------------------- stages
    def stage_keys(self, row: int) -> list[str]:
        st = self.sm.stage
        if st.mode == "fixed":
            return list(st.stage_keys)
        if st.mode != "column":
            return []
        cell = self.eff(row, "stage")
        if cell is None or not _text(cell):
            return []
        keys: list[str] = []
        pos = 0
        for term in cell.raw_text.split(st.separator) if st.separator else [cell.raw_text]:
            start = cell.raw_text.find(term, pos)
            pos = start + len(term)
            if not term.strip():
                continue
            key = self.value_map.get(normalize_text_key(term))
            if key:
                if key not in keys:
                    keys.append(key)
            elif (self.sd.name, cell.cell_ref, start) not in self._seen_terms:
                self._seen_terms.add((self.sd.name, cell.cell_ref, start))
                self.out.unparsed.append({"sheet": self.sd.name, "cell_ref": cell.cell_ref, "char_start": start,
                                          "char_end": pos, "text": term, "reason": "unmapped_stage_term"})
        return keys

    _seen_terms: set = set()

    # ---------------------------------------------------------------- findings
    def context(self, row: int) -> tuple[dict[str, Any], dict[str, Any], list[dict], list[str], dict[str, str]]:
        """Row-level context shared by the row's findings: (content, spans, locations, flags, field_status)."""
        content: dict[str, Any] = {}
        spans: dict[str, Any] = {}
        locs: list[dict] = []
        flags: list[str] = []
        status: dict[str, str] = {}
        for role in CONTEXT_ROLES:
            cell = self.eff(row, role)
            if cell is not None and _text(cell):
                content[role] = _text(cell)
                locs.append(cell_loc(cell, f"context:{role}"))
        cell = self.eff(row, "timepoint")
        if cell is not None and _text(cell):
            raw = cell.raw_text
            locs.append(cell_loc(cell, "context:timepoint"))
            self._timepoint_context(raw, len(raw) - len(raw.lstrip()), len(raw.rstrip()), content, spans, flags,
                                    status, "timepoint_column_not_parsed")
        self._consumed = {}
        for col_idx, role, rx in self.pattern_cols:
            cell = self.sd.effective(row, col_idx, self.fill)
            raw = cell.raw_text if cell is not None else None
            m = rx.search(raw) if raw else None
            if m is None or m.group("value") is None:
                continue
            s, e = m.span("value")
            while s < e and raw[s].isspace():
                s += 1
            while e > s and raw[e - 1].isspace():
                e -= 1
            if s >= e:
                continue
            self._consumed.setdefault(cell.cell_ref, []).append(m.span())
            locs.append(cell_loc(cell, f"context:{role}", s, e))
            if role == "timepoint":
                self._timepoint_context(raw, s, e, content, spans, flags, status, "timepoint_pattern_not_parsed")
            else:
                content[role] = raw[s:e]
        return content, spans, locs, flags, status

    def _timepoint_context(self, raw: str, start: int, end: int, content: dict[str, Any], spans: dict[str, Any],
                           flags: list[str], status: dict[str, str], unparsed_flag: str) -> None:
        """Row timepoint from ``raw[start:end]``; spans address the whole cell text."""
        text = raw[start:end]
        p = parse_text(text, self.phrases)
        times = p.timepoints + [q for q in p.quantities if q.is_time]
        if len(times) == 1 and times[0].statement_type in ("exact", "approximate"):
            t = times[0]
            s, e = _time_span(t)
            content.update(timepoint_literal=text[s:e], timepoint_value=t.value_num, time_unit=t.unit_literal)
            spans["timepoint_literal"] = {"role": "context:timepoint", "start": start + s, "end": start + e}
        else:
            content["timepoint_literal"] = text
            spans["timepoint_literal"] = {"role": "context:timepoint", "start": start, "end": end}
            status["timepoint"] = "could_not_be_parsed"
            flags.append(unparsed_flag)

    def _measurement(self, q: Quantity, p: TextParse, off: int, base: dict[str, Any],
                     seg_text: str) -> tuple[dict, dict, list[str], dict]:
        return measurement_content(q, p, off, base, seg_text)

    def content_cell(self, cell: CellRecord, role: str, row: int, paper: PaperRef, stage_keys: list[str],
                     ctx: tuple, limitation_cell: tuple[str, str] | None, limits_text: str | None,
                     limits_loc: dict | None) -> None:
        text = cell.raw_text or ""
        code = ROLE_CODES[role]
        ctx_content, ctx_spans, ctx_locs, ctx_flags, ctx_status = ctx
        base = F.blank_content(**ctx_content)
        extra_locs = list(ctx_locs)
        base_flags = list(ctx_flags)
        if cell.comment:
            base_flags.append("source_cell_has_comment")
        if role == "measurements" and not base.get("outcome"):
            head = self.header(cell.col)
            if head is not None and _text(head):
                base["outcome"] = _text(head)
                extra_locs.append(cell_loc(head, "context:outcome"))
                base_flags.append("outcome_from_column_header")
        if role != "limitations" and limits_text:
            base["interpretation_limits"] = limits_text
            extra_locs.append(limits_loc)
        prev: list[DraftFinding] = []
        consumed = self._consumed.get(cell.cell_ref, [])
        for seg in split_segments(text, self.on_semicolon):
            if any(ms <= seg.start and seg.end <= me for ms, me in consumed):
                continue      # read by a column pattern as row context
            p = parse_text(seg.text, self.phrases)
            seg_norm = normalize_text_key(seg.text)
            primary = cell_loc(cell, F.PRIMARY_ROLE, seg.start, seg.end)

            def emit(content: dict, spans: dict, flags: list[str], qidx: int, status: dict | None = None) -> None:
                content = {**content, "source_wording": seg.text,
                           "field_status": {**ctx_status, **(status or {})}}
                try:
                    content = F.validate_content(content)
                except Exception as exc:     # keep the text; never drop or bend it to fit
                    self.out.parse_failures.append({
                        "sheet": cell.sheet, "cell_ref": cell.cell_ref, "char_start": seg.start,
                        "char_end": seg.end, "field": "content", "claimed_literal": None,
                        "reason": f"schema validation failed: {str(exc)[:300]}"})
                    content = F.validate_content({**base, "kind": "narrative", "statement_type": "unparsed",
                                                  "source_wording": seg.text, "field_status": dict(ctx_status)})
                    spans, flags = {}, [*flags, "transcription_rejected"]
                d = DraftFinding(paper.identity_key, code, cell.sheet, row, cell.cell_ref, seg.start, seg_norm,
                                 qidx, content, {**ctx_spans, **spans}, [primary, *extra_locs], list(stage_keys),
                                 list(dict.fromkeys([*base_flags, *flags])), limitation_cell)
                self.out.findings.append(d)
                prev_new.append(d)

            prev_new: list[DraftFinding] = []
            for u in p.unparsed:
                self.out.unparsed.append({"sheet": cell.sheet, "cell_ref": cell.cell_ref,
                                          "char_start": seg.start + u["start"], "char_end": seg.start + u["end"],
                                          "text": u["text"], "reason": u["reason"]})
            seg_quals = [{**x, "start": seg.start + x["start"], "end": seg.start + x["end"]} for x in p.qualifiers]
            if role == "limitations":
                emit({**base, "kind": "limitation", "statement_type": "qualitative", "qualifiers": seg_quals},
                     {}, [], 0)
                prev = prev_new
                continue
            if self.proposer is not None:
                if self._proposals(cell, seg, base, emit):
                    prev = prev_new
                    continue
            if not p.quantities:
                residue = seg.text
                for x in sorted(p.qualifiers, key=lambda x: -x["start"]):
                    residue = residue[:x["start"]] + residue[x["end"]:]
                if p.qualifiers and not normalize_text_key(residue) and prev:
                    # a segment that is only a caveat ("denominator unclear.") qualifies what precedes it
                    for d in prev:
                        d.content["qualifiers"] = [*d.content["qualifiers"], *seg_quals]
                        if "qualifier_scope_assumed" not in d.review_flags:
                            d.review_flags.append("qualifier_scope_assumed")
                    continue
                if role == "measurements":
                    self.out.unparsed.append({"sheet": cell.sheet, "cell_ref": cell.cell_ref,
                                              "char_start": seg.start, "char_end": seg.end, "text": seg.text,
                                              "reason": "no_quantity_in_measurement_cell"})
                    emit({**base, "kind": "narrative", "statement_type": "unparsed", "qualifiers": seg_quals},
                         {}, ["no_quantity_in_measurement_cell"], 0)
                else:
                    c = {**base, "kind": "narrative", "statement_type": "qualitative", "qualifiers": seg_quals}
                    t_spans: dict[str, Any] = {}
                    t_flags: list[str] = []
                    t_status: dict[str, str] = {}
                    _timepoint(p, seg.start, seg.text, c, t_spans, t_flags, t_status)
                    emit(c, t_spans, t_flags, 0, t_status)
                prev = prev_new
                continue
            many = ["multiple_quantities_in_segment"] if len(p.quantities) > 1 else []
            for qidx, q in enumerate(p.quantities):
                c, spans, flags, status = self._measurement(q, p, seg.start, base, seg.text)
                bad = [f for f in F.LITERAL_FIELDS if c.get(f) and f in spans and isinstance(spans[f], list)
                       and not check_transcription(text, spans[f][0], spans[f][1], c[f])]
                if bad:
                    for f in bad:
                        self.out.parse_failures.append({
                            "sheet": cell.sheet, "cell_ref": cell.cell_ref, "char_start": spans[f][0],
                            "char_end": spans[f][1], "field": f, "claimed_literal": c[f],
                            "reason": "claimed source string is absent from the specified span"})
                    emit({**base, "kind": "narrative", "statement_type": "unparsed", "qualifiers": seg_quals},
                         {}, ["transcription_rejected"], qidx)
                    continue
                emit(c, spans, [*flags, *many], qidx, status)
            prev = prev_new

    def _proposals(self, cell: CellRecord, seg, base: dict, emit) -> bool:
        """Run the optional proposer through the grounding gate. True when the segment was handled."""
        text = cell.raw_text or ""
        proposals = self.proposer(text, seg.start, seg.end)
        if not proposals:
            return False
        accepted = 0
        rejected = False
        for qidx, prop in enumerate(proposals):
            content, spans = dict(prop.get("content") or {}), dict(prop.get("spans") or {})
            problems = []
            for f in F.LITERAL_FIELDS:
                if content.get(f) in (None, ""):
                    continue
                sp = spans.get(f)
                ok = (isinstance(sp, (list, tuple)) and len(sp) == 2 and seg.start <= sp[0] and sp[1] <= seg.end
                      and check_transcription(text, sp[0], sp[1], content[f]))
                if not ok:
                    problems.append({"sheet": cell.sheet, "cell_ref": cell.cell_ref,
                                     "char_start": sp[0] if sp else seg.start, "char_end": sp[1] if sp else seg.end,
                                     "field": f, "claimed_literal": str(content[f]),
                                     "reason": "claimed source string is absent from the specified span"})
            if problems:
                self.out.parse_failures.extend(problems)
                rejected = True
            else:
                emit({**base, **content}, spans, [EXTERNAL_PROPOSAL_FLAG], accepted, content.get("field_status"))
                accepted += 1
        if rejected and not accepted:
            # keep the text as an unparsed draft; a rejected transcription never becomes a value
            emit({**base, "kind": "narrative", "statement_type": "unparsed"}, {}, ["transcription_rejected"], 0)
        return True

    # ---------------------------------------------------------------- rows
    def run(self) -> None:
        sd, sm = self.sd, self.sm
        self._seen_terms = set()
        content_cols = [(c.index, c.role) for c in sm.columns if c.role in ROLE_CODES]
        lim_cols = [i for i, r in content_cols if r == "limitations"]
        last = min(sm.data_end_row or sd.max_row, sd.max_row)
        n_rows = 0
        for row in range(sm.data_start_row, last + 1):
            own = self._own.get(row, [])
            if not own:
                continue
            paper = self.paper_ref(row)
            cells = [(sd.cells.get((row, i)), role) for i, role in content_cols]
            cells = [(c, role) for c, role in cells if c is not None and _text(c)]
            if paper is None:
                for c in own:
                    self.out.unparsed.append({"sheet": sd.name, "cell_ref": c.cell_ref, "char_start": None,
                                              "char_end": None, "text": c.raw_text,
                                              "reason": "row_without_paper_reference"})
                continue
            n_rows += 1
            keys = self.stage_keys(row)
            paper.stage_keys.update(keys)
            unknown = {}
            for c in own:
                if c.col not in self.mapped_cols:
                    head = self.header(c.col)
                    unknown[c.cell_ref] = {"header": _text(head) if head else None, "text": c.raw_text}
            self.out.rows.append({"sheet": sd.name, "row": row, "paper_key": paper.identity_key,
                                  "stage_keys": keys, "unknown_fields": unknown})
            ctx = self.context(row)
            lim_cell = None
            for i in lim_cols:
                cand = sd.effective(row, i, self.fill)
                if cand is not None and _text(cand):
                    lim_cell = cand
                    break
            lim_key = (sd.name, lim_cell.cell_ref) if lim_cell else None
            lim_text = _text(lim_cell) if lim_cell else None
            lim_loc = cell_loc(lim_cell, "context:interpretation_limits") if lim_cell else None
            for c, role in cells:
                self.content_cell(c, role, row, paper, keys, ctx, lim_key, lim_text, lim_loc)
        self.out.evidence_rows_by_sheet[sd.name] = self.out.evidence_rows_by_sheet.get(sd.name, 0) + n_rows

    def index_rows(self) -> None:
        self._own: dict[int, list[CellRecord]] = {}
        for (r, _), c in self.sd.cells.items():
            if c.raw_text not in (None, ""):
                self._own.setdefault(r, []).append(c)
        for cells in self._own.values():
            cells.sort(key=lambda c: c.col)


def measurement_content(q: Quantity, p: TextParse, off: int, base: dict[str, Any],
                        seg_text: str) -> tuple[dict, dict, list[str], dict]:
    """Content, spans, flags and field status for one parsed quantity.

    ``off`` is the offset of the parsed text inside the stored unit, so the returned spans address
    the stored unit text (a workbook cell, a page, an element or a table cell)."""
    c = dict(base)
    spans: dict[str, Any] = {"value_literal": [off + q.value_start, off + q.value_end]}
    flags = list(q.flags)
    status: dict[str, str] = {}
    c.update(kind="measurement", statement_type=q.statement_type, value_literal=q.value_literal,
             value_num=q.value_num, value_low=q.value_low, value_high=q.value_high, bound_type=q.bound_type,
             relative_semantics=q.relative_semantics, reference_definition=q.reference_definition)
    if q.unit_literal is not None:
        c["unit_original"] = q.unit_literal
        spans["unit_original"] = [off + q.unit_start, off + q.unit_end]
    else:
        status["unit_original"] = "not_yet_extracted"
    if q.uncertainty_type:
        c.update(uncertainty_type=q.uncertainty_type, uncertainty_literal=q.uncertainty_literal,
                 uncertainty_value=q.uncertainty_value, uncertainty_low=q.uncertainty_low,
                 uncertainty_high=q.uncertainty_high,
                 confidence_level=q.confidence_level if q.uncertainty_type == "ci" else None)
        if q.uncertainty_literal is not None:
            spans["uncertainty_literal"] = [off + q.uncertainty_start, off + q.uncertainty_end]
    else:
        status["uncertainty"] = "not_yet_extracted"
    quals = [{**x, "start": off + x["start"], "end": off + x["end"]} for x in [*q.qualifiers, *p.qualifiers]]
    c["qualifiers"] = quals
    _timepoint(p, off, seg_text, c, spans, flags, status)
    if len(p.sample_sizes) == 1:
        s = p.sample_sizes[0]
        c.update(sample_size=s.value, sample_size_literal=s.literal, replicate_type=s.replicate_type)
        spans["sample_size_literal"] = [off + s.start, off + s.end]
        if s.from_count_phrase:
            flags.append("sample_size_from_count_phrase")
    elif len(p.sample_sizes) > 1:
        flags.append("sample_size_assignment_ambiguous")
        status["sample_size"] = "could_not_be_parsed"
    else:
        status["sample_size"] = "not_yet_extracted"
    if p.summary_statistic:
        c.update(summary_statistic=p.summary_statistic.get("name") or p.summary_statistic["text"],
                 observation_kind="summary_statistic")
    else:
        status["observation_kind"] = "not_yet_extracted"
    if p.normalization:
        c["normalization"] = p.normalization["text"]
    if not c.get("outcome"):
        status["outcome"] = "not_yet_extracted"
    status["observation_type"] = "not_yet_extracted"
    return c, spans, flags, status


def _timepoint(p: TextParse, off: int, seg_text: str, c: dict[str, Any], spans: dict[str, Any], flags: list[str],
               status: dict[str, str]) -> None:
    """Attach the segment's timepoint when it states exactly one; several are left for the reviewer."""
    if len(p.timepoints) == 1:
        t = p.timepoints[0]
        c.update(timepoint_literal=None, timepoint_value=None, time_unit=None)
        if t.statement_type in ("exact", "approximate") and t.unit_end is not None:
            s, e = _time_span(t)
            c.update(timepoint_literal=seg_text[s:e], timepoint_value=t.value_num, time_unit=t.unit_literal)
            spans["timepoint_literal"] = [off + s, off + e]
            status.pop("timepoint", None)
        else:
            c["timepoint_literal"] = t.value_literal
            spans["timepoint_literal"] = [off + t.value_start, off + t.value_end]
            status["timepoint"] = "could_not_be_parsed"
        flags += [f for f in t.flags if f not in flags]
    elif len(p.timepoints) > 1:
        flags.append("timepoint_assignment_ambiguous")
        status["timepoint"] = "could_not_be_parsed"
    elif not c.get("timepoint_literal"):
        status.setdefault("timepoint", "not_yet_extracted")


def _time_span(t: Quantity) -> tuple[int, int]:
    """Span of a timepoint as written, value and unit in source order (``24 h`` or ``day 7``)."""
    return min(t.value_start, t.unit_start), max(t.value_end, t.unit_end)


def extract(workbook: WorkbookData, mapping: WorkbookMapping, qualifier_phrases: tuple[str, ...] | None = None,
            on_semicolon: bool = True, proposer: Proposer | None = None) -> Extracted:
    """Extract paper references and draft findings from every evidence sheet of a validated mapping."""
    out = Extracted()
    phrases = tuple(qualifier_phrases) if qualifier_phrases is not None else DEFAULT_QUALIFIER_PHRASES
    cit_index: dict[str, str] = {}
    for sm in mapping.sheets:
        if sm.role != "evidence":
            continue
        sd = workbook.sheet(sm.sheet)
        ex = _SheetExtractor(sd, sm, out, phrases, on_semicolon, proposer, cit_index)
        ex.index_rows()
        ex.run()
    return out
