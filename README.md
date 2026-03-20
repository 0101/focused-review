# Focused Review

A Copilot CLI / Claude Code plugin for thorough, consistent code reviews.

Focused Review breaks your review criteria into individual rules and concerns, then gives each one a dedicated context window. No competition for attention, no dropped instructions. The result is high recall and precision, even across dozens of review criteria.

A multi-phase pipeline then validates and refines findings:

1. **Discovery** — Rules and concerns run in parallel, each in its own agent with a focused context window.
2. **Consolidation** — Findings are deduplicated and prioritized. Same issue caught by three rules? You see it once.
3. **Assessment** — Every finding is challenged against the actual diff. Counter-arguments are constructed. Weak findings are filtered out.
4. **Rebuttal** — High-severity findings marked as invalid get a second look, catching edge cases the assessor missed.

The result: high-signal reviews with near-zero noise, enforcing your specific codebase standards across every PR.

### Key features

- **Auto-generated rules** — Scans your `CLAUDE.md`, `AGENTS.md`, and other instruction files and extracts review criteria as individual rules
- **Built-in defaults** — Ships with universal rules (code duplication, general review) and concerns (security, bugs, architecture) — use them as-is or as a starting point
- **Focused contexts** — Each rule gets its own agent with full context window attention. Dozens of rules, zero degradation
- **Version-controlled rules** — Rules live in `review/` as Markdown, reviewed in PRs like code
- **Any diff scope** — Branch, commit, staged, unstaged, or full codebase scans

### Concerns

Some review criteria need broad context and benefit from multiple perspectives — things like architectural coherence, error handling patterns, or security posture. These get expressed as **concerns**: higher-level review prompts that are run by multiple LLM models in parallel. Where rules are precise and narrow, concerns cast a wider net across your codebase.

This separation also lets you scale cost and quality independently. Simple style rules run on fast, cheap models with small focused contexts. Deep concerns like security get multiple frontier models that spend time understanding the codebase for a proper in-depth review.

You choose which rules and concerns to enable, and at what scale — from a lightweight pass with a few built-in defaults to a comprehensive review with dozens of project-specific rules and multi-model concerns.

## Installation

### Copilot CLI

```bash
copilot plugin marketplace add 0101/focused-review
copilot plugin install focused-review@focused-review
```

### Claude Code

```bash
/plugin marketplace add 0101/focused-review
/plugin install focused-review@focused-review
```

## Usage

### Getting started

The skill is conversational — you can ask it to help you set things up:

```
/focused-review:review Help me set this up!
```

Or use the built-in commands to configure step by step:

```
/focused-review:review configure    # set rules directory, discover instruction files
/focused-review:review refresh      # generate rules from your instruction files
```

`refresh` scans your instruction files (`CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`, etc.), extracts review criteria, and writes them as individual rule files to `review/`. If no instruction files are found, it copies the built-in defaults — general review, code duplication, plus security, bugs, and architecture concerns.

Review the generated rules in `review/`, enable or disable what you need, and commit them. From here on, rules are version-controlled like any other code.

### Running a review

Run a focused review against the current branch diff (default):

```
/focused-review:review
```

Specify a diff scope:

```
/focused-review:review branch      # diff against origin/main (default)
/focused-review:review commit      # diff of last commit
/focused-review:review staged      # staged changes only
/focused-review:review unstaged    # unstaged changes only
/focused-review:review full        # scan entire codebase (no diff)
```

## Configuration

Rules directory is controlled by a `focused-review.json` config file:

```json
{ "rules_dir": "custom-rules/" }
```

The config file is discovered from these locations (first match wins):

1. `.claude/focused-review.json` -- project shared (version-controlled)
2. `focused-review.json` -- repo root
3. `.github/focused-review.json` -- GitHub convention
4. `~/.claude/focused-review.json` -- user-wide (Claude Code)
5. `~/.copilot/focused-review.json` -- user-wide (Copilot CLI)

If no config file is found, defaults to `review/`.

Resolution priority: explicit `--rules-dir` CLI flag > `focused-review.json` config file > `review/` default.

Run `/focused-review:review configure` to create or update the config file interactively.

## Review Rules

Review rules live in the rules directory (`review/` by default, configurable via `focused-review.json`). Each rule is a Markdown file with YAML frontmatter:

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

Rules are version-controlled and reviewed in PRs like any other code change.

## Project Structure

```
focused-review/
├── plugin.json                         # Copilot CLI plugin manifest
├── .claude-plugin/
│   ├── plugin.json                     # Claude Code plugin manifest
│   └── marketplace.json                # Self-hosted marketplace catalog
├── skills/
│   └── focused-review/
│       ├── SKILL.md                    # Main skill — orchestration instructions
│       └── scripts/
│           └── focused-review.py       # Python helper (discover, prepare-review)
├── agents/
│   └── review-runner.agent.md          # Review-runner agent profile
├── review/                             # Committed review rules (per-repo)
│   └── ...
└── .agents/focused-review/             # Ephemeral working directory (gitignored)
    ├── diff.patch
    ├── dispatch.json
    └── review-{timestamp}.md
```
