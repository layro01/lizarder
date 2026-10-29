"""Read recorded AI token usage from any system into one standard record shape.

Every record is normalised to:

    repository                 "owner/name" (or just "name") of the scanned repo
    input_tokens               ALL input tokens processed, including cached ones
    output_tokens              output tokens
    cache_read_input_tokens    the part of input_tokens read from a prompt cache
    cache_write_input_tokens   the part of input_tokens written to a prompt cache

Files can be JSON (a list, or an object holding a list), JSON Lines, or CSV.
Field names are matched against common aliases from different providers, and
nested objects such as `usage` are searched too, so most exports load as they
are. Anything else can be mapped explicitly with `field=source` pairs.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

FIELDS = ("repository", "input_tokens", "output_tokens",
          "cache_read_input_tokens", "cache_write_input_tokens")

# Checked in order; the first one present wins.
ALIASES: Dict[str, tuple] = {
    "repository": ("repository", "repo", "repo_name", "full_name", "project"),
    "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens", "promptTokenCount",
                     "input"),
    "output_tokens": ("output_tokens", "outputTokens", "completion_tokens",
                      "candidatesTokenCount", "output"),
    "cache_read_input_tokens": ("cache_read_input_tokens", "cacheReadInputTokens",
                                "cache_read_tokens", "cached_tokens",
                                "cachedContentTokenCount"),
    "cache_write_input_tokens": ("cache_write_input_tokens", "cache_creation_input_tokens",
                                 "cacheWriteInputTokens", "cache_write_tokens"),
}

# Input fields that already include cached tokens. OpenAI's prompt_tokens and
# Gemini's promptTokenCount do; Anthropic-style input_tokens counts only the
# uncached part, so the cache fields are added to it.
INPUT_INCLUDES_CACHE = {"prompt_tokens", "promptTokenCount"}

OK_STATUSES = {"completed", "complete", "succeeded", "success", "ok", "done"}


def flatten(record: Mapping, prefix: str = "") -> Dict[str, object]:
    """{"usage": {"prompt_tokens": 5}} -> {"usage.prompt_tokens": 5, "prompt_tokens": 5}.

    Leaf names are also exposed bare (first occurrence wins) so aliases match
    regardless of nesting.
    """
    flat: Dict[str, object] = {}
    leaves: Dict[str, object] = {}

    def walk(obj: Mapping, path: str) -> None:
        for key, value in obj.items():
            full = f"{path}.{key}" if path else str(key)
            if isinstance(value, Mapping):
                walk(value, full)
            else:
                flat[full] = value
                leaves.setdefault(str(key), value)

    walk(record, prefix)
    return {**leaves, **flat}


def _number(value) -> int:
    if value in (None, ""):
        return 0
    return int(float(value))


def read_records(path: Path) -> List[dict]:
    """Raw records from a JSON, JSON Lines or CSV file."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return list(csv.DictReader(text.splitlines()))
    if suffix in (".jsonl", ".ndjson"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if isinstance(data, dict):
        # e.g. {"records": [...]} or {"data": [...]}: take the first list of objects.
        lists = [v for v in data.values()
                 if isinstance(v, list) and v and isinstance(v[0], dict)]
        data = lists[0] if lists else [data]
    if not isinstance(data, list):
        raise ValueError("expected a list of usage records")
    return data


def normalise(record: Mapping, mapping: Optional[Mapping[str, str]] = None,
              input_includes_cache: Optional[bool] = None) -> Optional[dict]:
    """One standard record, or None if it's a failed scan or has no tokens.

    `mapping` maps standard field names to source fields (dotted paths reach
    into nested objects). `input_includes_cache` overrides the alias-based
    guess about whether the input count already includes cached tokens.
    """
    flat = flatten(record)
    status = flat.get("status")
    if status and str(status).lower() not in OK_STATUSES:
        return None
    if flat.get("error"):
        return None

    out: Dict[str, object] = {}
    input_source = None
    for field in FIELDS:
        sources = [mapping[field]] if mapping and field in mapping else ALIASES[field]
        for source in sources:
            if flat.get(source) not in (None, ""):
                out[field] = flat[source]
                if field == "input_tokens":
                    input_source = source.split(".")[-1]
                break
    if not out.get("repository"):
        return None

    rec = {"repository": str(out["repository"])}
    rec.update({f: _number(out.get(f)) for f in FIELDS[1:]})
    includes = (input_includes_cache if input_includes_cache is not None
                else input_source in INPUT_INCLUDES_CACHE)
    if not includes:
        rec["input_tokens"] += rec["cache_read_input_tokens"] + rec["cache_write_input_tokens"]
    if rec["input_tokens"] <= 0:
        return None
    return rec


def load_usage(path: Path, mapping: Optional[Mapping[str, str]] = None,
               input_includes_cache: Optional[bool] = None) -> List[dict]:
    """Standard records for every successful scan in the file."""
    records = (normalise(r, mapping, input_includes_cache) for r in read_records(path)
               if isinstance(r, Mapping))
    return [r for r in records if r is not None]


def parse_mapping(pairs: Iterable[str]) -> Dict[str, str]:
    """["input_tokens=usage.in"] -> {"input_tokens": "usage.in"}."""
    mapping = {}
    for pair in pairs:
        field, sep, source = pair.partition("=")
        if not sep or field not in FIELDS or not source:
            raise ValueError(f"bad mapping {pair!r}: expected FIELD=SOURCE with FIELD one "
                             f"of {', '.join(FIELDS)}")
        mapping[field] = source
    return mapping
