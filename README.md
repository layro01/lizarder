# lizarder

A command-line wrapper around [Lizard](https://github.com/terryyin/lizard). Point it at a
directory and it searches the subdirectories for Git projects, runs Lizard on each one, and
reports code complexity metrics per project as a table, JSON, or CSV.

## Install

```sh
pipx install .            # or: pip install .
```

For development:

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Requires Python 3.9+ and `git` on the `PATH`.

## Usage

```sh
lizarder ~/code                       # table on stdout
lizarder ~/code -o report.json        # format inferred from the extension
lizarder ~/code -f csv > report.csv
lizarder ~/code --sort avg_ccn        # most complex projects first
lizarder ~/code -l python -l java     # only these languages
lizarder ~/code -x 'tests/*' -x '*_pb2.py'
lizarder ~/code --nested              # also report submodules and nested repos
```

```shell
Project               Files  Functions   NLOC  Avg NLOC  Avg CCN  Max CCN  Avg Tokens  Avg Params  Warnings  Warn %
--------------------  -----  ---------  -----  --------  -------  -------  ----------  ----------  --------  ------
stockroom-api            21         25    513      10.8     1.64        4        75.4        1.28         0    0.0%
stockroom-frontend       19         73  1,141      14.0     2.15       14        48.4        0.36         0    0.0%
--------------------  -----  ---------  -----  --------  -------  -------  ----------  ----------  --------  ------
TOTAL                    40         98  1,654      13.2     2.02       14        55.3        0.59         0    0.0%
```

Progress goes to stderr (silence it with `-q`), so stdout holds only the report.

### Options

| Option | Description |
| --- | --- |
| `-f, --format {table,json,csv}` | Output format. Default: from the `--output` extension, otherwise `table`. |
| `-o, --output FILE` | Write the report to a file instead of stdout. |
| `-s, --sort COLUMN` | Sort by `path` (default), `project`, or any numeric metric (highest first). |
| `--nested` | Keep searching inside Git projects so submodules and nested repos are reported separately. |
| `--max-depth N` | Limit how deep below the directory to search. |
| `-l, --language LANG` | Only analyse this language (repeatable). |
| `-x, --exclude GLOB` | Skip files whose repo-relative path matches the glob (repeatable). |
| `--all-files` | Analyse every file on disk, not just files tracked by Git. |
| `-C, --ccn N` / `-L, --length N` / `-a, --arguments N` | Warning thresholds for cyclomatic complexity, function NLOC, and parameter count (Lizard's defaults: 15 / 1000 / 100). |
| `-t, --threads N` | Number of worker threads Lizard uses per project. |
| `-q, --quiet` | Don't print progress. |

Exit status: `0` on success, `1` if no Git projects were found, `2` if any project failed to
analyse (the report is still written, and the failing project's `error` field says what
went wrong).

## How it works

- **Discovery.** A directory counts as a Git project if it contains `.git`, either a
  directory (a normal clone) or a file (a worktree or submodule). By default the search stops
  descending at each project root. Symlinked directories are not followed.
- **File selection.** Files come from `git ls-files`, so anything matched by any
  `.gitignore` (`node_modules`, build output, virtualenvs) is skipped, along with untracked
  files and the contents of nested repos and submodules. If `git` can't read a repository,
  lizarder falls back to walking the directory, as it also does with `--all-files`.
  (Lizard's own directory walker only reads the top-level `.gitignore`, which is why
  lizarder doesn't use it.)
- **Metrics.** Each file is analysed with `lizard.analyze_files`. Function-level values are
  then summed per project and across all projects, so the `TOTAL` row is weighted by
  function, not an average of the project averages.

### Metrics

| Field | Meaning |
| --- | --- |
| `files` | Source files Lizard recognised and analysed |
| `functions` | Number of functions/methods |
| `nloc` | Total non-comment lines of code across all files |
| `avg_nloc` | Average NLOC per function |
| `avg_ccn` / `max_ccn` | Average and maximum cyclomatic complexity per function |
| `avg_tokens` | Average token count per function |
| `avg_params` | Average parameter count per function |
| `warnings` | Functions exceeding any threshold (honours `#lizard forgives` comments) |
| `warning_ratio` | `warnings / functions` (Lizard's "Fun Rt") |
| `nloc_warning_ratio` | NLOC inside warning functions / total NLOC (Lizard's "nloc Rt") |
| `worst_function` | The function with the highest CCN, as `name (file:line)` |
| `branch`, `commit` | Checked-out branch and short commit SHA |

JSON output also records the scan root, a timestamp, the Lizard version, the thresholds used,
and a `total` object. CSV output has one row per project with the same fields.
