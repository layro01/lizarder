import csv
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lizarder.analysis import Options, analyze_repo
from lizarder.cli import main
from lizarder.discovery import find_git_repos

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

SIMPLE_PY = "def add(a, b):\n    return a + b\n"

# CCN 4: one base path plus three branches.
BRANCHY_PY = """\
def classify(n):
    if n < 0:
        return "neg"
    elif n == 0:
        return "zero"
    elif n < 10:
        return "small"
    return "big"
"""


def make_repo(path: Path, files: dict, untracked: dict = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for name, content in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(content)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-q", "-m", "init"],
        check=True,
    )
    for name, content in (untracked or {}).items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(content)
    return path


@pytest.fixture
def workspace(tmp_path):
    make_repo(tmp_path / "alpha", {"a.py": SIMPLE_PY, "lib/b.py": BRANCHY_PY},
              untracked={"scratch.py": BRANCHY_PY})
    make_repo(tmp_path / "group" / "beta", {"main.js": "function f(x) { return x ? 1 : 2; }\n"})
    make_repo(tmp_path / "alpha" / "vendor" / "inner", {"c.py": SIMPLE_PY})
    (tmp_path / "not-a-repo").mkdir()
    (tmp_path / "not-a-repo" / "x.py").write_text(SIMPLE_PY)
    return tmp_path


def names(repos, root):
    return [r.relative_to(root).as_posix() for r in repos]


def test_discovery_stops_at_repo_roots_by_default(workspace):
    assert names(find_git_repos(workspace), workspace) == ["alpha", "group/beta"]


def test_discovery_nested(workspace):
    assert names(find_git_repos(workspace, nested=True), workspace) == [
        "alpha", "alpha/vendor/inner", "group/beta"]


def test_discovery_max_depth(workspace):
    assert names(find_git_repos(workspace, max_depth=1), workspace) == ["alpha"]


def test_discovery_git_file_counts_as_repo(tmp_path):
    (tmp_path / "wt").mkdir()
    (tmp_path / "wt" / ".git").write_text("gitdir: /elsewhere\n")
    assert names(find_git_repos(tmp_path), tmp_path) == ["wt"]


def test_analyze_tracked_only_skips_untracked_and_nested(workspace):
    m, _ = analyze_repo(workspace / "alpha", workspace, Options())
    assert (m.files, m.functions, m.max_ccn) == (2, 2, 4)
    assert m.avg_ccn == 2.5
    assert m.worst_function == "classify (lib/b.py:1)"
    assert m.commit and m.branch


def test_analyze_all_files_includes_untracked(workspace):
    m, _ = analyze_repo(workspace / "alpha", workspace, Options(tracked_only=False))
    # scratch.py plus the nested repo's c.py
    assert m.files == 4


def test_exclude_language_and_thresholds(workspace):
    m, _ = analyze_repo(workspace / "alpha", workspace, Options(exclude=["lib/*"]))
    assert m.files == 1

    m, _ = analyze_repo(workspace / "group" / "beta", workspace, Options(languages=["python"]))
    assert m.files == 0

    opts = Options(thresholds={"cyclomatic_complexity": 3, "nloc": 1000, "parameter_count": 100})
    m, _ = analyze_repo(workspace / "alpha", workspace, opts)
    assert (m.warnings, m.warning_ratio) == (1, 0.5)


def test_cli_json(workspace, tmp_path):
    out = tmp_path / "report.json"
    assert main([str(workspace), "-q", "-o", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert [p["path"] for p in doc["projects"]] == ["alpha", "group/beta"]
    assert doc["total"]["functions"] == 3
    assert doc["thresholds"]["cyclomatic_complexity"] == 15


def test_cli_csv_sorted(workspace, capsys):
    assert main([str(workspace), "-q", "-f", "csv", "--sort", "max_ccn"]) == 0
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert [r["path"] for r in rows] == ["alpha", "group/beta"]
    assert rows[0]["max_ccn"] == "4"


def test_cli_table(workspace, capsys):
    assert main([str(workspace / "alpha"), "-q"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:5] == ["Project", "Source", "Files", "Tracked", "Files"]
    assert lines[2].split()[:4] == ["alpha", "2", "2", "2"]
    assert lines[-1].startswith("TOTAL")


def test_cli_no_repos(tmp_path, capsys):
    assert main([str(tmp_path), "-q"]) == 1
    assert "no Git projects" in capsys.readouterr().err


# --- estimate / calibrate ----------------------------------------------------

import math

from lizarder.analysis import ProjectMetrics
from lizarder.estimate import TokenModel, fit, match_project


def synthetic_samples():
    # input = 1000 * (1+functions)^0.5 * (1+tracked)^0.8, output = 50 * (1+files)^0.6 * (1+tracked)^0.3
    out = []
    for i, (functions, files, tracked) in enumerate(
            [(0, 0, 5), (3, 1, 6), (12, 4, 14), (25, 21, 31), (73, 19, 32), (183, 31, 75),
             (745, 10, 113), (1122, 135, 487)]):
        out.append({
            "project": f"org/p{i}", "path": f"p{i}",
            "functions": functions, "files": files, "tracked_files": tracked,
            "input_tokens": 1000 * (1 + functions) ** 0.5 * (1 + tracked) ** 0.8,
            "output_tokens": 50 * (1 + files) ** 0.6 * (1 + tracked) ** 0.3,
        })
    return out


def test_fit_recovers_exact_power_law():
    data = fit(synthetic_samples())
    inp = data["targets"]["input_tokens"]
    assert math.exp(inp["intercept"]) == pytest.approx(1000, rel=1e-6)
    assert inp["coefficients"] == pytest.approx({"functions": 0.5, "tracked_files": 0.8})
    assert inp["loo_max_error"] == pytest.approx(0, abs=1e-6)
    out = data["targets"]["output_tokens"]
    assert out["coefficients"] == pytest.approx({"files": 0.6, "tracked_files": 0.3})

    model = TokenModel(data)
    pred = model.predict({"functions": 99, "files": 9, "tracked_files": 9})
    assert pred["input_tokens"][0] == pytest.approx(1000 * 10 * 10 ** 0.8)
    assert not model.is_extrapolation({"functions": 99, "files": 9, "tracked_files": 9})
    assert model.is_extrapolation({"functions": 5000, "files": 9, "tracked_files": 9})


def test_fit_needs_enough_samples():
    with pytest.raises(ValueError, match="need at least"):
        fit(synthetic_samples()[:4])


def test_match_project_prefers_owner_then_unique_name():
    projects = [ProjectMetrics(project=p.split("/")[-1], path=p) for p in
                ["layro01/verademo-java", "layro01-org/verademo-java",
                 "layro01-org/polyrepo-app/stockroom-api"]]
    assert match_project("layro01-org/verademo-java", projects).path == "layro01-org/verademo-java"
    assert match_project("layro01-org/stockroom-api", projects).path.endswith("stockroom-api")
    assert match_project("someone/verademo-java", projects) is None  # ambiguous
    assert match_project("x/missing", projects) is None


def test_bundled_model_loads():
    model = TokenModel.load()
    est = model.predict({"functions": 10, "files": 5, "tracked_files": 16})
    assert est["input_tokens"][1] < est["input_tokens"][0] < est["input_tokens"][2]


def test_cli_estimate(workspace, capsys):
    assert main(["estimate", str(workspace), "-q", "-f", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    alpha = doc["projects"][0]
    assert alpha["path"] == "alpha" and alpha["tracked_files"] == 2 and alpha["functions"] == 2
    assert alpha["total_low"] < alpha["total_tokens"] < alpha["total_high"]
    assert doc["total"]["total_tokens"] == sum(p["total_tokens"] for p in doc["projects"])

    assert main(["estimate", str(workspace / "group" / "beta"), "-q"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:5] == ["Project", "Source", "Files", "Tracked", "Files"]
    assert lines[2].startswith("beta")


def test_cli_calibrate_then_estimate(tmp_path, capsys):
    root = tmp_path / "repos"
    usage = []
    for i, n in enumerate([1, 2, 4, 8, 16, 32]):
        files = {f"m{j}.py": SIMPLE_PY * (j + 1) for j in range(n)}
        files.update({f"doc{j}.md": "notes\n" for j in range(i % 3 + 1)})
        make_repo(root / "org" / f"r{i}", files)
        usage.append({"repository": f"org/r{i}", "status": "completed", "error": None,
                      "input_tokens": 5000 * n + 30 * n * n, "output_tokens": 100 * n + 7})
    usage.append({"repository": "org/gone", "status": "completed", "input_tokens": 1,
                  "output_tokens": 1})
    usage.append({"repository": "org/r0", "status": "failed", "input_tokens": 1})
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps(usage))
    model_file = tmp_path / "model.json"

    assert main(["calibrate", str(usage_file), str(root), "-q", "-o", str(model_file)]) == 0
    captured = capsys.readouterr()
    assert "skipping org/gone" in captured.err
    assert "leave-one-out" in captured.out
    data = json.loads(model_file.read_text())
    assert len(data["samples"]) == 6

    assert main(["estimate", str(root / "org" / "r3"), "-q", "-m", str(model_file),
                 "-f", "csv"]) == 0
    row = next(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert int(row["input_tokens"]) == pytest.approx(5000 * 8 + 30 * 64, rel=0.25)


# --- report and estimate must agree -------------------------------------------

import re

from lizarder.analysis import SHARED_FIELDS
from lizarder.report import ESTIMATE_COLUMNS, SHARED_COLUMNS, TABLE_COLUMNS


def table_rows(text):
    """Parse a rendered table into {project: {header: cell}}, including TOTAL."""
    lines = [l for l in text.splitlines() if l and not l.startswith("-")]
    headers = re.split(r"\s{2,}", lines[0].strip())
    rows = {}
    for line in lines[1:]:
        if line.startswith("*") or line.startswith("error in"):
            break
        cells = re.split(r"\s{2,}", line.strip())
        rows[cells[0].rstrip(" *")] = dict(zip(headers, cells))
    return headers, rows


def test_shared_columns_cover_shared_fields():
    assert [attr for _, attr, _ in SHARED_COLUMNS] == list(SHARED_FIELDS)
    for columns in (TABLE_COLUMNS, ESTIMATE_COLUMNS):
        assert columns[1:1 + len(SHARED_COLUMNS)] == SHARED_COLUMNS


@pytest.mark.parametrize("flags", [[], ["-x", "lib/*"], ["-l", "javascript"], ["--all-files"],
                                   ["--nested"]])
def test_report_and_estimate_agree(workspace, capsys, flags):
    target = [str(workspace), "-q", *flags]

    assert main([*target, "-f", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert main(["estimate", *target, "-f", "json"]) == 0
    est = json.loads(capsys.readouterr().out)
    assert [p["path"] for p in report["projects"]] == [p["path"] for p in est["projects"]]
    for r, e in zip(report["projects"], est["projects"]):
        assert {f: r[f] for f in SHARED_FIELDS} == {f: e[f] for f in SHARED_FIELDS}
    assert {f: report["total"][f] for f in SHARED_FIELDS} == \
           {f: est["total"][f] for f in SHARED_FIELDS}

    assert main([*target, "-f", "csv"]) == 0
    report_csv = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert main(["estimate", *target, "-f", "csv"]) == 0
    est_csv = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert [{f: r[f] for f in SHARED_FIELDS} for r in report_csv] == \
           [{f: r[f] for f in SHARED_FIELDS} for r in est_csv]

    assert main(target) == 0
    report_headers, report_rows = table_rows(capsys.readouterr().out)
    assert main(["estimate", *target]) == 0
    est_headers, est_rows = table_rows(capsys.readouterr().out)
    shared = [h for h, _, _ in SHARED_COLUMNS]
    assert report_headers[1:5] == est_headers[1:5] == shared
    assert report_rows.keys() == est_rows.keys()
    for name in report_rows:
        assert [report_rows[name][h] for h in shared] == [est_rows[name][h] for h in shared]


# --- usage formats and pricing -------------------------------------------------

from lizarder.pricing import Price, PriceList
from lizarder.usage import load_usage, normalise, parse_mapping


def test_normalise_provider_formats():
    # This platform / Anthropic style: input_tokens excludes cache, so cache is added.
    assert normalise({"repository": "o/a", "status": "completed", "input_tokens": 100,
                      "output_tokens": 5, "cache_read_input_tokens": 50,
                      "cache_write_input_tokens": 10}) == {
        "repository": "o/a", "input_tokens": 160, "output_tokens": 5,
        "cache_read_input_tokens": 50, "cache_write_input_tokens": 10}
    # OpenAI style, nested: prompt_tokens already includes cached_tokens.
    openai = normalise({"repo": "o/b", "usage": {
        "prompt_tokens": 100, "completion_tokens": 7,
        "prompt_tokens_details": {"cached_tokens": 40}}})
    assert (openai["input_tokens"], openai["output_tokens"],
            openai["cache_read_input_tokens"]) == (100, 7, 40)
    # Gemini style.
    gemini = normalise({"project": "o/c", "usageMetadata": {
        "promptTokenCount": 90, "candidatesTokenCount": 9, "cachedContentTokenCount": 30}})
    assert (gemini["input_tokens"], gemini["cache_read_input_tokens"]) == (90, 30)
    # Failed scans and records without tokens are dropped.
    assert normalise({"repository": "o/d", "status": "failed", "input_tokens": 5}) is None
    assert normalise({"repository": "o/d", "error": "boom", "input_tokens": 5}) is None
    assert normalise({"repository": "o/d", "input_tokens": 0}) is None


def test_normalise_explicit_mapping_and_cache_override():
    rec = {"slug": "o/e", "stats": {"in": "200", "out": "20", "hit": "50"}}
    mapping = parse_mapping(["repository=slug", "input_tokens=stats.in",
                             "output_tokens=stats.out", "cache_read_input_tokens=stats.hit"])
    assert normalise(rec, mapping)["input_tokens"] == 250  # cache added by default
    assert normalise(rec, mapping, input_includes_cache=True)["input_tokens"] == 200
    with pytest.raises(ValueError, match="bad mapping"):
        parse_mapping(["tokens=x"])


def test_load_usage_csv_jsonl_and_wrapped_json(tmp_path):
    (tmp_path / "u.csv").write_text("repo,prompt_tokens,completion_tokens,status\n"
                                    "o/a,100,10,completed\no/b,5,1,failed\n")
    (tmp_path / "u.jsonl").write_text('{"repository": "o/a", "input_tokens": 3, '
                                      '"output_tokens": 1}\n\n')
    (tmp_path / "u.json").write_text(json.dumps({"meta": 1, "data": [
        {"repository": "o/a", "input_tokens": 3, "output_tokens": 1}]}))
    assert [r["repository"] for r in load_usage(tmp_path / "u.csv")] == ["o/a"]
    assert load_usage(tmp_path / "u.jsonl")[0]["input_tokens"] == 3
    assert len(load_usage(tmp_path / "u.json")) == 1


def test_price_cost_with_cache_shares():
    p = Price("x", input_per_million=4.0, output_per_million=20.0, cache_read_per_million=0.4)
    assert p.cost(1_000_000, 100_000) == pytest.approx(4.0 + 2.0)
    # 50% of input read from cache at a tenth of the price; no write rate -> input rate.
    assert p.cost(1_000_000, 0, cache_read_share=0.5) == pytest.approx(2.0 + 0.2)
    assert p.cost(1_000_000, 0, cache_write_share=0.5) == pytest.approx(4.0)


def test_bundled_prices_and_validation(tmp_path):
    prices = PriceList.load()
    assert prices.prices["claude-opus-5-5"].input_per_million == 4.0
    assert [p.name for p in prices.select(["all"])] == list(prices.prices)
    with pytest.raises(ValueError, match="unknown price"):
        prices.select(["nope"])
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"prices": {"x": {"input_per_million": 1}}}))
    with pytest.raises(ValueError, match="output_per_million"):
        PriceList.load(bad)


def test_cli_estimate_costs(workspace, tmp_path, capsys):
    pricing = tmp_path / "prices.json"
    pricing.write_text(json.dumps({"currency": "USD", "prices": {
        "acme-large": {"provider": "acme", "input_per_million": 1.0, "output_per_million": 2.0},
        "acme-small": {"provider": "acme", "input_per_million": 0.5, "output_per_million": 1.0},
    }}))
    base = ["estimate", str(workspace), "-q", "--pricing", str(pricing)]

    assert main([*base, "--list-prices"]) == 0
    assert "acme-large" in capsys.readouterr().out

    assert main([*base, "-p", "all", "-f", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    p = doc["projects"][0]
    expected = (p["input_tokens"] * 1.0 + p["output_tokens"] * 2.0) / 1e6
    assert p["costs"]["acme-large"]["cost"] == pytest.approx(expected, abs=1e-4)
    assert p["costs"]["acme-large"]["low"] < p["costs"]["acme-large"]["cost"] \
        < p["costs"]["acme-large"]["high"]
    assert doc["total"]["costs"]["acme-small"]["cost"] == pytest.approx(
        sum(q["costs"]["acme-small"]["cost"] for q in doc["projects"]), abs=1e-3)
    assert set(doc["pricing"]["prices"]) == {"acme-large", "acme-small"}

    assert main([*base, "-p", "acme-small", "-f", "csv"]) == 0
    row = next(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert {"cost_acme-small", "cost_acme-small_low", "cost_acme-small_high"} <= row.keys()
    assert "costs" not in row

    assert main([*base, "-p", "acme-large"]) == 0
    assert "Cost (acme-large)" in capsys.readouterr().out.splitlines()[0]


def test_cli_calibrate_openai_style_usage(tmp_path, capsys):
    root = tmp_path / "repos"
    rows = ["repo_slug,prompt_tokens,completion_tokens,cached"]
    for i, n in enumerate([1, 2, 4, 8, 16, 32]):
        files = {f"m{j}.py": SIMPLE_PY * (j + 1) for j in range(n)}
        files.update({f"doc{j}.md": "notes\n" for j in range(i % 3 + 1)})
        make_repo(root / "org" / f"r{i}", files)
        total_in = 5000 * n + 30 * n * n
        rows.append(f"org/r{i},{total_in},{100 * n + 7},{total_in // 4}")
    usage = tmp_path / "openai-usage.csv"
    usage.write_text("\n".join(rows) + "\n")
    model_file = tmp_path / "model.json"

    assert main(["calibrate", str(usage), str(root), "-q", "-o", str(model_file),
                 "--map", "repository=repo_slug", "--map", "cache_read_input_tokens=cached"]) == 0
    capsys.readouterr()
    data = json.loads(model_file.read_text())
    assert len(data["samples"]) == 6
    # prompt_tokens includes cached tokens, so a quarter of input was cache reads.
    assert data["cache_read_share"] == pytest.approx(0.25, abs=0.01)
