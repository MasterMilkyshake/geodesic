"""Shared helpers of the dashboard pages.

The dashboard holds no application logic: each page calls the public functions
the CLI calls and shows what they return. Helpers here deal only with Streamlit
(tables, messages, the acting reviewer, error display).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd
import streamlit as st

from ..errors import GeodesicError
from ..project import Project
from ..util import Actor

FLASH = "_geodesic_flash"
NO_REVIEWER = ("Enter your reviewer name in the sidebar first. Review actions are recorded under the name of the "
               "person who made them.")


@dataclass
class Ctx:
    """What a page needs: the open project and who is acting."""

    project: Project
    reviewer_name: str

    @property
    def conn(self):
        return self.project.conn

    def reviewer(self) -> Actor:
        """The named human reviewer; raises when no name was entered."""
        name = (self.reviewer_name or "").strip()
        if not name:
            raise GeodesicError(NO_REVIEWER)
        return Actor(name, "human")

    def operator(self) -> Actor:
        """Who ran a non-review action: the named reviewer when there is one, else the dashboard itself."""
        name = (self.reviewer_name or "").strip()
        return Actor(name, "human") if name else Actor("geodesic-dashboard", "system")


def attempt(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[bool, Any]:
    """Call an application function; show a refusal or input error instead of a traceback."""
    try:
        return True, fn(*args, **kwargs)
    except (GeodesicError, OSError, ValueError) as exc:
        st.error(str(exc))
        return False, None


def done(message: str) -> None:
    """Show ``message`` after reloading the page, so every table reflects the action just taken."""
    st.session_state[FLASH] = message
    st.rerun()


def show_flash() -> None:
    message = st.session_state.pop(FLASH, None)
    if message:
        st.success(message)


def _cell(value: Any) -> Any:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in value.items())
    return value


def table(rows: Iterable[dict[str, Any]], columns: list[str] | None = None, *, empty: str = "Nothing to show.",
          key: str | None = None, height: int | None = None) -> None:
    """Show dict rows as a table (selected ``columns`` in order), or ``empty`` when there are none."""
    rows = list(rows)
    if not rows:
        st.caption(empty)
        return
    cols = columns or list(rows[0].keys())
    frame = pd.DataFrame([{c: _cell(r.get(c)) for c in cols} for r in rows], columns=cols)
    kwargs: dict[str, Any] = {"hide_index": True}
    if key:
        kwargs["key"] = key
    if height:
        kwargs["height"] = height
    st.dataframe(frame, **kwargs)


def export_table(tab: Any, *, empty: str = "Nothing to show.", key: str | None = None) -> None:
    """Show an export ``Table`` (the object the table exports write) with its note."""
    table(tab.rows, list(tab.columns), empty=empty, key=key)
    if tab.note:
        st.caption(tab.note)


def kv(d: dict[str, Any] | None) -> str:
    return ", ".join(f"{k}: {v}" for k, v in (d or {}).items()) or "none"


def csv_list(text: str | None) -> list[str]:
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def opt(text: str | None) -> str | None:
    """Empty input means "not given"."""
    text = (text or "").strip()
    return text or None


def path_of(text: str | None) -> Path | None:
    text = (text or "").strip()
    return Path(text).expanduser() if text else None


def pager(total: int, key: str, page_size: int = 50) -> int:
    """Offset chosen with a page control; nothing is shown for a single page."""
    pages = max(1, -(-total // page_size))
    if pages == 1:
        return 0
    page = st.number_input(f"Page (of {pages}; {total} rows)", min_value=1, max_value=pages, value=1, step=1, key=key)
    return (int(page) - 1) * page_size


def paper_label(row: dict[str, Any], limit: int = 90) -> str:
    text = row.get("title") or row.get("citation_text") or row.get("paper_title") or row.get("paper_citation") or ""
    year = row.get("year") or row.get("paper_year")
    label = (text[:limit] + "…") if len(text) > limit else text
    return f"{label} ({year})" if year else (label or "(no title or citation)")


def lines_of(text: str | None) -> list[str]:
    """Non-empty lines of a text area (one ``field=value`` entry per line)."""
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def paper_picker(ctx: Ctx, key: str, label: str = "Paper (ID, or words from its title or citation)") -> str | None:
    """Choose one paper record: type its ID, or words to search for, then pick from the matches."""
    from .. import query

    text = opt(st.text_input(label, key=f"{key}_text"))
    if not text:
        return None
    if ctx.conn.execute("SELECT 1 FROM paper WHERE id = ?", (text,)).fetchone():
        return text
    page = query.search_papers(ctx.conn, query.PaperFilter(text=text, canonical_only=False), limit=25)
    if not page.rows:
        st.caption("No paper record matches.")
        return None
    labels = {r["id"]: f"{paper_label(r, 70)} · {r['id']}" for r in page.rows}
    if page.total > len(page.rows):
        st.caption(f"{page.total} records match; the first {len(page.rows)} are listed. Add words to narrow.")
    return st.selectbox("Matching paper records", list(labels), format_func=labels.get, key=f"{key}_pick")


def finding_text(finding: dict[str, Any]) -> str:
    """The plain-text description of a finding that the CLI prints (same function)."""
    from ..cli_docs import _finding_lines

    return "\n".join(_finding_lines(finding))


REVIEW_STATUSES = ("draft", "unreviewed", "verified", "rejected", "stale")


def finding_filter(ctx: Ctx, key: str, *, expanded: bool = False, label: str = "Filter findings") -> Any:
    """Filter controls shared by the pages that list findings. Returns a ``query.FindingFilter``."""
    from .. import findings as F
    from .. import query

    with st.expander(label, expanded=expanded):
        c1, c2, c3 = st.columns(3)
        text = c1.text_input("Words in the wording or fields", key=f"{key}_text")
        paper_text = c2.text_input("Words in the paper title or citation", key=f"{key}_paper")
        stages = c3.multiselect("Stages", [s.key for s in ctx.project.config.stages], key=f"{key}_stages")
        c1, c2, c3 = st.columns(3)
        statuses = c1.multiselect("Review status", list(REVIEW_STATUSES), key=f"{key}_review")
        kinds = c2.multiselect("Kind", ["measurement", "narrative", "limitation"], key=f"{key}_kinds")
        stypes = c3.multiselect("Statement type", list(F.STATEMENT_TYPES), key=f"{key}_stypes")
        c1, c2, c3, c4 = st.columns(4)
        system = c1.text_input("System / context contains", key=f"{key}_system")
        outcome = c2.text_input("Outcome contains", key=f"{key}_outcome")
        year_min = c3.number_input("Year from", min_value=0, max_value=3000, value=None, step=1, key=f"{key}_ymin")
        year_max = c4.number_input("Year to", min_value=0, max_value=3000, value=None, step=1, key=f"{key}_ymax")
        c1, c2 = st.columns(2)
        quant = c1.selectbox("Quantitative", ["(any)", "yes", "no"], key=f"{key}_quant")
        flags = c2.text_input("Review flags (comma-separated)", key=f"{key}_flags")
    return query.FindingFilter(
        text=opt(text), paper_text=opt(paper_text), stage_keys=stages or None, review_statuses=statuses or None,
        kinds=kinds or None, statement_types=stypes or None, system_context=opt(system), outcome=opt(outcome),
        year_min=int(year_min) if year_min is not None else None,
        year_max=int(year_max) if year_max is not None else None,
        quantitative={"yes": True, "no": False}.get(quant), flags=csv_list(flags) or None)


FINDING_COLUMNS = ["id", "paper", "kind", "statement_type", "outcome", "system_context", "value_literal",
                   "unit_original", "review_status", "grounding_status", "stages", "source_wording"]


def finding_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**r, "paper": paper_label(r, 50)} for r in rows]
