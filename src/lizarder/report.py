"""Render per-project rows as a text table, JSON or CSV."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import fields
from typing import Callable, List, Optional, Sequence, Tuple, Union

from lizarder.analysis import ProjectMetrics

Format = Union[str, Callable[[object], str]]
Column = Tuple[str, str, Format]

FIELDS = [f.name for f in fields(ProjectMetrics)]

# Table columns are (header, attribute, format), where format is a str.format
# pattern for the attribute or a callable taking the whole row.

# Shown identically by every command; one entry per analysis.SHARED_FIELDS.
SHARED_COLUMNS: List[Column] = [
    ("Source Files", "files", "{:,}"),
    ("Tracked Files", "tracked_files", "{:,}"),
    ("Functions", "functions", "{:,}"),
    ("NLOC", "nloc", "{:,}"),
]

TABLE_COLUMNS: List[Column] = [
    ("Project", "path", "{}"),
    *SHARED_COLUMNS,
    ("Avg NLOC", "avg_nloc", "{:.1f}"),
    ("Avg CCN", "avg_ccn", "{:.2f}"),
    ("Max CCN", "max_ccn", "{}"),
    # Lizard's per-function token count, not AI tokens.
    ("Avg Code Tokens", "avg_tokens", "{:.1f}"),
    ("Avg Params", "avg_params", "{:.2f}"),
    ("Warnings", "warnings", "{:,}"),
    ("Warn %", "warning_ratio", "{:.1%}"),
]


def compact(n: float) -> str:
    """1234567 -> '1.23M'."""
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= limit:
            return f"{n / limit:.{2 if suffix != 'k' else 1}f}{suffix}"
    return f"{n:.0f}"


ESTIMATE_COLUMNS: List[Column] = [
    ("Project", "path", lambda r: r.path + (" *" if r.extrapolated else "")),
    *SHARED_COLUMNS,
    ("Est. Input", "input_tokens", lambda r: compact(r.input_tokens)),
    ("Est. Output", "output_tokens", lambda r: compact(r.output_tokens)),
    ("Est. Total", "total_tokens", lambda r: compact(r.total_tokens)),
    ("Likely Range", "total_low", lambda r: f"{compact(r.total_low)} - {compact(r.total_high)}"),
]


def render_table(rows: Sequence, columns: Sequence[Column], footer=None) -> str:
    """Rows (and the optional footer) are objects with the column attributes,
    plus `path` and `error`."""
    def cells(row) -> List[str]:
        out = []
        for _, attr, fmt in columns:
            value = getattr(row, attr)
            out.append(fmt(row) if callable(fmt) else fmt.format(value))
        if row.error:
            out[0] += " (error)"
        return out

    headers = [h for h, _, _ in columns]
    body = [cells(r) for r in rows]
    tail = [cells(footer)] if footer is not None else []
    widths = [max(len(r[i]) for r in [headers, *tail, *body]) for i in range(len(headers))]

    def line(row: List[str]) -> str:
        # Left-align the project name, right-align the numbers.
        parts = [row[0].ljust(widths[0])] + [c.rjust(w) for c, w in zip(row[1:], widths[1:])]
        return "  ".join(parts).rstrip()

    rule = "  ".join("-" * w for w in widths)
    out = [line(headers), rule, *(line(r) for r in body)]
    if tail:
        out += [rule, line(tail[0])]

    errors = [r for r in rows if r.error]
    if errors:
        out.append("")
        out.extend(f"error in {r.path}: {r.error}" for r in errors)
    return "\n".join(out) + "\n"


def render_json(rows: Sequence, total: Optional[dict], meta: dict) -> str:
    doc = {**meta, "projects": [r.to_dict() for r in rows]}
    if total is not None:
        doc["total"] = total
    return json.dumps(doc, indent=2) + "\n"


def render_csv(rows: Sequence, fieldnames: Sequence[str] = FIELDS) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    for r in rows:
        writer.writerow(r.to_dict())
    return buf.getvalue()
