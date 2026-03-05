# Benchmark Round 4: Expanded Discovery + Full SKILL.md Rules + Copilot Baseline

## Goals

1. Add agent-assisted discovery to the SKILL.md refresh flow so it finds relevant code review guidance beyond the hardcoded glob patterns (e.g. `.github/skills/code-review/SKILL.md`)
2. Regenerate rules from the **runtime repo's code-review SKILL.md** (685 lines of maintainer-extracted review guidance) — the primary source material that round 3 completely missed
3. Re-run all 8 benchmark PRs with the new rules and produce a comprehensive findings table
4. Measure whether rules extracted from the full SKILL.md improve recall (catch more of the 13 "notable misses" from round 3) without degrading precision (FP rate stays <= 25%)
5. **Run Copilot CLI with the code-review skill** against the same 8 PRs as a controlled baseline, using the same TP/FP classification methodology — head-to-head comparison to demonstrate whether focused-review outperforms stock Copilot

## Context

Round 3 generated only 6 rules because the Python `discover` script uses hardcoded glob patterns that don't search `.github/skills/`. The runtime repo's main source of review guidance is `.github/skills/code-review/SKILL.md` — a 685-line file extracted from 43,000+ maintainer review comments covering thread safety, security, API design, correctness patterns, performance, testing, native code, and more. The `discover` command only found the 183-line `copilot-instructions.md` (mostly build/test workflow), producing thin rules that missed entire categories of findings.

## Key Files

- `skills/focused-review/SKILL.md` — refresh flow (Step 1: add agent-assisted discovery)
- `skills/focused-review/scripts/focused-review.py` — Python discover subcommand (keep as fast first pass; agent layer added in SKILL.md)
- `Q:/code/runtime/.github/skills/code-review/SKILL.md` — the 685-line source material (runtime repo clone)
- `.agents/benchmark/round-3-findings-table.md` — round 3 baseline for comparison

## Technical Approach

### Phase 0: Environment Setup

Before any benchmark work, set up the environment so all runs use real PR branches with full repo context via Copilot CLI.

**a. Install focused-review as Copilot CLI plugin** via symlink:
- Create symlink: `~/.copilot/plugins/focused-review` → `Q:\code\focused-review\`
- On Windows: `mklink /D "%USERPROFILE%\.copilot\plugins\focused-review" "Q:\code\focused-review"`
- Verify: `copilot plugin list` shows focused-review

**b. Fetch all 8 PR branches into the runtime repo** (`Q:\code\runtime\`):
- For each PR number (124736, 106374, 124725, 124485, 120634, 123921, 122947, 117148):
  - `git fetch origin pull/NNNNN/head:benchmark/pr-NNNNN`
- This creates local branches `benchmark/pr-NNNNN` with the actual PR commits
- Record the merge base for each: `git merge-base main benchmark/pr-NNNNN`

**c. Configure focused-review rules directory** for benchmark:
- Set `focused-review.json` in the runtime repo to point `rules_dir` at the benchmark rules once they're generated

### Phase 1: Fix Discovery

Two-layer approach: keep the Python discover script's hardcoded globs as a fast first pass, then add an agent exploration step in the SKILL.md refresh flow.

1. **Python script (unchanged)**: `INSTRUCTION_PATTERNS` stays as-is — it catches the obvious files (CLAUDE.md, copilot-instructions.md, .cursor/rules, etc.)
2. **SKILL.md Step 1 (new)**: After running `python discover`, use an Explore agent to search the repo for additional relevant source material. The agent reads candidate files (e.g. `.github/skills/**/*.md`) and filters to only those containing **code review guidance** (correctness, style, conventions, patterns). Excludes deployment skills, CI/CD workflows, testing infrastructure, etc.
3. **User override**: Allow users to explicitly list source files in `focused-review.json` config (`"sources": ["path1", "path2"]`)

The key insight: we can't just glob `.github/skills/**/*.md` because that pulls in unrelated skills. The agent reads candidates and decides which are relevant.

Update SKILL.md Step 1 with the agent exploration step.

### Phase 2: Regenerate Rules (clean slate)

Generate rules from scratch — no prior rules influencing the output. This ensures we measure the pure impact of the discovery change.

1. **Archive then remove** existing rules from `Q:/code/runtime/review/`. Move them to `.agents/benchmark/round-4-rules/attempt-1/` (in the focused-review repo) so we can later compare how the discovery change affected generation — then clear `review/` for a clean slate. The refresh flow compares new instructions against existing rules and produces incremental updates; prior rules would bias the generation.
2. Run `focused-review refresh` **interactively** inside `Q:/code/runtime/` (the actual runtime repo where the SKILL.md lives at `.github/skills/code-review/SKILL.md`). This is a manual step — the refresh flow requires user feedback to accept/reject generated rules.
3. **Hard gate**: Before accepting rules, verify the refresh confirmed the code-review SKILL.md was discovered and used as source. If only `copilot-instructions.md` was found, diagnose and fix the discovery step, delete rules, retry. Do not proceed with thin rules — iterate until the full source material is confirmed.
4. The refresh flow should extract ~15-25 rules covering: correctness/safety, thread safety, security, performance, API design, testing, code style, consistency, native code
5. **Model selection check**: Before accepting, verify model assignments are appropriate for a complex runtime codebase. Most rules should be `sonnet` or `inherit` — `haiku` only for purely mechanical/syntactic checks (e.g. null-check patterns). Rules about concurrency, API design, correctness, security must not be `haiku`.
6. Archive the generated rules to `.agents/benchmark/round-4-rules/` (in the focused-review repo)

### Phase 3: Smoke-test on 2 PRs (fail-fast gate)

Before committing to all 8 PRs, validate the new rules on 2 PRs — one small, one large — to get early signal.

**Canary PRs:**
- PR #124736 (Regex LeadingStrings, 162 lines) — small diff, had 3 TPs in round 3
- PR #106374 (Parallel Connect, 720 lines) — medium-large diff, had 7 findings in round 3

**Process per canary PR:**
1. In the runtime repo, checkout the PR branch: `git checkout benchmark/pr-NNNNN`
2. Run focused-review via Copilot CLI: `copilot -p '/focused-review branch --no-autofix' --model claude-opus-4.6 --yolo --silent --add-dir Q:\code\focused-review` from `Q:\code\runtime\`
3. Classify findings as TP, TP-Novel, FP

Copilot CLI baselines for these 2 PRs run as separate tasks (same process as Phase 3c) — they don't need to re-run when canary iteration happens.

The `branch` scope in focused-review uses `git diff main...HEAD` which diffs against the merge base of main and HEAD. When the PR branch is checked out, this gives us the PR's changes against its original merge base — exactly what we want.

**Quality gate (all must pass):**
- FP rate <= 25% across the 2 canary PRs (matches overall success criterion)
- No unjustified misses: if there's a known issue in the diff (human reviewer found it AND it's discoverable from the code-review skill guidance or common sense), focused-review must catch it. Zero TPs is fine if the diff is genuinely clean.

If gate fails → STOP. Diagnose which rules are noisy or what was missed. Fix the SKILL.md generation guidance or individual rules, regenerate, and re-run the canaries. Iterate until the gate passes.
If gate passes → proceed to Phase 3b (remaining 6 PRs).

This avoids wasting 6 more benchmark runs on rules that don't work.

### Phase 3b: Benchmark remaining 6 PRs

After canaries pass, run the same process for the remaining 6 PRs:
- PR #124725 (FSync, 57 lines)
- PR #124485 (SafeEvpPKeyHandle, 138 lines)
- PR #120634 (Socket.SendFile, 240 lines)
- PR #123921 (Threadpool semaphore, 1125 lines)
- PR #122947 (Directory.GetFiles NtQuery, 1318 lines)
- PR #117148 (FSW inotify, 2440 lines)

For each PR:
1. In the runtime repo, checkout the PR branch: `git checkout benchmark/pr-NNNNN`
2. Run focused-review via Copilot CLI: `copilot -p '/focused-review branch --no-autofix' --model claude-opus-4.6 --yolo --silent --add-dir Q:\code\focused-review` from `Q:\code\runtime\`
3. Save review output + analysis, classify findings as TP, TP-Novel, FP
4. After review, checkout main to leave repo clean: `git checkout main`

### Phase 3c: Copilot CLI Baseline

Run Copilot CLI against all 8 PRs as a controlled baseline (canary PRs done in Phase 3, remaining in Phase 3b). Head-to-head comparison under identical conditions.

**Setup:**
- Working directory: `Q:/code/runtime/` (the actual runtime repo, so Copilot loads `.github/skills/code-review/SKILL.md` naturally)
- Model: `claude-opus-4.6` (same model class for fair comparison)
- Mode: non-interactive (`copilot -p` with `--yolo`)
- PR branch checked out: `git checkout benchmark/pr-NNNNN` — Copilot has full repo context

**Per-PR process:**
1. Checkout the PR branch in the runtime repo: `git checkout benchmark/pr-NNNNN`
2. Run Copilot CLI: `copilot -p 'Review the changes on this branch for code quality issues. Focus on correctness, security, concurrency, and API design.' --model claude-opus-4.6 --yolo --silent` from `Q:\code\runtime\`
3. Save raw Copilot output to `.agents/benchmark/pr-NNNNN/copilot-review-output.md`
4. Read `human-comments.json` and `round3-analysis.md` for context
5. Classify each Copilot finding as TP, TP-Novel, or FP using the same methodology
6. Write classification to `.agents/benchmark/pr-NNNNN/copilot-analysis.md`
7. Checkout main to leave repo clean: `git checkout main`

**Two data sources for Copilot comparison:**
- **Historical**: "Copilot Found" column from round 3 findings table — what Copilot commented on the actual GitHub PRs
- **Controlled**: Copilot CLI run locally against the same diffs — systematic, reproducible baseline

### Phase 4: Comparison Report

Compare across three dimensions:
- **Round 4 vs Round 3**: FP rate change, novel finding count, coverage of 13 round-3 "notable misses", per-rule precision
- **Focused-review vs Copilot CLI baseline**: side-by-side finding counts, TP/FP rates, which issues each tool caught that the other missed
- **Copilot CLI vs historical GitHub Copilot**: did the controlled local run produce different results from what Copilot posted on the actual PRs?
- Overall assessment: does focused-review demonstrably outperform stock Copilot?

## Success Criteria

- Discovery finds `.github/skills/code-review/SKILL.md` in the runtime repo via agent exploration
- Refresh generates >= 12 rules (vs 6 in round 3) covering thread safety, security, API design, performance
- FP rate <= 25% across all 8 PRs
- At least 3 of the 13 round-3 "notable misses" are now caught
- Comprehensive findings table produced with all columns from round 3 table
- **Copilot CLI baseline completed for all 8 PRs with same TP/FP classification**
- **Head-to-head comparison table produced showing focused-review vs Copilot performance**

## Decisions

### Bug-spotter regression fix (Phase 3 iteration)

Investigation found that the round 3 and round 4 bug-spotter rule texts were **identical** -- the regression was not caused by a rule text change. The root cause is over-suppression from the anti-false-positive guidance. The original rule had a 4-line paragraph with multiple examples of what NOT to report ("do NOT report speculative or theoretical concerns..."), plus a separate "Wrong (false positive)" code example section teaching the model to stay silent. This biased the model toward "NO VIOLATIONS FOUND" because every real bug has some uncertainty the model could use to rationalize silence.

Fix applied: rewrote the rule to be more balanced:
1. Shortened the anti-FP guidance to a single concise sentence
2. Added a pro-detection instruction: "When you find a concrete bug, report it. Do not stay silent on real bugs."
3. Added more Wrong examples covering the exact bug patterns that were missed (dead code from circular dependencies, early returns blocking alternatives)
4. Removed the "Wrong (false positive)" section that was teaching the model to suppress findings
