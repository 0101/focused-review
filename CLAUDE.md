# Focused Review Plugin

Copilot CLI plugin / Claude Code skill that runs parallel code reviews using committed review rules.

## Architecture

- `plugin.json` — Copilot CLI plugin manifest
- `skills/focused-review/SKILL.md` — orchestration skill (dispatches review agents)
- `skills/focused-review/scripts/focused-review.py` — Python helper (discovery, diff generation, dispatch planning)
- `agents/review-runner.agent.md` — subagent profile (reviews one rule against one diff chunk)
- `skills/focused-review/defaults/` — built-in bootstrap rules (shipped with the plugin, loaded on first refresh)
- `review/` — committed review rules (version-controlled, one rule per `.md` file); directory configurable via `focused-review.json` config file
- `.agents/focused-review/` — ephemeral working directory (gitignored)

## Key Design Decisions

- **Orchestrator stays context-lean**: Python produces a dispatch plan with file paths only; subagents load their own rule + diff content
- **One rule per agent**: each subagent checks exactly one criterion against one diff chunk
- **Rules are version-controlled**: live in `review/` (configurable via `focused-review.json`), reviewed in PRs like code
- **Refresh is explicit**: user runs `/focused-review refresh` — no auto-generation
- **Python for deterministic work** (discovery, diffing, chunking); **LLM for semantic work** (rule extraction, comparison, agent-assisted discovery)
- **Three-layer discovery**: Python globs (fast first pass) → configured `sources` in `focused-review.json` → agent-assisted exploration (reads candidates and filters to code review guidance)
- **Review agents default to Haiku** (fast, cheap); **generation agents use Sonnet** (better at structured extraction)
- **Windows path compatibility required** — primary dev environment is Windows

## Python (`focused-review.py`)

- Python 3.10+
- Tests: `pytest` — run with `python -m pytest skills/focused-review/scripts/tests/`
- Two subcommands:
  - `discover --repo .` — find instruction files, output JSON array of paths
  - `prepare-review --repo . --scope {scope} --rules-dir review/` — produce `dispatch.json` (`--rules-dir` defaults to `focused-review.json` config file, then `review/`)
- Diff chunking: ≤500 lines = single file; >500 lines = split at file boundaries
- Full scope (`--scope full`): produces file lists per rule, no diff

## Rule Format

```yaml
---
autofix: false
model: haiku
applies-to: "**/*Tests*.cs"  # optional glob, omit = all files
source: "CLAUDE.md"
---
# Rule Name
## Rule
## Why
## Requirements
## Wrong
## Correct
```

## Working With This Codebase

- Spec: `docs/spec/focused-review.md`
- Skill instructions: `skills/focused-review/SKILL.md`
- Agent profile: `agents/review-runner.agent.md`
- When changing the Python script, run `python -m pytest skills/focused-review/scripts/tests/` to verify
