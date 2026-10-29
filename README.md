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
Project                 Source Files  Tracked Files  Functions   NLOC  Avg NLOC  Avg CCN  Max CCN  Avg Code Tokens  Avg Params  Warnings  Warn %
----------------------  ------------  -------------  ---------  -----  --------  -------  -------  ---------------  ----------  --------  ------
stockroom-api                     21             31         25    513      10.8     1.64        4             75.4        1.28         0    0.0%
stockroom-deployment               0             26          0      0       0.0     0.00        0              0.0        0.00         0    0.0%
stockroom-frontend                19             32         73  1,141      14.0     2.15       14             48.4        0.36         0    0.0%
stockroom-polyrepo-app             0              6          0      0       0.0     0.00        0              0.0        0.00         0    0.0%
----------------------  ------------  -------------  ---------  -----  --------  -------  -------  ---------------  ----------  --------  ------
TOTAL                             40             95         98  1,654      13.2     2.02       14             55.3        0.59         0    0.0%
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
| `-l, --language LANG` | Only analyse this language (repeatable). Affects Source Files, Functions and NLOC. |
| `-x, --exclude GLOB` | Skip files whose repo-relative path matches the glob (repeatable). |
| `--all-files` | Analyse every file on disk, not just files tracked by Git. Tracked Files is still counted from Git. |
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
| `files` | **Source Files**: files Lizard recognised as source code and analysed |
| `tracked_files` | **Tracked Files**: every file tracked by Git, source or not (YAML, docs, images...) |
| `functions` | Number of functions/methods |
| `nloc` | Total non-comment lines of code across all source files |
| `avg_nloc` | Average NLOC per function |
| `avg_ccn` / `max_ccn` | Average and maximum cyclomatic complexity per function |
| `avg_tokens` | **Avg Code Tokens**: Lizard's average count of code tokens per function (unrelated to AI tokens) |
| `avg_params` | Average parameter count per function |
| `warnings` | Functions exceeding any threshold (honours `#lizard forgives` comments) |
| `warning_ratio` | `warnings / functions` (Lizard's "Fun Rt") |
| `nloc_warning_ratio` | NLOC inside warning functions / total NLOC (Lizard's "nloc Rt") |
| `worst_function` | The function with the highest CCN, as `name (file:line)` |
| `branch`, `commit` | Checked-out branch and short commit SHA |

JSON output also records the scan root, a timestamp, the Lizard version, the thresholds used,
and a `total` object. CSV output has one row per project with the same fields.

## Estimating AI token usage

`lizarder estimate` predicts how many tokens an AI platform will use to scan each project. It
finds projects the same way as the main command, so `PATH` can be a single project or a
directory of them.

```sh
lizarder estimate ~/code/my-service
lizarder estimate ~/code --sort total_tokens
lizarder estimate ~/code -o estimate.csv
lizarder estimate ~/code -m my-model.json      # use your own calibration
lizarder estimate ~/code -p claude-sonnet-5-5   # add a cost column
```

```shell
Project                 Source Files  Tracked Files  Functions   NLOC  Est. Input  Est. Output  Est. Total     Likely Range
----------------------  ------------  -------------  ---------  -----  ----------  -----------  ----------  ---------------
stockroom-api                     21             31         25    513      482.4k        15.3k      497.8k  421.7k - 591.1k
stockroom-deployment               0             26          0      0      235.7k         3.3k      239.0k  203.7k - 281.2k
stockroom-frontend                19             32         73  1,141      596.1k        14.8k      610.9k  518.8k - 722.7k
stockroom-polyrepo-app             0              6          0      0       82.4k         2.2k       84.6k   71.8k - 100.2k
----------------------  ------------  -------------  ---------  -----  ----------  -----------  ----------  ---------------
TOTAL                             40             95         98  1,654       1.40M        35.7k       1.43M    1.22M - 1.70M
```

The first four metric columns are the same as the complexity report's: same names, same
values. The two commands accept the same discovery and file-selection options (`--nested`,
`--max-depth`, `-l`, `-x`, `--all-files`, `-t`), so the same flags always give the same
counts. A test enforces this. `estimate` also takes `-m, --model FILE` and the cost options (`-p`, `--pricing`,
`--list-prices`, see [Estimating cost](#estimating-cost)), and sorts by
`input_tokens`, `output_tokens` or `total_tokens` as well as the shared metrics. The bundled
model was trained with no file-selection flags, so filtering changes what the estimate
measures; for filtered estimates, calibrate with the same flags.

### The model

The estimate is a log-log regression fitted to recorded scans:

```text
ln(input_tokens)  = a + b * ln(1 + functions) + c * ln(1 + tracked_files)
ln(output_tokens) = d + e * ln(1 + files)     + f * ln(1 + tracked_files)
```

The bundled model ([src/lizarder/token_model.json](src/lizarder/token_model.json)) was fitted
to 11 scanned repositories. Each repo was predicted from a model fitted to the other 10
(leave-one-out cross-validation):

- **Input tokens** (about 98% of the total): median error 16%, worst 25%.
- **Output tokens**: median error about 2×. The AI-focused repos produced roughly twice the
  output of the traditional ones, and none of the metrics capture that.
- **Total**: every repo was within ±20%.

`Likely Range` is the estimate × / ÷ the root-mean-square leave-one-out error, roughly a
one-standard-deviation band. A project marked `*` lies outside the size range of the training
data, so its estimate is an extrapolation.

Lizard's complexity metrics on their own predict poorly (up to 5× off), because the platform
reads every file, not just source code. A repo with no source files still cost about 240k
tokens. The count of tracked files carries most of the signal, and Lizard's function count
adds the rest.

### Calibrating with your own data

As you collect more scan results, refit the model:

```sh
lizarder calibrate token-usage.json ~/code -o my-model.json
lizarder estimate ~/code -m my-model.json
```

Each usage record is matched to the project under `~/code` whose path ends with its
`owner/name`, falling back to a unique match on `name`. `calibrate` prints the leave-one-out
error for every project so you can see how well the fit holds up. Use `--input-feature` /
`--output-feature` (repeatable) to try other metrics.

Metrics should come from the same commit that was scanned. If a local checkout has moved on,
its sample will be slightly off.

### Usage file format

`calibrate` reads JSON (a list, or an object containing a list), JSON Lines (`.jsonl`), or
CSV. Every record is normalised to one standard shape:

| Field | Meaning |
| --- | --- |
| `repository` | `owner/name` (or `name`) of the scanned repo |
| `input_tokens` | All input tokens processed, **including** cached ones |
| `output_tokens` | Output tokens |
| `cache_read_input_tokens` | The part of the input read from a prompt cache (optional) |
| `cache_write_input_tokens` | The part of the input written to a prompt cache (optional) |

Common field names from different systems are recognised automatically, including inside
nested objects such as `usage` or `usageMetadata`:

| Standard field | Also accepted |
| --- | --- |
| `repository` | `repo`, `repo_name`, `full_name`, `project` |
| `input_tokens` | `inputTokens`, `prompt_tokens` (OpenAI), `promptTokenCount` (Gemini), `input` |
| `output_tokens` | `outputTokens`, `completion_tokens` (OpenAI), `candidatesTokenCount` (Gemini), `output` |
| `cache_read_input_tokens` | `cacheReadInputTokens`, `cache_read_tokens`, `cached_tokens` (OpenAI), `cachedContentTokenCount` (Gemini) |
| `cache_write_input_tokens` | `cache_creation_input_tokens` (Anthropic), `cacheWriteInputTokens`, `cache_write_tokens` |

Records with a `status` other than `completed`/`succeeded`/`success`/`ok`/`done`, or with an
`error`, are skipped.

Providers disagree on whether the input count includes cached tokens. OpenAI's
`prompt_tokens` and Gemini's `promptTokenCount` include them. Anthropic-style
`input_tokens` doesn't, so lizarder adds the cache counts to it. Override the guess with
`--input-includes-cache yes|no`.

For anything else, map fields explicitly. Dotted paths reach into nested objects:

```sh
lizarder calibrate scans.csv ~/code -o acme-model.json \
  --map repository=repo_slug --map input_tokens=stats.in --map output_tokens=stats.out
```

## Estimating cost

Add one or more cost columns with `-p/--price`:

```sh
lizarder estimate --list-prices                    # what's in the pricing file
lizarder estimate ~/code -p claude-sonnet-5-5
lizarder estimate ~/code -p claude-opus-5-5 -p claude-haiku-5-5
lizarder estimate ~/code -p all -o costs.json
```

The table shows the point estimate. JSON (`costs`) and CSV (`cost_<name>`,
`cost_<name>_low`, `cost_<name>_high`) also carry the range. JSON output records the exact
prices used.

The bundled [pricing file](src/lizarder/pricing.json) has Claude API list prices as of
2026-10-06. Check the [pricing page](https://platform.claude.com/docs/en/about-claude/pricing)
for changes. Claude Haiku 5.5's listed price applies to prompts up to 100K tokens; agentic
scans with longer prompts cost more than the estimate shows.

### Adding other providers

Write your own pricing file and pass it with `--pricing`. Rates are per million tokens, and
the cache rates are optional:

```json
{
  "currency": "USD",
  "prices": {
    "acme-large": {
      "provider": "acme",
      "input_per_million": 3.0,
      "output_per_million": 12.0,
      "cache_read_per_million": 0.3,
      "source": "https://acme.example/pricing",
      "as_of": "2026-10-29"
    }
  }
}
```

```sh
lizarder estimate ~/code --pricing my-prices.json -p acme-large
```

Cached input is billed at the cache rates in proportion to how much caching the token
model's training scans used (`cache_read_share` / `cache_write_share` in the model file). A
missing cache rate falls back to the input rate. The bundled model's training scans used no
caching, so it prices all input at the full input rate.

### Token volume differs by provider

A price list changes the rate, not the token count. The bundled token model was fitted to one
platform's scans, and other providers can use noticeably different token counts for the same
repo: tokenizers differ, and so do the agent's prompts, tool calls, and how much it reads.
Applying another provider's prices to the bundled model answers "what would these tokens cost
at that rate?", not "what would that provider's scan cost?".

For a real estimate of another provider, scan a handful of repos with it and calibrate a
model from its usage export:

```sh
lizarder calibrate acme-usage.jsonl ~/code -o acme-model.json
lizarder estimate ~/code -m acme-model.json --pricing my-prices.json -p acme-large
```

`-m` sets whose token counts to use and `--price` sets whose rates, so the two choices stay
independent.
