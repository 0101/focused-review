# Benchmark Re-run with Local Checkouts

## Goals

Re-run the unified review system against all 6 benchmark PRs with:
1. **Local checkouts** — clone target repos and checkout PR branches so the model reads files from disk (no GitHub MCP fetch latency)
2. **1200s timeout** — the new default, giving the bugs concern enough time to complete
3. **Full 5-phase pipeline** — discovery → consolidation → assessment → presentation per PR
4. **Clean comparison** — all PRs tested under identical conditions for an apples-to-apples baseline

## Expected Behavior

For each PR:
- Clone the target repo to a working directory (e.g., `Q:\code\benchmark-repos\<repo>`)
- Checkout the PR's merge base + apply the PR diff (or checkout the PR branch)
- Run `focused-review.py prepare-review --scope branch` from within the checkout
- Run `focused-review.py run-concerns` with 1200s timeout
- Run consolidation, assessment, and presentation phases
- Classify each finding using the definitions below
- Cross-reference against human PR review threads (read via GitHub MCP tools)

### Finding Classification

- **TP (True Positive)**: A valid finding that matches something a human reviewer also caught on the PR
- **TP-Novel**: A valid finding that human reviewers did NOT catch — the tool found something humans missed
- **FP (False Positive)**: An incorrect, speculative, or unhelpful finding that does not identify a real issue

## Technical Approach

### Workspace setup per PR

```bash
# Clone once per repo (reuse across PRs from same repo)
git clone --depth=100 https://github.com/<owner>/<repo>.git Q:\code\benchmark-repos\<repo>

# For each PR: create a branch from the merge base with the PR changes
cd Q:\code\benchmark-repos\<repo>
git fetch origin pull/<pr>/head:pr-<pr>
git checkout pr-<pr>
```

### Running the pipeline

The Python script and concern files live in the plugin repo. Run with absolute paths:
```bash
cd Q:\code\benchmark-repos\<repo>
python Q:/code/fr-comprehensive/skills/focused-review/scripts/focused-review.py \
  prepare-review --repo . --scope branch \
  --concerns-dir Q:/code/fr-comprehensive/skills/focused-review/defaults/concerns

python Q:/code/fr-comprehensive/skills/focused-review/scripts/focused-review.py \
  run-concerns --repo . --timeout 1200
```

Consolidation and assessment run as Copilot CLI agents reading from `.agents/focused-review/findings/`.

### Output

Each PR produces:
- `.agents/focused-review/findings/concern--{name}--{model}.md` per concern
- `.agents/focused-review/consolidated.md`
- `.agents/focused-review/assessed.md`
- Final report

Copy results back to plugin repo under `.agents/benchmark-results/<pr>/` for comparison.

## PRs to Benchmark

| # | Repo | PR | Title | Diff Size |
|---|------|----|-------|-----------|
| 1 | dotnet/runtime | #106374 | Parallel Connect (Happy Eyeballs v2) | ~332 lines, 8 files |
| 2 | dotnet/runtime | #117148 | FileSystemWatcher.Linux inotify refactor | ~1777 lines, 6 files |
| 3 | dotnet/runtime | #123921 | Threadpool semaphore / LIFO policy | ~878 lines, 21 files |
| 4 | django/django | #20300 | Deprecate Field.get_placeholder | ~264 lines, 17 files |
| 5 | dotnet/fsharp | #19040 | Type subsumption cache | ~244 lines, 8 files |
| 6 | dotnet/fsharp | #19297 | State machine compilation | ~445 lines, 14 files |

## Decisions

- **One task per PR** — each PR is self-contained: clone/checkout → run pipeline → classify findings. Keeps tasks focused and recoverable.
- **Repos cloned to `Q:\code\benchmark-repos\`** — fully separate from the plugin repo. Three unique repos (runtime, django, fsharp) shared across PRs.
- **Use `--scope branch`** — compares PR branch against its merge base, giving the natural diff the PR author intended.
- **Results stored in `.agents/benchmark-results/<pr>/`** — preserves all artifacts for the final report.
