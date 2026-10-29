"""Estimate AI token usage for a project from its size and complexity metrics.

The model is a log-log linear regression fitted to past scans:

    ln(tokens) = intercept + sum(coef_f * ln(1 + feature_f))

fitted separately for input and output tokens, each with its own features
(numeric ProjectMetrics fields). The spread of the leave-one-out residuals
gives the uncertainty range.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from lizarder.analysis import SHARED_FIELDS, ProjectMetrics
from lizarder.pricing import Price

TARGETS = ("input_tokens", "output_tokens")
# Chosen by leave-one-out error on the bundled training data. Output tokens
# follow source file count more closely than function count.
DEFAULT_FEATURES: Dict[str, Tuple[str, ...]] = {
    "input_tokens": ("functions", "tracked_files"),
    "output_tokens": ("files", "tracked_files"),
}
BUNDLED_MODEL = Path(__file__).with_name("token_model.json")


@dataclass
class ProjectEstimate:
    project: str
    path: str
    # Same values as the complexity report's SHARED_FIELDS.
    files: int = 0
    tracked_files: int = 0
    functions: int = 0
    nloc: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    total_low: int = 0
    total_high: int = 0
    extrapolated: bool = False
    error: Optional[str] = None
    # {price name: {"cost", "low", "high"}}, one per selected price.
    costs: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_row(self) -> dict:
        """Flat form for CSV: costs become cost_<name>, cost_<name>_low/_high."""
        row = asdict(self)
        for name, c in row.pop("costs").items():
            row[f"cost_{name}"] = c["cost"]
            row[f"cost_{name}_low"] = c["low"]
            row[f"cost_{name}_high"] = c["high"]
        return row


def _row(features: Sequence[str], values: dict) -> List[float]:
    return [1.0] + [math.log1p(values[f]) for f in features]


def _least_squares(X: List[List[float]], y: List[float]) -> List[float]:
    """Solve the normal equations (X'X)b = X'y by Gauss-Jordan elimination."""
    n = len(X[0])
    a = [[sum(r[i] * r[j] for r in X) for j in range(n)] + [sum(r[i] * t for r, t in zip(X, y))]
         for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("features are collinear or constant across the samples")
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(n):
            if r != col:
                f = a[r][col] / a[col][col]
                a[r] = [x - f * p for x, p in zip(a[r], a[col])]
    return [a[i][n] / a[i][i] for i in range(n)]


def _predict(coefs: List[float], row: List[float]) -> float:
    return math.exp(sum(c * v for c, v in zip(coefs, row)))


def leave_one_out(samples: List[dict], features: Sequence[str], target: str) -> List[float]:
    """Predict each sample from a model fitted to all the others."""
    preds = []
    for i, held_out in enumerate(samples):
        rest = [s for j, s in enumerate(samples) if j != i]
        coefs = _least_squares([_row(features, s) for s in rest],
                               [math.log(s[target]) for s in rest])
        preds.append(_predict(coefs, _row(features, held_out)))
    return preds


def all_features(features: Dict[str, Sequence[str]]) -> List[str]:
    return sorted({f for fs in features.values() for f in fs})


def fit(samples: List[dict], features: Optional[Dict[str, Sequence[str]]] = None,
        source: Optional[str] = None) -> dict:
    """Fit a model to samples of {"project", <features>, "input_tokens", "output_tokens"}.

    `features` maps each target to the metrics it is regressed on.
    """
    features = {t: list(fs) for t, fs in (features or DEFAULT_FEATURES).items()}
    samples = [s for s in samples if all(s[t] > 0 for t in TARGETS)]
    most = max(len(fs) for fs in features.values())
    if len(samples) < most + 3:
        raise ValueError(
            f"need at least {most + 3} samples with non-zero token counts "
            f"to fit {most} feature(s), got {len(samples)}")

    targets = {}
    for target in TARGETS:
        fs = features[target]
        try:
            coefs = _least_squares([_row(fs, s) for s in samples],
                                   [math.log(s[target]) for s in samples])
            loo = leave_one_out(samples, fs, target)
        except ValueError as exc:
            raise ValueError(f"{target} ({', '.join(fs)}): {exc}") from None
        errs = sorted(abs(math.log(p / s[target])) for p, s in zip(loo, samples))
        targets[target] = {
            "intercept": coefs[0],
            "coefficients": dict(zip(fs, coefs[1:])),
            # RMS of the leave-one-out log residuals; est * exp(+/-) gives the range.
            "loo_rmse_log": math.sqrt(sum(e * e for e in errs) / len(errs)),
            "loo_median_error": math.exp(errs[len(errs) // 2]) - 1,
            "loo_max_error": math.exp(errs[-1]) - 1,
        }
    total_input = sum(s["input_tokens"] for s in samples)
    return {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "targets": targets,
        # Share of input tokens served from / written to a prompt cache in the
        # training scans, so cost estimates can price cached input separately.
        "cache_read_share": sum(s.get("cache_read_input_tokens", 0) for s in samples)
                            / total_input,
        "cache_write_share": sum(s.get("cache_write_input_tokens", 0) for s in samples)
                             / total_input,
        "training_range": {f: [min(s[f] for s in samples), max(s[f] for s in samples)]
                           for f in all_features(features)},
        "samples": samples,
    }


class TokenModel:
    def __init__(self, data: dict, path: Optional[Path] = None):
        self.data = data
        self.path = path
        self.features = {t: list(v["coefficients"]) for t, v in data["targets"].items()}

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "TokenModel":
        path = path or BUNDLED_MODEL
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), path)

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")

    def predict(self, values: dict) -> Dict[str, Tuple[float, float, float]]:
        """Return {target: (estimate, low, high)}."""
        out = {}
        for target, t in self.data["targets"].items():
            fs = self.features[target]
            coefs = [t["intercept"]] + [t["coefficients"][f] for f in fs]
            est = _predict(coefs, _row(fs, values))
            spread = math.exp(t["loo_rmse_log"])
            out[target] = (est, est / spread, est * spread)
        return out

    @property
    def cache_shares(self) -> Tuple[float, float]:
        return self.data.get("cache_read_share", 0.0), self.data.get("cache_write_share", 0.0)

    def is_extrapolation(self, values: dict) -> bool:
        return any(not lo <= values[f] <= hi
                   for f, (lo, hi) in self.data["training_range"].items())

    def summary(self) -> dict:
        return {
            "path": str(self.path) if self.path else None,
            "features": self.features,
            "samples": len(self.data["samples"]),
            "created": self.data.get("created"),
            "cache_read_share": round(self.cache_shares[0], 4),
            "cache_write_share": round(self.cache_shares[1], 4),
            "loo_median_error": {t: round(v["loo_median_error"], 4)
                                 for t, v in self.data["targets"].items()},
        }


TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "total_low", "total_high")


def estimate(metrics: ProjectMetrics, model: TokenModel,
             prices: Sequence[Price] = ()) -> ProjectEstimate:
    est = ProjectEstimate(project=metrics.project, path=metrics.path, error=metrics.error,
                          **{f: getattr(metrics, f) for f in SHARED_FIELDS})
    if metrics.error:
        return est
    values = asdict(metrics)
    pred = model.predict(values)
    est.input_tokens = round(pred["input_tokens"][0])
    est.output_tokens = round(pred["output_tokens"][0])
    est.total_tokens = est.input_tokens + est.output_tokens
    est.total_low = round(sum(p[1] for p in pred.values()))
    est.total_high = round(sum(p[2] for p in pred.values()))
    est.extrapolated = model.is_extrapolation(values)
    shares = model.cache_shares
    (inp, in_lo, in_hi), (out, out_lo, out_hi) = pred["input_tokens"], pred["output_tokens"]
    for price in prices:
        est.costs[price.name] = {
            "cost": round(price.cost(inp, out, *shares), 4),
            "low": round(price.cost(in_lo, out_lo, *shares), 4),
            "high": round(price.cost(in_hi, out_hi, *shares), 4),
        }
    return est


def total_estimate(estimates: Sequence[ProjectEstimate]) -> ProjectEstimate:
    total = ProjectEstimate(project="TOTAL", path="TOTAL")
    for e in estimates:
        for attr in SHARED_FIELDS + TOKEN_FIELDS:
            setattr(total, attr, getattr(total, attr) + getattr(e, attr))
        for name, c in e.costs.items():
            t = total.costs.setdefault(name, {"cost": 0.0, "low": 0.0, "high": 0.0})
            for k in t:
                t[k] = round(t[k] + c[k], 4)
    total.extrapolated = any(e.extrapolated for e in estimates)
    return total


# --- calibration from recorded usage ---------------------------------------

def match_project(repository: str, projects: Sequence[ProjectMetrics]) -> Optional[ProjectMetrics]:
    """Find the scanned project for a usage record's `owner/name` repository.

    Prefers a project whose path ends with the full `owner/name`, falling back
    to a unique match on the repository name alone.
    """
    want = repository.lower().strip("/").split("/")
    by_suffix = [p for p in projects if p.path.lower().split("/")[-len(want):] == want]
    if len(by_suffix) == 1:
        return by_suffix[0]
    by_name = [p for p in projects if p.path.lower().split("/")[-1] == want[-1]]
    return by_name[0] if len(by_name) == 1 else None


def build_samples(usage: Sequence[dict], projects: Sequence[ProjectMetrics],
                  features: Sequence[str]) -> Tuple[List[dict], List[str]]:
    """Join usage records to scanned projects. Returns (samples, unmatched repositories)."""
    samples, unmatched = [], []
    for record in usage:
        project = match_project(record["repository"], projects)
        if project is None or project.error:
            unmatched.append(record["repository"])
            continue
        sample = {"project": record["repository"], "path": project.path}
        sample.update({f: getattr(project, f) for f in features})
        sample.update({t: int(record.get(t) or 0) for t in TARGETS})
        for cache in ("cache_read_input_tokens", "cache_write_input_tokens"):
            sample[cache] = int(record.get(cache) or 0)
        samples.append(sample)
    return samples, unmatched
