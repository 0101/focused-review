# Focused Review Plugin

## What It Is

A code review system that runs as a Copilot CLI plugin (or Claude Code skill). It checks code changes against committed review rules and broad concerns (bugs, security, architecture), validates findings through a multi-phase pipeline, and produces a prioritized report.

Everything is version-controlled. Rules, concerns, and project context live in the repo alongside the code, reviewed in PRs like any other change. The system works out of the box with built-in defaults and improves as teams customize it for their project.

## What Users See

When the plugin is set up, the repo gains a `review/` directory (configurable):

```
review/
├── project.md              # What this project is, what matters, trade-off guidance
├── rules/                  # Specific code rules — one criterion per file
│   ├── windows-path.md     # "Use pathlib for path handling in Python"
│   ├── no-python-llm.md    # "Python must not parse LLM-generated content"
│   └── ...
└── concerns/               # Broad review categories — deep exploration per category
    ├── bugs.md             # Adversarial bug finder
    ├── security.md         # Vulnerability scanner
    └── architecture.md     # Pattern consistency reviewer
```

**Rules** are single-criterion checks. Each rule has Requirements, Wrong/Correct examples, and runs as a lightweight subagent. They catch specific patterns: naming violations, missing annotations, forbidden API usage.

**Concerns** are broad review categories. Each concern has a role, evidence standards, and runs as a full CLI session with deep codebase exploration. They find complex issues: race conditions, injection vulnerabilities, abstraction leaks.

**Project context** (`project.md`) tells the review system what kind of project this is and what matters most. A compiler cares about correctness and performance. A SaaS app cares about clarity and security. This calibrates how findings are assessed — a micro-optimization finding is Critical in a trading system but Low in a CRUD app.

A human or agent reading `review/` can immediately understand: what this project is (project.md), what specific patterns are enforced (rules/), and what broad categories are analyzed (concerns/).

## Lifecycle

### 1. Installation

```bash
# Copilot CLI
copilot plugin install 0101/focused-review

# Claude Code
copilot plugin marketplace add 0101/focused-review
```

The plugin installs globally — nothing is added to the repo yet. The plugin ships with built-in default rules and concerns in its `defaults/` directory.

### 2. First Use — Two Paths

**Path A: Just run it (zero setup)**

```bash
/focused-review
```

No rules found → the plugin auto-triggers refresh. Refresh discovers instruction files in the repo (CLAUDE.md, .github/copilot-instructions.md, etc.), extracts rules from them, and copies built-in default concerns. Creates `review/rules/` and `review/concerns/`. Runs the review. Done.

After the review, the plugin nudges: *"No project context found. Run `/focused-review configure` to generate `review/project.md` — reviews work without it, but assessment quality improves with project context."*

Good enough for a first review. Assessment uses generic validation, which catches obvious false positives but can't calibrate for domain-specific priorities.

**Path B: Explicit setup (recommended)**

```bash
/focused-review configure    # Set directories, generate project.md
/focused-review refresh       # Discover rules + populate concerns
/focused-review               # Run the review
```

Configure asks for directory preferences, examines the repo to detect project type, and generates `review/project.md` with priorities and trade-off guidance. The user reviews and edits before committing.

Refresh scans instruction files, extracts rules, compares against existing rules, and presents a summary for the user to accept/modify. Also adds built-in default concerns if not already present.

### 3. Day-to-Day Review

```bash
/focused-review              # Diff against origin/main (default)
/focused-review branch       # Same as above
/focused-review commit       # Last commit only
/focused-review staged       # Staged changes
/focused-review unstaged     # Unstaged changes
/focused-review full         # Entire codebase (no diff, skips assessment)
/focused-review full src/    # Only files under src/ (no diff)
/focused-review branch src/  # Only changes in src/
/focused-review staged **/*.cs  # Only staged changes to .cs files
```

Free-text is also supported — the orchestrator infers scope and paths from natural language:

```bash
/focused-review review all the unit tests       # → full scope, test file globs
/focused-review review the auth module          # → full scope, path to auth code
/focused-review review changes in src/auth      # → ambiguous, will ask to clarify
```

When intent is ambiguous (e.g., "changes" without specifying branch/staged/unstaged), the orchestrator asks the user to clarify — unless an unattended directive is present (e.g., "CI run, don't ask questions"), in which case it defaults to `branch`.

Runs the five-phase pipeline (see Review Pipeline below). Produces a report at `.agents/focused-review/review-{timestamp}.md` and prints a summary to the terminal.

### 4. CI Use

Same command, non-interactive. Typically in a GitHub Actions workflow or similar:

```bash
/focused-review branch
/focused-review post-comments {pr-url}
```

The first command runs the review. The second posts findings as PR comments. No prompts, no configure — uses whatever rules/concerns/project.md are committed in the repo.

CI gets the same quality as local use because all configuration is committed. No "works on my machine" differences.

### 5. Post-Review Maintenance

**Post-mortem** — when findings were wrong:
```bash
/focused-review post-mortem 1,3,5
```
Traces invalid findings back to their source rules/concerns. Analyzes the false-positive pattern. Suggests specific rule/concern adjustments. Does not edit files directly — the user reviews and applies.

**Refresh** — when instruction files change:
```bash
/focused-review refresh
```
Re-scans instruction files, compares against existing rules, proposes additions/updates/orphan cleanup. User picks what to apply.

**Manual editing** — direct rule/concern/project.md changes:
- Edit any file in `review/` directly
- Rules can be added, modified, or deleted
- Concerns can be customized with domain-specific assessment sections
- Project context can be updated as priorities evolve
- All changes go through normal PR review

### 6. Evolution Over Time

The review configuration is a living document that evolves with the project:

| What changes | How | When |
|---|---|---|
| Instruction files updated | `/focused-review refresh` | After editing CLAUDE.md, copilot-instructions, etc. |
| False positives from a rule | `/focused-review post-mortem` → edit rule | After reviewing a report |
| Missing concern category | Create new `.md` in `review/concerns/` | When a new review focus emerges |
| Project priorities shift | Edit `review/project.md` | Periodically, or after major architectural changes |
| New team member onboards | Read `review/` directory | The directory is self-documenting |

## Review Pipeline

Five-phase pipeline: Discovery → Consolidation → Assessment → (Rebuttal) → Presentation.

### Phase 1: Discovery (parallel)

All rules and concerns run simultaneously.

**Rules** dispatch as lightweight Task subagents (one per rule, model from rule frontmatter). Each reads its own rule file + diff chunk. Writes findings to `findings/rule--{name}.md`.

**Concerns** dispatch as full `copilot -p` CLI sessions (one per concern × model). Deep codebase exploration with full tool access. Writes findings to `findings/concern--{name}--{model}.md`.

Discovery optimizes for recall over precision — flag everything suspicious. Assessment filters later.

### Phase 2: Consolidation

Single agent reads all finding files. Semantic deduplication: same file + within 5 lines + same issue = one finding with merged provenance. Prioritizes by severity → source count → fix complexity. Caps at 30 findings. Output: `consolidated.md`.

### Phase 3: Assessment (parallel)

One investigation agent per finding, each with a full context window. Reads source rules, concerns, and project context. For each finding:

1. **Investigate** — read actual source code, trace code paths, verify claims, check callers
2. **Build pro-arguments** — evidence the issue IS real
3. **Build counter-arguments** — evidence it's NOT a problem
4. **Weigh proportionality** — severity vs fix cost (Critical bugs always reported; Low-severity issues with disproportionate fix cost are filtered)
5. **Verdict** — Confirmed / Questionable / Invalid

Output: `assessed.md`.

### Phase 4: Rebuttal (optional)

Critical/High findings marked Invalid get challenged by a rebuttal agent. Safety net against over-aggressive filtering.

### Phase 5: Presentation

Final report with confirmed findings, questionable findings, and a provenance trail showing which rules/concerns found each issue.

## File Formats

### Rule Format

```yaml
---
autofix: false
model: inherit                 # inherit | sonnet | haiku
applies-to: "**/*Tests*.cs"   # optional glob, omit = all files
source: "CLAUDE.md"            # which instruction file produced this
---
# Rule Name
## Rule
One-sentence summary.
## Why
Justification.
## Requirements
- Concrete, checkable requirements
## Wrong
Code example showing violation.
## Correct
Code example showing compliant code.
## Assessment                    # optional — domain-specific verification guidance for the assessor
```

### Concern Format

```yaml
---
type: concern
models: [opus, codex, gemini]
priority: high                 # high | standard
applies-to: "**/*.cs"         # optional
---
# Concern Name
## Role
Reviewer persona and approach.
## What to Check
Categories and specific patterns.
## Evidence Requirements
What constitutes valid evidence (triggers, traces, impact).
## Anti-patterns
Common false-positive patterns to avoid.
## Assessment                    # optional — domain-specific verification guidance for the assessor
```

### Project Context Format

```markdown
# Project Review Context

## Project Type
What this project is — e.g., "ASP.NET Core web API", "F# compiler", "Python CLI tool"

## Priorities (highest first)
1. Most important concern
2. Second
3. Third

## Trade-off Guidance
- When X conflicts with Y, prefer X because...

## Domain Notes
- Things about this codebase a reviewer should know
- Framework guarantees, architectural patterns
- Common false-positive patterns specific to this project
```

### Configuration (`focused-review.json`)

```json
{
  "rules_dir": "review/rules/",
  "concerns_dir": "review/concerns/"
}
```

Locations searched (in order): `.claude/focused-review.json`, `focused-review.json`, `.github/focused-review.json`, `~/.claude/focused-review.json`, `~/.copilot/focused-review.json`. First found wins. If none found, defaults to `review/rules/` and `review/concerns/`.

## Technical Approach

### Architecture

```
Plugin (installed globally)
├── plugin.json                           # Manifest
├── skills/focused-review/
│   ├── SKILL.md                          # Orchestrator (5 phases)
│   ├── REFRESH.md                        # Configure + refresh flows
│   └── scripts/focused-review.py         # Python helper (deterministic work)
├── agents/
│   ├── review-runner.agent.md            # Phase 1: rule subagent
│   ├── review-consolidator.agent.md      # Phase 2: dedup + merge
│   ├── review-assessor.agent.md          # Phase 3: investigation
│   └── review-reporter.agent.md          # Phase 5: final report
└── skills/focused-review/defaults/       # Built-in rules + concerns
    ├── code-duplication.md
    ├── general-review.md
    └── concerns/
        ├── bugs.md
        ├── security.md
        └── architecture.md

Repo (version-controlled)
├── focused-review.json                   # Config (optional)
└── review/                               # Review configuration
    ├── project.md                        # Project context (optional)
    ├── rules/*.md                        # Committed rules
    └── concerns/*.md                     # Committed concerns

Working directory (gitignored, ephemeral)
└── .agents/focused-review/
    ├── diff.patch
    ├── chunks/
    ├── dispatch.json
    ├── concern-dispatch.json
    ├── findings/
    ├── assessments/
    ├── consolidated.md
    ├── assessed.md
    └── review-{timestamp}.md
```

### Division of Labor

- **Python** — deterministic work: git operations, diff generation, file discovery, chunking, dispatch planning, concern session launching
- **LLM orchestrator (SKILL.md)** — pipeline coordination: reads dispatch plans, launches agents, assembles outputs
- **LLM subagents** — semantic work: rule checking, concern exploration, consolidation, assessment, reporting
- **LLM generation (REFRESH.md)** — rule extraction, comparison, concern generation, project context drafting

### Key Design Principles

- **Everything is committed** — rules, concerns, project context live in the repo. No hidden state, no "works on my machine."
- **Orchestrator stays context-lean** — Python produces dispatch plans with file paths only. Subagents load their own content. Orchestrator never reads rule/diff/concern content.
- **One rule per agent, one concern per session** — isolation prevents cross-contamination of findings.
- **Discovery optimizes for recall, assessment for precision** — cast a wide net, then filter rigorously.
- **Pipeline degrades gracefully** — if consolidation fails, reporter uses raw findings. If assessment fails, reporter uses consolidated. Always produces a report.
- **Review is read-only** — the pipeline reports findings but never modifies source files.
- **Refresh is explicit** — no auto-generation, no magic. User runs refresh when they want to sync.

## Decisions

- **Rules are version-controlled** — live in `review/` alongside code, reviewed in PRs
- **Default rules directory is `review/`** at repo root — simple, visible, configurable via `focused-review.json`
- **Concerns use `copilot -p`** — full CLI sessions with all tools for deep exploration
- **Built-in defaults shipped in plugin** — bugs, security, architecture concerns + general-review, code-duplication rules
- **Project context is optional** — system works without it, assessment quality improves with it
- **Model selection favors `inherit`** — rules inherit the user's model by default. Only mechanical checks downgrade to `haiku`
- **Post-mortem is suggest-only** — traces cause of false positives but does not edit files directly
- **`full` scope skips assessment** — no diff to assess against; consolidated findings treated as Confirmed
- **Path filters use git pathspecs** — `--path` args are passed to `git diff`/`git ls-files` directly; globs are wrapped in `:(glob)` syntax for correct matching
- **Assessment investigates, not just challenges** — builds pro AND counter-arguments, reads source rules/concerns/project context
- **Severity gates proportionality** — Critical/High findings reported regardless of fix cost. Low-severity findings with disproportionate fix cost are filtered as noise
- **Markdown throughout** — no JSON interchange between phases, filenames encode provenance
- **Windows path compatibility required** — primary dev environment is Windows

## Verification Strategy

See `docs/spec/benchmarks.md` for benchmark PRs and quality targets.

## Related Specs

- `docs/spec/benchmarks.md` — Benchmark PRs, verification tiers, quality targets
- `docs/spec/concern-reliability.md` — Timeout/continuation architecture for concern agents
- `docs/spec/post-comments.md` — Posting findings as PR comments (GitHub + Azure DevOps)
