# Focused Review Plugin

Auto-generates review rules from instruction files and runs parallel code reviews.

## Installation

### Copilot CLI

```bash
copilot plugin marketplace add 0101/focused-review
copilot plugin install focused-review@0101/focused-review
```

### Claude Code

```bash
/plugin marketplace add 0101/focused-review
/plugin install focused-review@0101/focused-review
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

## Review Rules

Review rules live in the `review/` directory at your repo root. Each rule is a Markdown file with YAML frontmatter:

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
