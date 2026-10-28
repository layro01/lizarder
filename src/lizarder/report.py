"""Render project metrics as a text table, JSON or CSV."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import fields
from typing import List, Sequence

from lizarder.analysis import ProjectMetrics

FIELDS = [f.name for f in fields(ProjectMetrics)]

# (header, attribute, format) for the human-readable table.
TABLE_COLUMNS = [
    ("Project", "path", "{}"),
    ("Files", "files", "{:,}"),
    ("Functions", "functions", "{:,}"),
    ("NLOC", "nloc", "{:,}"),
    ("Avg NLOC", "avg_nloc", "{:.1f}"),
    ("Avg CCN", "avg_ccn", "{:.2f}"),
    ("Max CCN", "max_ccn", "{}"),
    ("Avg Tokens", "avg_tokens", "{:.1f}"),
    ("Avg Params", "avg_params", "{:.2f}"),
    ("Warnings", "warnings", "{:,}"),
    ("Warn %", "warning_ratio", "{:.1%}"),
]


def render_table(projects: Sequence[ProjectMetrics], total: ProjectMetrics) -> str:
    def cells(m: ProjectMetrics) -> List[str]:
        row = [fmt.format(getattr(m, attr)) for _, attr, fmt in TABLE_COLUMNS]
        if m.error:
            row[0] += " (error)"
        return row

    headers = [h for h, _, _ in TABLE_COLUMNS]
    body = [cells(m) for m in projects]
    footer = cells(total)
    footer[0] = "TOTAL"
    widths = [max(len(r[i]) for r in [headers, footer, *body]) for i in range(len(headers))]

    def line(row: List[str]) -> str:
        # Left-align the project name, right-align the numbers.
        parts = [row[0].ljust(widths[0])] + [c.rjust(w) for c, w in zip(row[1:], widths[1:])]
        return "  ".join(parts).rstrip()

    rule = "  ".join("-" * w for w in widths)
    out = [line(headers), rule, *(line(r) for r in body), rule, line(footer)]

    errors = [m for m in projects if m.error]
    if errors:
        out.append("")
        out.extend(f"error in {m.path}: {m.error}" for m in errors)
    return "\n".join(out) + "\n"


def render_json(projects: Sequence[ProjectMetrics], total: ProjectMetrics, meta: dict) -> str:
    total_dict = total.to_dict()
    for key in ("project", "path", "branch", "commit", "error"):
        total_dict.pop(key)
    doc = {**meta, "projects": [m.to_dict() for m in projects], "total": total_dict}
    return json.dumps(doc, indent=2) + "\n"


def render_csv(projects: Sequence[ProjectMetrics]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for m in projects:
        writer.writerow(m.to_dict())
    return buf.getvalue()
