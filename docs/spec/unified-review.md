# Unified Review System

## Goals

Evolve the focused-review plugin into a single-command review system that combines rule-based checks AND general concern analysis (bugs, security, architecture), then validates, deduplicates, and prioritizes all findings through a multi-phase pipeline.

1. Single `/focused-review [scope]` command runs both rules and concerns in parallel
2. Five-phase pipeline: Discovery → Consolidation → Assessment → (Rebuttal) → Presentation
3. Rules dispatch as lightweight Task subagents (existing behavior, unchanged)
4. Concerns dispatch as full `copilot -p` CLI sessions with deep codebase exploration
5. Consolidation deduplicates findings across all sources
6. Assessment validates each finding with adversarial counter-arguments (Advocate-style)
7. Final report includes provenance, severity, fix complexity, and connected findings
8. Post-mortem flow traces invalid findings back to source rules/concerns

## Expected Behavior

### Rules vs Concerns Taxonomy

**Rules** — Mechanical, single-criterion checks. Run as Task subagents (Haiku). One per agent. Fast, cheap, parallel in batches of 12. Existing behavior.

**Concerns** — Broad categories requiring deep exploration. Run as `copilot -p` CLI sessions (Opus/Codex/Gemini). Each concern has a role, evidence standards, and output format. Always includes a "general" catch-all concern.

### Five-Phase Pipeline

**Phase 1 — Discovery**: All rules + concerns run in parallel. Wide net. Rule agents write to `.agents/focused-review/findings/rule--{name}.md`. Concern agents write to `.agents/focused-review/findings/concern--{name}--{model}.md`.

**Phase 2 — Consolidation**: Single agent reads all finding files. Semantic dedup (same location + same issue = one finding with multiple sources). Merges best reasoning. Averages severity/complexity. Output: `.agents/focused-review/consolidated.md`.

**Phase 3 — Assessment**: Sequential agent validates each finding. Checks if really introduced by diff. Adds counter-arguments. Verdict: Confirmed / Questionable / Invalid. For rule violations: evaluates rule applicability. Output: `.agents/focused-review/assessed.md`.

**Phase 4 — Rebuttal** (optional, `scaling=thorough`): High-priority findings marked Invalid get sent back for adversarial challenge.

**Phase 5 — Presentation**: Final report groups connected findings, presents Confirmed + Questionable, shows provenance and assessment reasoning. Output: `.agents/focused-review/review-{timestamp}.md`.

### Concern File Format

```yaml
---
type: concern
models: [opus]
priority: standard
applies-to: "**/*.cs"   # optional
---
# Concern Name
## Role
## What to Check
## Evidence Standards
## Output Format
```

### Adaptive Scaling

Diff size drives dispatch intensity:
- 1-10 lines: Rules + 1 general concern (single model) — lightweight but catches convention issues
- 11-100 lines: Rules + bugs + security concerns (single model each) — architecture/general omitted to keep cost proportional
- 101-500 lines: Rules + all concerns (primary model)
- 501+ lines: Rules + all concerns (multi-model for high-priority concerns)

"Key concerns" at the 11-100 tier = bugs + security only. Architecture and general are omitted to keep cost proportional to diff size.

### Finding Format

Both rule agents and concern agents write markdown findings with: File (path:line), Severity (Critical/High/Medium/Low), Fix complexity (quickfix/moderate/complex), Description, Reasoning, Suggestion, Evidence.

Filenames encode provenance: `rule--sealed-classes.md`, `concern--bugs--opus.md`.

### SKILL.md / REFRESH.md Split

SKILL.md stays lean — review pipeline only. Routes "refresh"/"configure" to REFRESH.md. REFRESH.md handles bootstrap + refresh for both rules AND concerns.

## Technical Approach

### Python Script Extensions

`focused-review.py` gains:
- `prepare-review`: Extended to read concern files, generate per-file diffs to `.agents/focused-review/diffs/`, write concern prompt files
- `run-concerns`: NEW subcommand. Launches `copilot -p` per (concern × model) via ThreadPoolExecutor. Retry/timeout. Writes findings to `.agents/focused-review/findings/concern--*.md`.

### New Agent Profiles

- `agents/review-consolidator.agent.md` — Phase 2: reads all findings, deduplicates, merges
- `agents/review-assessor.agent.md` — Phase 3: validates findings, adds counter-arguments

### Working Directory Structure

```
.agents/focused-review/
├── diff.patch
├── changed-files.txt
├── diffs/                         # Per-file diff patches
├── chunks/                        # Chunked diffs for rules
├── dispatch.json
├── prompts/                       # Generated concern prompts
├── findings/                      # Phase 1 outputs
│   ├── rule--*.md
│   └── concern--*--*.md
├── consolidated.md                # Phase 2
├── assessed.md                    # Phase 3
├── rebuttals/                     # Phase 4
└── review-{timestamp}.md          # Phase 5
```

### Configuration

Per-project config in `focused-review.json`:
```json
{
    "rules_dir": "review/",
    "concerns_dir": "review/concerns/",
    "scaling": "standard"
}
```

## Decisions

- **Existing rule review is unchanged** — rules still dispatch as Task subagents, same format
- **Concerns use `copilot -p`** — full CLI sessions with all tools, unlike lightweight Task subagents
- **Python launches concerns** — ThreadPoolExecutor adapted from comprehensive-review.py
- **Markdown throughout** — no JSON interchange between phases, filenames encode provenance
- **SKILL.md / REFRESH.md split** — progressive disclosure keeps review pipeline context-lean
- **Post-mortem is suggest-only** — after review, user marks invalids → system traces cause and produces recommendation report (does not edit rule/concern files directly)
- **Post-mortem invoked as a mode** — `/focused-review post-mortem [numbers]` is a separate SKILL.md mode alongside review/refresh/configure. Finding numbers reference the `### {n}.` headings in the review report. If omitted, presents the table interactively
- **Built-in concerns shipped in defaults/** — bugs, security, architecture, general
- **Rule findings saved to disk by orchestrator** — review-runner agents return output as text; SKILL.md saves to `findings/rule--{name}.md` so Phase 2 consolidator can read them alongside concern findings
- **Scaling moved to Python `scale-concerns` subcommand** — tier logic extracted from SKILL.md one-liner into `focused-review.py scale-concerns` with `--diff-path`/`--diff-lines` and `--dispatch-path`. Pure functions `_filter_concerns_by_tier`, `_dedup_concerns`, `_diff_lines_to_tier` are unit-tested
- **`full` scope skips assessment** — no diff.patch exists for `full` scope, so Phases 3–4 are skipped; consolidated findings are treated as Confirmed
- **Scaling dedup across all tiers** — all non-501+ tiers deduplicate multi-model concern entries to keep one model per concern. The 1-10 tier caps at one entry via `[:1]`, the 11-100 and 101-500 tiers use dict-dedup by concern name (keeping the first/primary model entry)
- **Phase 2 failure fallback** — Step 7 has three data source tiers: assessed.md → consolidated.md → raw findings/. The third case ensures the pipeline degrades gracefully when consolidation crashes
- **Model name mapping** — Concern files use shorthand names (opus, codex, gemini); `_resolve_model()` maps these to full CLI identifiers (claude-opus-4.6, gpt-5.1-codex, gemini-3-pro-preview). Unknown names pass through unchanged so users can specify full names directly. `MODEL_MAP` dict lives in constants section of focused-review.py
- **Prompt as direct CLI argument** — `copilot -p <prompt>` passes the prompt text as a direct argument, not via stdin (`-p -` does not work in copilot CLI). This means prompts are subject to OS argument length limits but avoids stdin piping issues
- **OSError handling for oversized prompts** — `_run_single_concern` catches `OSError` (raised when the prompt exceeds OS argument limits like Windows 32K CreateProcess) and returns a structured error dict immediately without retrying. `_resolve_model()` uses case-insensitive lookup so concern files can use `Opus`, `OPUS`, etc.

## Key Files

- `skills/focused-review/SKILL.md` — main orchestration (5 phases)
- `skills/focused-review/REFRESH.md` — bootstrap + refresh (rules + concerns)
- `skills/focused-review/scripts/focused-review.py` — Python helper
- `agents/review-runner.agent.md` — rule subagent (existing)
- `agents/review-consolidator.agent.md` — consolidation agent (new)
- `agents/review-assessor.agent.md` — assessment agent (new)
- `skills/focused-review/defaults/concerns/` — built-in concern prompts

## Verification Strategy

### Progressive Verification

Verification proceeds in tiers. Each tier surfaces issues → fix tasks are created and resolved → next tier runs against the improved system. This prevents wasted effort benchmarking a broken pipeline and ensures fixes are validated incrementally.

**Tier 1 — Pipeline smoke test**: Run on this repo's own branch diff. Verify all 5 phases execute end-to-end: findings files created (both rule and concern), consolidation deduplicates, assessment produces verdicts, final report generated. Goal: mechanical correctness, no crashes.

**Tier 2 — Runtime baseline check**: Run against 2-3 dotnet/runtime PRs from the Round 4.1 benchmark (#123921, #117148, #106374). Classify findings as TP/TP-Novel/FP, compute precision. Compare to R4.1 baseline (91% precision, 9% FP rate). Goal: match or improve on known baseline.

**Tier 3 — Diverse repo benchmark + head-to-head**: Run against the diverse benchmark PRs listed below. Head-to-head vs Copilot CLI. Goal: quality generalizes across repos, not just runtime-tuned.

Each tier may spawn fix tasks that block the next tier. The system is not "done" until Tier 3 passes.

### Benchmark PRs

**Tier 2 — dotnet/runtime (existing R4.1 baseline, C#)**

| PR | Area | Diff | Review threads | Notes |
|----|------|------|----------------|-------|
| #123921 | Threadpool semaphore | ~1125 lines | 5 critical TPs | Uninitialized memory, integer overflow |
| #117148 | FSW inotify refactor | 2440 lines | 6 TPs + 6 novel | Volatile access, TOCTOU race |
| #106374 | Parallel Connect | 720 lines | 1 TP + 18 novel | Race condition, null-forgiving |

Full baseline: 8 PRs total (#106374, #117148, #120634, #122947, #123921, #124485, #124725, #124736).

**Tier 3 — Diverse repos (Python + F#)**

| Repo | PR | Area | Diff | Review threads | Why selected |
|------|----|------|------|----------------|--------------|
| django/django | #20300 | Deprecate Field.get_placeholder → get_placeholder_sql | 184+80 lines, 17 files | 23 threads | MRO correctness, SQL compilation edges, deprecation patterns. Python web framework. |
| dotnet/fsharp | #19040 | Type subsumption cache: unsolved type vars | 180+64 lines, 8 files | 8 threads | Caching correctness, race conditions, type variable stability. F# compiler. |
| dotnet/fsharp | #19297 | Static compilation of state machines | 342+103 lines, 14 files | 5 threads | Compiler lowering, resumable code. F# compiler. |

### Benchmark Baseline (from focused-review repo)

Round 4.1 results on 8 dotnet/runtime PRs establish the quality bar:
- 67 deduplicated findings, 91% precision, 9% FP rate
- FR outperforms Copilot CLI on 7/8 PRs (2.5× more findings, 2.5× better precision)
- bug-spotter rule achieves 100% precision but limited recall as Haiku subagent
- code-style-conventions has 33% precision (hallucination issues)
- 61% of valid findings are TP-Novel (humans missed them)

### Quality Targets

The unified system should improve on this baseline by:
1. Running bugs/security as full `copilot -p` sessions (not Haiku subagents) — expect improved recall on complex bugs
2. Assessment phase filtering false positives that currently survive — target ≤9% FP rate
3. Consolidation eliminating cross-rule duplicates (24 of 91 raw findings were duplicates in R4.1)
4. Maintaining quality across diverse repos, not just dotnet/runtime
