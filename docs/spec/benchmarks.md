# Verification & Benchmarks

Quality verification data for the focused-review plugin. For system description, see `docs/spec/focused-review.md`.

## Verification Strategy

### Progressive Verification

Verification proceeds in tiers. Each tier surfaces issues → fix tasks are created and resolved → next tier runs against the improved system.

**Tier 1 — Pipeline smoke test**: Run on this repo's own branch diff. Verify all 5 phases execute end-to-end. Goal: mechanical correctness, no crashes.

**Tier 2 — Runtime baseline check**: Run against dotnet/runtime PRs from the Round 4.1 benchmark. Compare to baseline (91% precision, 9% FP rate). Goal: match or improve.

**Tier 3 — Diverse repo benchmark + head-to-head**: Run against diverse benchmark PRs. Head-to-head vs Copilot CLI. Goal: quality generalizes across repos.

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

### Benchmark Baseline (Round 4.1)

Results on 8 dotnet/runtime PRs:
- 67 deduplicated findings, 91% precision, 9% FP rate
- FR outperforms Copilot CLI on 7/8 PRs (2.5× more findings, 2.5× better precision)
- 61% of valid findings are TP-Novel (humans missed them)

### Quality Targets

1. Bugs/security as full `copilot -p` sessions → improved recall on complex bugs
2. Assessment phase filtering false positives → target ≤9% FP rate
3. Consolidation eliminating cross-rule duplicates
4. Quality generalizes across diverse repos, not just dotnet/runtime

## Implementation-Specific Decisions

These are technical decisions from the pipeline implementation. Preserved here as reference:

- **Model name mapping** — Concern files use shorthand names (opus, codex, gemini); `_resolve_model()` maps to full CLI identifiers. Case-insensitive lookup.
- **Prompt as direct CLI argument** — `copilot -p <prompt>` passes prompt as argument (not stdin). Subject to OS argument length limits.
- **OSError handling** — `_run_single_concern` catches `OSError` for oversized prompts (Windows 32K CreateProcess limit). No retry.
