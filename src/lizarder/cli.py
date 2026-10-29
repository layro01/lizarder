"""Command-line entry point.

    lizarder [DIRECTORY]            complexity report (the default command)
    lizarder estimate [DIRECTORY]   estimated AI token usage per project
    lizarder calibrate USAGE DIR    fit the token model to recorded usage
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

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
from lizarder.estimate import (
    DEFAULT_FEATURES,
    ProjectEstimate,
    TokenModel,
    all_features,
    build_samples,
    estimate,
    fit,
    leave_one_out,
    load_usage,
    total_estimate,
)
from lizarder.report import FIELDS, TABLE_COLUMNS, compact, render_csv, render_json, render_table

FORMATS = ("table", "json", "csv")
METRIC_KEYS = ("files", "tracked_files", "functions", "nloc", "avg_nloc", "avg_ccn",
               "max_ccn", "avg_tokens", "avg_params", "warnings", "warning_ratio",
               "nloc_warning_ratio")
SORT_KEYS = ("path", "project") + METRIC_KEYS
ESTIMATE_SORT_KEYS = ("path", "project", "tracked_files", "functions", "input_tokens",
                      "output_tokens", "total_tokens")
ESTIMATE_FIELDS = [f for f in ProjectEstimate.__dataclass_fields__]

ESTIMATE_COLUMNS = [
    ("Project", "path", lambda r: r.path + (" *" if r.extrapolated else "")),
    ("Tracked Files", "tracked_files", "{:,}"),
    ("Functions", "functions", "{:,}"),
    ("Est. Input", "input_tokens", lambda r: compact(r.input_tokens)),
    ("Est. Output", "output_tokens", lambda r: compact(r.output_tokens)),
    ("Est. Total", "total_tokens", lambda r: compact(r.total_tokens)),
    ("Likely Range", "total_low", lambda r: f"{compact(r.total_low)} - {compact(r.total_high)}"),
]


def language_names() -> List[str]:
    return sorted({name for reader in languages() for name in reader.language_names})


# --- shared arguments ------------------------------------------------------

def add_directory_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("directory", nargs="?", default=".", type=Path,
                        help="a Git project, or a directory to search for Git projects "
                             "(default: current directory)")


def add_output_args(parser: argparse.ArgumentParser, sort_keys: Sequence[str]) -> None:
    out = parser.add_argument_group("output")
    out.add_argument("-f", "--format", choices=FORMATS,
                     help="output format (default: inferred from --output extension, else table)")
    out.add_argument("-o", "--output", type=Path,
                     help="write the report to this file instead of stdout")
    out.add_argument("-s", "--sort", choices=sort_keys, default="path",
                     help="sort projects by this column; numeric columns sort "
                          "highest first (default: path)")
    out.add_argument("-q", "--quiet", action="store_true",
                     help="don't print progress to stderr")


def add_discovery_args(parser: argparse.ArgumentParser) -> None:
    disc = parser.add_argument_group("discovery")
    disc.add_argument("--nested", action="store_true",
                      help="keep searching inside Git projects for nested repos "
                           "and submodules, reporting each separately")
    disc.add_argument("--max-depth", type=int, metavar="N",
                      help="maximum directory depth to search below DIRECTORY")
    disc.add_argument("-t", "--threads", type=int, default=1,
                      help="number of worker threads Lizard uses per project (default: 1)")


def add_version_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--version", action="version",
                        version=f"lizarder {__version__} (lizard {lizard.version})")


# --- parsers ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lizarder",
        description="Recursively find Git projects under DIRECTORY and report "
                    "Lizard code complexity metrics for each one.",
        epilog="other commands:\n"
               "  lizarder estimate [DIRECTORY]       estimate AI token usage per project\n"
               "  lizarder calibrate USAGE DIRECTORY  fit the token model to recorded usage\n"
               "run 'lizarder <command> -h' for details",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_directory_arg(parser)
    add_output_args(parser, SORT_KEYS)
    add_discovery_args(parser)

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
    add_version_arg(parser)
    return parser


def build_estimate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lizarder estimate",
        description="Estimate the AI tokens needed to scan each Git project under "
                    "DIRECTORY, from its size and Lizard metrics. Projects marked * "
                    "fall outside the range the model was trained on.",
    )
    add_directory_arg(parser)
    parser.add_argument("-m", "--model", type=Path,
                        help="model file from 'lizarder calibrate' (default: bundled model)")
    add_output_args(parser, ESTIMATE_SORT_KEYS)
    add_discovery_args(parser)
    add_version_arg(parser)
    return parser


def build_calibrate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lizarder calibrate",
        description="Fit the token model to recorded usage. Each record's 'owner/name' "
                    "repository is matched to a Git project found under DIRECTORY.",
    )
    parser.add_argument("usage", type=Path,
                        help="JSON list of scan records with repository, input_tokens "
                             "and output_tokens")
    add_directory_arg(parser)
    parser.add_argument("-o", "--output", type=Path, default=Path("token-model.json"),
                        help="where to write the model (default: %(default)s)")
    for target in ("input", "output"):
        defaults = " ".join(DEFAULT_FEATURES[f"{target}_tokens"])
        parser.add_argument(f"--{target}-feature", action="append", choices=METRIC_KEYS,
                            metavar="METRIC",
                            help=f"metric to predict {target} tokens from; repeatable "
                                 f"(default: {defaults})")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="don't print progress to stderr")
    add_discovery_args(parser)
    add_version_arg(parser)
    parser.epilog = f"metrics: {', '.join(METRIC_KEYS)}"
    return parser


# --- helpers ---------------------------------------------------------------

def resolve_format(args: argparse.Namespace) -> str:
    if args.format:
        return args.format
    if args.output:
        suffix = args.output.suffix.lower().lstrip(".")
        if suffix in ("json", "csv"):
            return suffix
    return "table"


def sort_rows(rows: list, key: str) -> list:
    if key in ("path", "project"):
        return sorted(rows, key=lambda r: getattr(r, key).lower())
    return sorted(rows, key=lambda r: (-getattr(r, key), r.path.lower()))


def make_progress(args: argparse.Namespace) -> Callable[[str], None]:
    def progress(msg: str) -> None:
        if not args.quiet:
            print(msg, file=sys.stderr, flush=True)
    return progress


def resolve_root(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Path:
    root = args.directory.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"not a directory: {args.directory}")
    return root


def scan(root: Path, args: argparse.Namespace, options: Options,
         progress: Callable[[str], None]) -> Optional[Tuple[List[ProjectMetrics], Accumulator]]:
    """Find and analyse every Git project under root. None if there are none."""
    repos = list(find_git_repos(root, nested=args.nested, max_depth=args.max_depth))
    if not repos:
        print(f"lizarder: no Git projects found under {root}", file=sys.stderr)
        return None
    progress(f"Found {len(repos)} Git project(s) under {root}")

    projects: List[ProjectMetrics] = []
    total = Accumulator(options.thresholds)
    for i, repo in enumerate(repos, 1):
        progress(f"[{i}/{len(repos)}] {display_path(repo, root)}")
        metrics, acc = analyze_repo(repo, root, options)
        projects.append(metrics)
        total.merge(acc)
    return projects, total


def emit(text: str, args: argparse.Namespace, fmt: str, progress) -> None:
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        progress(f"Wrote {fmt} report to {args.output}")
    else:
        sys.stdout.write(text)


def base_meta(root: Path) -> dict:
    return {
        "root": str(root),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lizard_version": lizard.version,
    }


# --- commands --------------------------------------------------------------

def report_main(argv: List[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = resolve_root(parser, args)
    progress = make_progress(args)

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
    result = scan(root, args, options, progress)
    if result is None:
        return 1
    projects, total_acc = result
    projects = sort_rows(projects, args.sort)
    total = total_acc.fill(ProjectMetrics(project="TOTAL", path="TOTAL"))

    fmt = resolve_format(args)
    if fmt == "json":
        total_dict = total.to_dict()
        for key in ("project", "path", "branch", "commit", "error"):
            total_dict.pop(key)
        meta = {**base_meta(root), "thresholds": options.thresholds}
        text = render_json(projects, total_dict, meta)
    elif fmt == "csv":
        text = render_csv(projects, FIELDS)
    else:
        text = render_table(projects, TABLE_COLUMNS, total)
    emit(text, args, fmt, progress)
    return 2 if any(m.error for m in projects) else 0


def estimate_main(argv: List[str]) -> int:
    parser = build_estimate_parser()
    args = parser.parse_args(argv)
    root = resolve_root(parser, args)
    progress = make_progress(args)
    try:
        model = TokenModel.load(args.model)
    except (OSError, ValueError, KeyError) as exc:
        parser.error(f"can't load model {args.model}: {exc}")

    # The model was fitted to whole repositories, so no language/exclude filtering.
    result = scan(root, args, Options(threads=max(1, args.threads)), progress)
    if result is None:
        return 1
    projects, _ = result
    estimates = sort_rows([estimate(m, model) for m in projects], args.sort)
    total = total_estimate(estimates)

    fmt = resolve_format(args)
    if fmt == "json":
        total_dict = total.to_dict()
        for key in ("project", "path", "error"):
            total_dict.pop(key)
        meta = {**base_meta(root), "model": model.summary()}
        text = render_json(estimates, total_dict, meta)
    elif fmt == "csv":
        text = render_csv(estimates, ESTIMATE_FIELDS)
    else:
        text = render_table(estimates, ESTIMATE_COLUMNS, total)
        if total.extrapolated:
            text += ("\n* outside the size range the model was trained on; "
                     "treat as a rough guess\n")
    emit(text, args, fmt, progress)
    return 2 if any(e.error for e in estimates) else 0


def calibrate_main(argv: List[str]) -> int:
    parser = build_calibrate_parser()
    args = parser.parse_args(argv)
    root = resolve_root(parser, args)
    progress = make_progress(args)
    features = {
        "input_tokens": args.input_feature or DEFAULT_FEATURES["input_tokens"],
        "output_tokens": args.output_feature or DEFAULT_FEATURES["output_tokens"],
    }

    try:
        usage = load_usage(args.usage)
    except (OSError, ValueError) as exc:
        parser.error(f"can't read usage file {args.usage}: {exc}")
    if not usage:
        parser.error(f"no completed scan records in {args.usage}")

    result = scan(root, args, Options(threads=max(1, args.threads)), progress)
    if result is None:
        return 1
    samples, unmatched = build_samples(usage, result[0], all_features(features))
    for repo in unmatched:
        print(f"lizarder: skipping {repo}: no unique matching project under {root}",
              file=sys.stderr)
    try:
        data = fit(samples, features, source=str(args.usage))
    except ValueError as exc:
        print(f"lizarder: can't fit model: {exc}", file=sys.stderr)
        return 1
    TokenModel(data).save(args.output)

    # Show how well each project is predicted when it's left out of the fit.
    samples = data["samples"]
    loo = {t: leave_one_out(samples, features[t], t) for t in data["targets"]}
    width = max(len(s["path"]) for s in samples)
    print(f"{'Project'.ljust(width)}  {'Actual':>8}  {'Predicted':>9}  {'Error':>6}")
    for i, s in enumerate(samples):
        actual = s["input_tokens"] + s["output_tokens"]
        pred = sum(loo[t][i] for t in loo)
        print(f"{s['path'].ljust(width)}  {compact(actual):>8}  {compact(pred):>9}  "
              f"{pred / actual - 1:>+6.0%}")
    print()
    for target, t in data["targets"].items():
        print(f"{target} ({' + '.join(features[target])}): median error "
              f"{t['loo_median_error']:.0%}, worst {t['loo_max_error']:.0%}")
    print(f"(leave-one-out errors over {len(samples)} projects)")
    progress(f"Wrote model to {args.output}")
    return 0


COMMANDS = {"estimate": estimate_main, "calibrate": calibrate_main}


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in COMMANDS:
        return COMMANDS[argv[0]](argv[1:])
    return report_main(argv)


if __name__ == "__main__":
    sys.exit(main())
