"""Run Lizard over a Git repository and aggregate its metrics."""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence

import lizard
from lizard_languages import get_reader_for

# Lizard's own CLI defaults (see `lizard --help`).
DEFAULT_THRESHOLDS = {
    "cyclomatic_complexity": 15,
    "nloc": 1000,
    "parameter_count": 100,
}


@dataclass
class Options:
    thresholds: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    languages: Sequence[str] = ()
    exclude: Sequence[str] = ()
    tracked_only: bool = True
    threads: int = 1


# Project-size metrics reported by every command, under the same names and
# column headers, so `lizarder` and `lizarder estimate` always agree.
SHARED_FIELDS = ("files", "tracked_files", "functions", "nloc")


@dataclass
class ProjectMetrics:
    project: str
    path: str
    branch: Optional[str] = None
    commit: Optional[str] = None
    files: int = 0
    tracked_files: int = 0
    functions: int = 0
    nloc: int = 0
    avg_nloc: float = 0.0
    avg_ccn: float = 0.0
    max_ccn: int = 0
    avg_tokens: float = 0.0
    avg_params: float = 0.0
    warnings: int = 0
    warning_ratio: float = 0.0
    nloc_warning_ratio: float = 0.0
    worst_function: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class Accumulator:
    """Sums raw per-function values so averages can be derived at any level."""

    def __init__(self, thresholds: Dict[str, int]):
        self.thresholds = thresholds
        self.files = 0
        self.tracked_files = 0
        self.file_nloc = 0
        self.functions = 0
        self.fn_nloc = 0
        self.ccn = 0
        self.tokens = 0
        self.params = 0
        self.warnings = 0
        self.warning_nloc = 0
        self.max_ccn = 0
        self.worst: Optional[str] = None

    def add_file(self, info, rel_name: str) -> None:
        self.files += 1
        self.file_nloc += info.nloc
        for fn in info.function_list:
            self.functions += 1
            self.fn_nloc += fn.nloc
            self.ccn += fn.cyclomatic_complexity
            self.tokens += fn.token_count
            self.params += fn.parameter_count
            if self._is_warning(fn):
                self.warnings += 1
                self.warning_nloc += fn.nloc
            if fn.cyclomatic_complexity > self.max_ccn:
                self.max_ccn = fn.cyclomatic_complexity
                self.worst = f"{fn.name} ({rel_name}:{fn.start_line})"

    def merge(self, other: "Accumulator") -> None:
        for attr in ("files", "tracked_files", "file_nloc", "functions", "fn_nloc", "ccn",
                     "tokens", "params", "warnings", "warning_nloc"):
            setattr(self, attr, getattr(self, attr) + getattr(other, attr))
        if other.max_ccn > self.max_ccn:
            self.max_ccn, self.worst = other.max_ccn, other.worst

    def _is_warning(self, fn) -> bool:
        # Mirrors lizard_core.warning_filter, including `#lizard forgives` comments.
        violated = [m for m, limit in self.thresholds.items() if getattr(fn, m) > limit]
        forgiven = getattr(fn, "forgiven_metrics", set())
        return bool(violated) and not all(m in forgiven for m in violated)

    def fill(self, metrics: ProjectMetrics) -> ProjectMetrics:
        n = self.functions

        def avg(total: int, digits: int = 2) -> float:
            return round(total / n, digits) if n else 0.0

        metrics.files = self.files
        metrics.tracked_files = self.tracked_files
        metrics.functions = n
        metrics.nloc = self.file_nloc
        metrics.avg_nloc = avg(self.fn_nloc)
        metrics.avg_ccn = avg(self.ccn)
        metrics.max_ccn = self.max_ccn
        metrics.avg_tokens = avg(self.tokens)
        metrics.avg_params = avg(self.params)
        metrics.warnings = self.warnings
        metrics.warning_ratio = avg(self.warnings, 4)
        metrics.nloc_warning_ratio = (
            round(self.warning_nloc / self.file_nloc, 4) if self.file_nloc else 0.0
        )
        metrics.worst_function = self.worst
        return metrics


def _git(repo: Path, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def git_tracked_files(repo: Path) -> Optional[List[Path]]:
    """Files tracked by Git, or None if git is unavailable or refuses the repo.

    Tracked-only means .gitignore'd build output, vendored dependencies and
    nested repositories/submodules are skipped automatically.
    """
    out = _git(repo, "ls-files", "-z", "--cached")
    if out is None:
        return None
    return [repo / name for name in out.split("\0") if name]


def walk_files(repo: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            yield Path(dirpath) / name


def existing(paths: Iterable[Path]) -> List[Path]:
    # Tracked files may be deleted in the working tree; symlinks may dangle.
    return [p for p in paths if p.is_file()]


def source_files(repo: Path, candidates: Iterable[Path], options: Options) -> List[str]:
    wanted_langs = set(options.languages)
    selected = []
    for path in candidates:
        reader = get_reader_for(str(path))
        if reader is None:
            continue
        if wanted_langs and not wanted_langs.intersection(reader.language_names):
            continue
        rel = path.relative_to(repo).as_posix()
        if any(fnmatch(rel, pattern) for pattern in options.exclude):
            continue
        selected.append(str(path))
    return selected


def git_info(repo: Path):
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    commit = _git(repo, "rev-parse", "--short", "HEAD")
    return (branch.strip() if branch else None, commit.strip() if commit else None)


def display_path(repo: Path, root: Path) -> str:
    return repo.relative_to(root).as_posix() if repo != root else repo.name


def analyze_repo(repo: Path, root: Path, options: Options) -> "tuple[ProjectMetrics, Accumulator]":
    branch, commit = git_info(repo)
    metrics = ProjectMetrics(
        project=repo.name, path=display_path(repo, root), branch=branch, commit=commit
    )
    acc = Accumulator(options.thresholds)
    try:
        tracked = git_tracked_files(repo)
        on_disk = existing(tracked if tracked is not None else walk_files(repo))
        # Counted from Git even with --all-files: it's a repo-size measure, not analysis scope.
        acc.tracked_files = len(on_disk)
        candidates = on_disk if options.tracked_only else existing(walk_files(repo))
        files = source_files(repo, candidates, options)
        for info in lizard.analyze_files(files, threads=options.threads):
            acc.add_file(info, os.path.relpath(info.filename, repo))
    except Exception as exc:  # keep going with the other repositories
        metrics.error = f"{type(exc).__name__}: {exc}"
    return acc.fill(metrics), acc
