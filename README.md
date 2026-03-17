# Focused Review

A Copilot CLI / Claude Code plugin that runs thorough, rule-driven code reviews — and catches what single-pass reviews miss.

**The problem:** AI reviewers scan your diff once and move on. They miss subtle issues, forget half your team's conventions, and can't tell a real bug from a style nit. Pile more rules into a single prompt and recall drops further.

**Focused Review fixes this** with a multi-phase pipeline that mirrors how experienced reviewers actually work:

1. **Discovery** — Each rule and concern gets its own agent, reviewing your diff with full attention. No competition for context, no dropped instructions.
2. **Consolidation** — Findings are deduplicated and prioritized. Same issue caught by three rules? You see it once.
3. **Assessment** — Every finding is challenged against the actual diff. Counter-arguments are constructed. Weak findings are filtered out.
4. **Rebuttal** — High-severity findings marked as invalid get a second look, catching edge cases the assessor missed.

The result: high-signal reviews with near-zero noise, enforcing your specific codebase standards across every PR.

### Key features

- **Rules from your instructions** — Extracts review criteria from `CLAUDE.md`, `AGENTS.md`, and other instruction files already in your repo
- **One rule, one agent** — Each rule gets full context window attention. Dozens of rules, zero degradation
- **Concern-driven discovery** — Beyond rules, the review probes for bugs, security issues, and architectural problems
- **Version-controlled rules** — Rules live in `review/` as Markdown, reviewed in PRs like code
- **Autofix support** — Rules can opt into automatic fixes, applied and verified in-place
- **Any diff scope** — Branch, commit, staged, unstaged, or full codebase scans

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

Refresh review rules from instruction files:

```
/focused-review:review refresh
```

Configure rules directory:

```
/focused-review:review configure
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
autofix: false
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
