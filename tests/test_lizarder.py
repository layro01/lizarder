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
    assert lines[0].split()[:3] == ["Project", "Files", "Functions"]
    assert lines[2].split()[:3] == ["alpha", "2", "2"]
    assert lines[-1].startswith("TOTAL")


def test_cli_no_repos(tmp_path, capsys):
    assert main([str(tmp_path), "-q"]) == 1
    assert "no Git projects" in capsys.readouterr().err
