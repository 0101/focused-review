# Focused Review Plugin

Auto-generates review rules from instruction files and runs parallel code reviews.

## Why

AI coding agents can follow a single instruction reliably — but give them a long list of rules and they start dropping things. The more instructions you pile into `CLAUDE.md`, `.cursorrules`, or `AGENTS.md`, the less consistently any one of them gets enforced.

Focused Review fixes this by giving each rule to its own agent. One rule, one agent, one job. The result is reliable enforcement even across dozens of rules.

- **Rules from your instructions**: Extracts review criteria from the instruction files already in your repo
- **One rule per agent**: Each rule gets full attention — no competition for context
- **Version-controlled rules**: Rules live in `review/` as Markdown files, reviewed in PRs like code
- **Works with your diff workflow**: Branch, commit, staged, unstaged, or full codebase scans

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
/focused-review
```

Specify a diff scope:

```
/focused-review branch      # diff against origin/main (default)
/focused-review commit      # diff of last commit
/focused-review staged      # staged changes only
/focused-review unstaged    # unstaged changes only
/focused-review full        # scan entire codebase (no diff)
```

Refresh review rules from instruction files:

```
/focused-review refresh
```

Configure rules directory:

```
/focused-review configure
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

Run `/focused-review configure` to create or update the config file interactively.

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
