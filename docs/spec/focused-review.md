# Focused Review Plugin

## Goals

Build an installable plugin (Copilot CLI native, Claude Code compatible) that:
1. Reads committed review rules from the rules directory (`review/` by default, configurable via `focused-review.json`)
2. Runs parallel review agents — one per rule — against the user's diff
3. Supports path-scoped rules (`applies-to` globs), diff chunking for large diffs, and full-codebase review
4. Offers a `refresh` command that discovers instruction files, extracts potential new rules via LLM, compares against existing rules, and lets the user decide what to update

## Expected Behavior

### Review Mode: `/focused-review [scope]`

The primary flow. Rules are already committed in the rules directory (`review/` by default, configurable via `focused-review.json`).

1. **Prepare**: Python reads all rule `.md` files from `review/`, generates diff based on scope (`branch` default, or `commit|staged|unstaged|full`), chunks large diffs at file boundaries, filters rules by `applies-to` against changed files, produces a dispatch plan JSON.

2. **Dispatch**: Orchestrator reads only the small dispatch plan (paths + model). Launches parallel subagents — each reads its own rule file + diff chunk from disk. Orchestrator never loads rule content or diff content (constant context budget).

3. **Report**: Compile all agent results into `.agents/focused-review/review-{timestamp}.md`. Format: header with scope and timestamp, then per-rule sections listing rule name and findings (or `NO VIOLATIONS FOUND`).

### Refresh Mode: `/focused-review refresh`

Run when setting up a new repo or when instruction files have changed. Transparent and user-controlled — no magic.

1. **Discover**: Python finds all instruction files in the repo (CLAUDE.md, .github/copilot-instructions.md, .github/instructions/**/*.instructions.md, GEMINI.md, .cursorrules, agents.md, .windsurfrules, .clinerules, etc.). Also checks `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` env var. Resolves symlinks, deduplicates.

2. **Compare & Extract**: LLM agent (Sonnet) reads each instruction file AND existing committed rules from `review/`. Produces a categorized action plan:
   - **New rules**: in instructions but no matching committed rule — includes full rule content
   - **Updated rules**: committed rule that needs changes based on current instructions — includes updated content
   - **Orphaned rules**: committed rule whose `source` instruction file no longer exists or has changed significantly (may be intentional — user decides)
   - **Unchanged**: committed rule still matches instructions

3. **Present**: Show summary with a sane default action:
   - Default: add new reasonable rules, apply updates, leave orphaned and unchanged rules alone
   - User can review each category and override individual decisions

4. **Apply**: LLM directly creates/edits/deletes rule files in `review/` using file tools. Updated rules preserve the original `source` field for traceability.

### Key Design Principles

- **Rules are committed, version-controlled** — they live in `review/` alongside the code, reviewed in PRs like any other change
- **No hidden caching or auto-generation** — the refresh command is explicit, transparent, user-controlled
- **Orchestrator stays context-lean** — Python produces dispatch plan with paths only, subagents load their own content
- **One rule per agent** — each subagent checks exactly one criterion

## Technical Approach

### Packaging: Copilot CLI Plugin

```
focused-review/
├── plugin.json                    # Copilot CLI plugin manifest
├── skills/
│   └── focused-review/
│       ├── SKILL.md               # Main skill — orchestration instructions
│       └── scripts/
│           └── focused-review.py  # Python helper
└── agents/
    └── review-runner.agent.md     # Review-runner agent profile
```

- `plugin.json`: name, description, version, skill/agent paths
- `.claude-plugin/`: marketplace manifest + Claude Code plugin descriptor for dual-target distribution
- Copilot CLI: `copilot plugin install ./` or `copilot plugin install owner/repo`
- Claude Code: `copilot plugin marketplace add 0101/focused-review`

### Skill Arguments

| Argument | Description |
|----------|-------------|
| _(empty)_ or `branch` | Review: diff against `origin/main` (default) |
| `commit` | Review: diff of last commit |
| `staged` | Review: staged changes |
| `unstaged` | Review: unstaged changes |
| `full` | Review: scan entire codebase (no diff) |
| `refresh` | Refresh: re-scan instruction files, suggest rule updates |
| `configure` | Configure: create or update `focused-review.json` interactively |

### Python Script Subcommands

| Subcommand | Purpose |
|------------|---------|
| `discover --repo .` | Find instruction files (including COPILOT_CUSTOM_INSTRUCTIONS_DIRS), output JSON |
| `prepare-review --repo . --scope branch --rules-dir review/` | Read committed rules, generate diff, filter by `applies-to`, chunk, produce dispatch plan |

The refresh flow's comparison and file writing are handled by the LLM orchestrator directly (SKILL.md), not Python — semantic comparison and infrequent file operations don't benefit from scripting.

### Rule Format

```yaml
---
autofix: false                  # ignored (kept for compatibility)
model: haiku
applies-to: "**/*Tests*.cs"    # optional glob, omit = all files
source: "CLAUDE.md"             # which instruction file produced this
---
# Rule Name
## Rule
One-sentence summary.
## Why
Justification.
## Requirements
- Concrete, checkable requirements
## Wrong
Code example.
## Correct
Code example.
```

### Diff Chunking

- Small (≤3000 lines): single diff.patch
- Large (>3000 lines): split at file boundaries into chunks (~3000 lines each)
- Full codebase (`--scope full`): file lists per rule, no diff

### Instruction File Discovery Patterns

```
CLAUDE.md, **/CLAUDE.md (max 2 levels)
GEMINI.md
AGENTS.md, agents.md
.github/copilot-instructions.md
.github/instructions/**/*.instructions.md
.cursor/rules/*.md, .cursor/rules/*.mdc
.cursorrules
.windsurfrules
.clinerules
$COPILOT_CUSTOM_INSTRUCTIONS_DIRS (env var, colon/semicolon-separated paths)
```

### Per-Repo Working Directory

```
{repo}/
├── review/                               # Committed review rules (version-controlled)
│   ├── sealed-classes.md
│   ├── immutable-data.md
│   └── ...
└── .agents/focused-review/               # Ephemeral working directory (gitignored)
    ├── diff.patch
    ├── changed-files.txt
    ├── chunks/
    │   ├── diff-001.patch
    │   └── ...
    ├── dispatch.json
    └── review-{timestamp}.md
```

## Decisions

- **No replacement of existing `~/.claude/commands/focused-review.md`** — the plugin is standalone and installable
- **Rules are committed to `review/` in the repo** — version-controlled, reviewed in PRs, no hidden caching
- **Refresh is explicit, not automatic** — user runs `/focused-review refresh` when they want to update rules
- **Refresh presents a sane default** — add new, fix contradictions, leave rest — but user can override each decision
- **Python for deterministic, performance-critical work** — discovery, diff splitting, dispatch planning
- **LLM for everything requiring understanding** — rule extraction, comparison against existing rules, contradiction detection, file writing during refresh
- **Review pipeline is read-only** — the pipeline reports findings but never modifies source files; fix suggestions stay in findings
- **Review agents inherit the orchestrator's model by default** — rules can override to `haiku` or `sonnet` for mechanical checks
- **Generation agents use Sonnet** — better at structured extraction from prose
- **Windows path compatibility required** — primary dev environment is Windows
- **Default rules directory is `review/`** at repo root — simple, visible, not hidden; configurable via `focused-review.json` config file (see `docs/spec/configurable-rules-dir.md`)
- **Self-dismissal suppression in review-runner** — when the agent concludes "no change needed", it outputs `NO VIOLATIONS FOUND` instead of a finding. Prevents false positives from self-dismissing findings.
- **Rule generation excludes cosmetic-only rules** — refresh guidance skips rules about whitespace, indentation, brace style (enforced by formatters). Rules require Wrong/Correct examples that differ in behavior, not just formatting.
- **Model selection favors `inherit` for judgment rules** — rules about concurrency, API design, correctness use `inherit` (resolves to user's model). Only purely mechanical/syntactic checks use `haiku`.
