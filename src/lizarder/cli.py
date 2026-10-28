"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import lizard
from lizard_languages import languages

from lizarder import __version__
from lizarder.analysis import (
    DEFAULT_THRESHOLDS,
    Accumulator,
    Options,
    ProjectMetrics,
    analyze_repo,
    display_path,
)
from lizarder.discovery import find_git_repos
from lizarder.report import render_csv, render_json, render_table

FORMATS = ("table", "json", "csv")
SORT_KEYS = ("path", "project", "files", "functions", "nloc", "avg_nloc", "avg_ccn",
             "max_ccn", "avg_tokens", "avg_params", "warnings", "warning_ratio",
             "nloc_warning_ratio")


def language_names() -> List[str]:
    return sorted({name for reader in languages() for name in reader.language_names})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lizarder",
        description="Recursively find Git projects under DIRECTORY and report "
                    "Lizard code complexity metrics for each one.",
    )
    parser.add_argument("directory", nargs="?", default=".", type=Path,
                        help="directory to search for Git projects (default: current directory)")

    out = parser.add_argument_group("output")
    out.add_argument("-f", "--format", choices=FORMATS,
                     help="output format (default: inferred from --output extension, else table)")
    out.add_argument("-o", "--output", type=Path,
                     help="write the report to this file instead of stdout")
    out.add_argument("-s", "--sort", choices=SORT_KEYS, default="path",
                     help="sort projects by this column; numeric columns sort "
                          "highest first (default: path)")
    out.add_argument("-q", "--quiet", action="store_true",
                     help="don't print progress to stderr")

    disc = parser.add_argument_group("discovery")
    disc.add_argument("--nested", action="store_true",
                      help="keep searching inside Git projects for nested repos "
                           "and submodules, reporting each separately")
    disc.add_argument("--max-depth", type=int, metavar="N",
                      help="maximum directory depth to search below DIRECTORY")

    anal = parser.add_argument_group("analysis")
    anal.add_argument("-l", "--language", action="append", default=[], metavar="LANG",
                      choices=language_names(),
                      help="only analyse this language; repeatable "
                           "(e.g. -l python -l java)")
    anal.add_argument("-x", "--exclude", action="append", default=[], metavar="GLOB",
                      help="exclude files whose repo-relative path matches GLOB; "
                           "repeatable (e.g. -x 'tests/*' -x '*_pb2.py')")
    anal.add_argument("--all-files", action="store_true",
                      help="analyse every file on disk rather than only files "
                           "tracked by Git")
    anal.add_argument("-C", "--ccn", type=int, default=DEFAULT_THRESHOLDS["cyclomatic_complexity"],
                      help="CCN warning threshold (default: %(default)s)")
    anal.add_argument("-L", "--length", type=int, default=DEFAULT_THRESHOLDS["nloc"],
                      help="function length (NLOC) warning threshold (default: %(default)s)")
    anal.add_argument("-a", "--arguments", type=int, default=DEFAULT_THRESHOLDS["parameter_count"],
                      help="parameter count warning threshold (default: %(default)s)")
    anal.add_argument("-t", "--threads", type=int, default=1,
                      help="number of worker threads Lizard uses per project (default: 1)")

    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__} (lizard {lizard.version})")
    return parser


def resolve_format(args: argparse.Namespace) -> str:
    if args.format:
        return args.format
    if args.output:
        suffix = args.output.suffix.lower().lstrip(".")
        if suffix in ("json", "csv"):
            return suffix
    return "table"


def sort_projects(projects: List[ProjectMetrics], key: str) -> List[ProjectMetrics]:
    if key in ("path", "project"):
        return sorted(projects, key=lambda m: getattr(m, key).lower())
    return sorted(projects, key=lambda m: (-getattr(m, key), m.path.lower()))


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    root = args.directory.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"not a directory: {args.directory}")

    def progress(msg: str) -> None:
        if not args.quiet:
            print(msg, file=sys.stderr, flush=True)

    options = Options(
        thresholds={
            "cyclomatic_complexity": args.ccn,
            "nloc": args.length,
            "parameter_count": args.arguments,
        },
        languages=args.language,
        exclude=args.exclude,
        tracked_only=not args.all_files,
        threads=max(1, args.threads),
    )

    repos = list(find_git_repos(root, nested=args.nested, max_depth=args.max_depth))
    if not repos:
        print(f"lizarder: no Git projects found under {root}", file=sys.stderr)
        return 1
    progress(f"Found {len(repos)} Git project(s) under {root}")

    projects: List[ProjectMetrics] = []
    total_acc = Accumulator(options.thresholds)
    for i, repo in enumerate(repos, 1):
        progress(f"[{i}/{len(repos)}] {display_path(repo, root)}")
        metrics, acc = analyze_repo(repo, root, options)
        projects.append(metrics)
        total_acc.merge(acc)

    projects = sort_projects(projects, args.sort)
    total = total_acc.fill(ProjectMetrics(project="TOTAL", path="TOTAL"))

    fmt = resolve_format(args)
    if fmt == "json":
        meta = {
            "root": str(root),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "lizard_version": lizard.version,
            "thresholds": options.thresholds,
        }
        text = render_json(projects, total, meta)
    elif fmt == "csv":
        text = render_csv(projects)
    else:
        text = render_table(projects, total)

    if args.output:
        args.output.write_text(text, encoding="utf-8")
        progress(f"Wrote {fmt} report to {args.output}")
    else:
        sys.stdout.write(text)

    return 2 if any(m.error for m in projects) else 0


if __name__ == "__main__":
    sys.exit(main())
