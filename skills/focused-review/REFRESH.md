# Refresh & Configure — Focused Review

This file handles bootstrap and refresh for both **rules** AND **concerns**. It is invoked by SKILL.md when the user runs `refresh` or `configure`.

You need these values from the calling skill (SKILL.md resolves them via `resolve-config` in Step 0 and passes them to you):
- **Script path** (`script_path`) — full path to `focused-review.py`
- **Rules directory** (`rules_dir`) — where rule files live (e.g. `review/rules/`)
- **Concerns directory** (`concerns_dir`) — where concern files live (e.g. `review/concerns/`)
- **Defaults directory** (`defaults_dir`) — built-in rules and concerns shipped with the plugin
- **Configured sources** (`sources`) — explicit source files from `focused-review.json`

---

## Configure Mode

Interactive flow to create or update a `focused-review.json` config file that controls where rules and concerns are stored.

### Step 1: Detect platform

Check if the `COPILOT_CLI` environment variable is set:
- **Set** → running under Copilot CLI
- **Not set** → running under Claude Code

### Step 2: Ask for rules directory

Tell the user the current resolved rules directory and ask what it should be. If the user presses Enter or says "keep", use the current value.

### Step 3: Ask for concerns directory

Tell the user the current resolved concerns directory and ask what it should be. If the user presses Enter or says "keep", use the current value.

### Step 4: Ask where to save

Present numbered options based on platform:

**Claude Code:**
1. `.claude/focused-review.json` — project shared (version-controlled)
2. `focused-review.json` — repo root (platform-agnostic)
3. `.github/focused-review.json` — GitHub convention
4. `~/.claude/focused-review.json` — user-wide

**Copilot CLI:**
1. `focused-review.json` — repo root (platform-agnostic)
2. `.github/focused-review.json` — GitHub convention
3. `~/.copilot/focused-review.json` — user-wide

### Step 5: Write the config file

Write the config file using a Python one-liner:

```bash
python -c "import json,os; from pathlib import Path; p=Path('{chosen_path}'); p.parent.mkdir(parents=True,exist_ok=True); d=json.loads(p.read_text()) if p.is_file() else {}; d['rules_dir']='{rules_dir_value}'; d['concerns_dir']='{concerns_dir_value}'; p.write_text(json.dumps(d,indent=2)+'\n')"
```

Where `{chosen_path}` is the path selected in Step 4 (expand `~` for user-wide paths), `{rules_dir_value}` is the directory from Step 2, and `{concerns_dir_value}` is the directory from Step 3.

### Step 6: Generate project context

Determine the review root directory — the parent of `rules_dir` (e.g., if `rules_dir` is `review/rules/`, the review root is `review/`). Check if `{review_root}/project.md` already exists.

**If it exists:** Show the user the current content and ask if they want to update it. If no, skip to Step 7.

**If it doesn't exist (or user wants to update):**

Examine the repository to understand the project:

1. **Detect project type** — look at project files (`.csproj`, `package.json`, `Cargo.toml`, `go.mod`, `pyproject.toml`, `*.sln`, etc.), directory structure, and framework indicators. Determine: is this a library, web API, GUI app, CLI tool, compiler/language tool, infrastructure/DevOps, data pipeline, etc.?

2. **Identify priorities** — read any existing instruction files (CLAUDE.md, etc.) and the project structure. What does the project care about most? Consider: correctness, security, performance, clarity/readability, backward compatibility, API stability, test coverage, etc.

3. **Note domain specifics** — what are the non-obvious things a reviewer should know about this codebase? Common patterns, architectural decisions, framework guarantees, known constraints.

Draft a `project.md` and show it to the user:

```markdown
# Project Review Context

## Project Type
{what this project is — e.g., "ASP.NET Core web API with React frontend", "F# compiler and language service", "Python CLI tool for data processing"}

## Priorities (highest first)
1. {most important concern}
2. {second}
3. {third}
{keep to 3-5 items}

## Trade-off Guidance
- {when X conflicts with Y, prefer X because...}
- {when A conflicts with B, prefer A because...}
{2-4 trade-off rules}

## Domain Notes
- {things about this codebase a reviewer should know}
- {framework guarantees, architectural patterns, known constraints}
- {common false-positive patterns specific to this project}
```

Ask the user to review and edit. After confirmation, write to `{review_root}/project.md`.

### Step 7: Confirm

Tell the user:
- What was written and where (config file + project context if generated)
- If a project-shared location was chosen (`.claude/`, `focused-review.json`, `.github/`), remind them to commit the file
- If project context was generated, remind them to commit `{review_root}/project.md` too

---

## Refresh Mode

Re-scan instruction files and update both review rules and concerns in their respective directories.

### Step 1: Discover instruction files

**1a. Fast scan (Python globs)**

Run the Python helper:

```bash
python {script_path} discover --repo .
```

This outputs a JSON array of instruction file paths (relative to repo root). Start building the discovered-files list from this output.

**1b. Configured sources**

Check **Configured sources**. If the array is non-empty, add each path to the discovered-files list (skip any that do not exist on disk). These are user-specified source files from the `"sources"` array in `focused-review.json`.

**1c. Agent-assisted exploration**

Search the repo for additional files that contain **code review guidance** but are not covered by the Python glob patterns. The Python discover step catches standard locations (CLAUDE.md, copilot-instructions.md, .cursor/rules, etc.) but misses project-specific locations like `.github/skills/` directories, `docs/review/`, or custom guideline files.

Search strategy:
1. Look for candidate files in these locations (glob for `.md` files):
   - `.github/skills/**/*.md`
   - `.github/review*/**/*.md`
   - `docs/review*/**/*.md`
   - `docs/coding*/**/*.md`
   - `docs/style*/**/*.md`
   - Any `*review*guide*.md` or `*coding*standard*.md` at the repo root
2. For each candidate, **read the file** (or at least the first ~100 lines) and determine whether it contains substantive code review guidance — rules about correctness, style, conventions, patterns, security, concurrency, API design, etc.
3. **Include** files that contain actionable code review rules or guidelines that a reviewer should enforce.
4. **Exclude** files that are about: deployment, CI/CD pipelines, workflow automation, testing infrastructure setup, project management, release processes, or general documentation that does not prescribe code quality standards.
5. Add any relevant files to the discovered-files list, deduplicating against what the Python discover step already found.

After this step, tell the user what was discovered:
- List all discovered instruction files (from all three sub-steps)
- Indicate which files came from Python discovery, which from configured sources, and which from agent exploration
- If agent exploration found no additional files, say so — this is normal for repos that keep all instructions in standard locations.

If the combined discovered-files list is empty after all three sub-steps, tell the user no instruction files were found and stop.

### Step 2: Read all sources

Read **every** instruction file returned by discover. Also read:
- **All existing rule files** from `{rules_dir}*.md` (if any exist)
- **All existing concern files** from `{concerns_dir}*.md` (if any exist)

You need all three sets of content to compare what instructions say vs. what rules and concerns currently enforce.

### Step 3: Compare and categorize — Rules

Analyze the instruction files against the existing rules. Produce a categorized action plan:

- **New rules**: Instructions contain guidance that has no matching committed rule. For each, draft the full rule content in the standard format (YAML frontmatter with `autofix: false`, `model: haiku|sonnet|inherit`, `source: {instruction file}`, optional `applies-to` glob, then Markdown body with `# Rule Name`, `## Rule`, `## Why`, `## Requirements`, `## Wrong`, `## Correct` sections).

  **Choosing `model`** — default to `inherit` (uses whatever model the user runs with) and only downgrade for purely mechanical checks:
  - **inherit** (default): any rule that requires understanding context, reasoning about behavior, or making judgment calls. This includes: bug finding, correctness, concurrency, security, API design, code duplication, design patterns, architectural patterns, performance analysis, platform-specific concerns, interop, error handling, and convention enforcement that requires understanding intent. When in doubt, use `inherit`.
  - **sonnet**: rules requiring semantic understanding but not deep reasoning — style preferences, naming conventions with semantic meaning, documentation completeness checks. Use only when the rule clearly does not need the strongest available model.
  - **haiku**: purely mechanical/syntactic pattern matching — keyword presence/absence, operator usage, literal string matching (e.g. "no `mutable` keyword", "no `break`/`continue`", "use `|>` operator"). The rule can be checked by looking at tokens alone with no surrounding context needed. Very few rules qualify.

- **Updated rules**: A committed rule exists but the source instruction has changed in a way that affects the rule's requirements. Include the updated content.

- **Orphaned rules**: A committed rule whose `source` instruction file no longer exists or no longer contains the relevant guidance. These may be intentionally kept — flag but default to keeping them.

- **Unchanged rules**: A committed rule that still accurately reflects its source instruction. No action needed.

**IMPORTANT — extract ALL substantive rules.** Do NOT skip rules because they seem "subjective", "design-level", or "hard to check mechanically". Review agents are LLMs — they can evaluate style, design patterns, idiomatic usage, and architectural guidance just as well as mechanical checks. If an instruction file says to prefer a certain pattern, that becomes a rule. Skip only these categories:
- **Tooling/workflow** instructions (e.g. "run tests with pytest") — not code quality.
- **Cosmetic-only** rules — if the ONLY substance is whitespace, indentation, brace style, brace placement, blank line counts, or line length, skip it. These are enforced by formatters, not reviewers. If a rule mixes cosmetic checks with behavioral checks (e.g. "use consistent indentation AND prefer early returns"), drop the cosmetic parts and keep the behavioral ones.
- **Rules without substantive examples** — if you cannot write Wrong/Correct examples that differ in behavior or semantic meaning (not just formatting or whitespace), the rule is too vague to produce actionable findings. Skip it.
- **Purely subjective rules** — if every requirement in the rule is subjective ("ensure quality", "keep it clean", "write good code") with no concrete, testable checkpoint, skip it. A rule must have at least one requirement that a reviewer could unambiguously check against a code sample.

**Quality guidance for drafted rules:**
- **Scope rules tightly.** If a rule only applies to specific file types (test files, native code, scripts, etc.), add an `applies-to` glob. For example: `applies-to: "**/*Test*.cs"` for test-only rules, `applies-to: "**/*.{cpp,h,c}"` for native code. Broad scope produces more noise.
- **Default to `inherit`.** Most rules benefit from the strongest available model. Only downgrade to `sonnet` or `haiku` when the rule is clearly mechanical enough that a weaker model handles it equally well. Rules that require reading surrounding code for context (not just the changed lines) must be `inherit`.
- **Check model assignments before presenting.** After drafting all rules, review the model assignments as a batch. The majority of rules should be `inherit`. If more than a few rules are `haiku` or `sonnet`, re-evaluate whether those rules are truly mechanical enough to downgrade.

### Step 3b: Built-in rules

Read all `.md` files from the **Defaults directory** (top-level only, NOT the `concerns/` subdirectory). Each file is a complete rule in standard format (YAML frontmatter + Markdown body). Compare each built-in (by filename) against the existing rules in `{rules_dir}`:

- **Missing**: no matching file in `{rules_dir}` → include as a "Built-in" new rule in the proposal
- **Already present**: a file with the same name exists in `{rules_dir}` → skip (do not propose)

If `{rules_dir}` was empty (no existing rules found in Step 2), also tell the user before presenting the summary:

> "Rules will be stored in `{rules_dir}`. Run `/focused-review configure` to change the rules directory."

### Step 3c: Compare and categorize — Concerns

Analyze the instruction files against the existing concerns. The concern taxonomy is different from rules — concerns are **broad categories** (bugs, security, architecture, general) rather than single-criterion checks.

**When to extract a concern from instruction files:**

Concerns are appropriate when instruction files describe broad review focus areas that require deep codebase exploration, cross-file analysis, or nuanced judgment. Examples:
- "Pay special attention to concurrency and thread safety" → could become a concern if there isn't already a bugs concern covering concurrency
- "Review for compliance with our internal API design guidelines" → API design concern
- "Check for performance regression patterns" → performance concern

**When NOT to create a concern (use a rule instead):**
- The guidance is a single, specific, checkable criterion → that's a rule
- The guidance can be evaluated by looking at changed lines only → that's a rule
- The guidance has concrete Wrong/Correct examples → that's a rule

For instruction-derived concerns, draft the content in the standard concern format:

```yaml
---
type: concern
models: [opus]
priority: standard
source: "{instruction file}"
applies-to: "glob/pattern"    # optional
---
# Concern Name
## Role
## What to Check
## Evidence Standards
## Output Format
```

Categorize concerns the same way as rules:
- **New concerns**: Instruction files describe a review focus area with no matching concern
- **Updated concerns**: An existing concern whose source instruction has changed
- **Orphaned concerns**: A concern whose source instruction file no longer exists
- **Unchanged concerns**: A concern that still matches its source

### Step 3d: Built-in concerns

Read all `.md` files from `{defaults_dir}concerns/`. Each is a complete concern file. Compare each built-in (by filename) against existing concerns in `{concerns_dir}`:

- **Missing**: no matching file in `{concerns_dir}` → include as a "Built-in" new concern in the proposal
- **Already present**: a file with the same name exists in `{concerns_dir}` → skip (do not propose)

If `{concerns_dir}` was empty (no existing concerns found in Step 2), also tell the user:

> "Concerns will be stored in `{concerns_dir}`. Run `/focused-review configure` to change the concerns directory."

### Step 4: Present summary to user

Show the user a **single unified numbered summary** of all proposed changes for both rules and concerns. Each entry shows the name, type (rule/concern), description, and relevant metadata. Default action is to apply everything.

```
## Refresh Summary

### New rules (will be added):
1. rule-name-here — one-line description [autofix: no, model: haiku] (source: CLAUDE.md)
2. another-rule — one-line description [autofix: yes, model: sonnet] (source: .claude/CLAUDE.md)

### Built-in rules (will be added):
3. code-duplication — flag new code that duplicates existing codebase patterns [autofix: no, model: sonnet] (built-in)
4. bug-spotter — find bugs, logic errors, and correctness issues [autofix: no, model: inherit] (built-in)

### New concerns (will be added):
5. performance — detect performance regression patterns [models: opus, priority: standard] (source: CLAUDE.md)

### Built-in concerns (will be added):
6. bugs — adversarial bug finder with evidence-based analysis [models: opus, priority: high] (built-in)
7. security — vulnerability scanner tracing attack vectors [models: opus, priority: high] (built-in)
8. architecture — pattern consistency and coupling analysis [models: opus, priority: standard] (built-in)
9. general — fresh-eyes catch-all reviewer [models: opus, priority: standard] (built-in)

### Updated rules (will be updated):
10. existing-rule — what changed [autofix: no, model: haiku] (source: CLAUDE.md)

### Orphaned rules (will be kept, no source match):
11. old-rule — source file removed/changed

### Unchanged rules (no action):
- good-rule — still matches source

### Unchanged concerns (no action):
- bugs — still matches built-in

Enter numbers to INCLUDE (e.g. "1, 3, 4"), "all", or "all but 3, 5":
```

**Quality flags** — append these inline warnings to flagged rules in the summary above:
- `[! missing applies-to]` — rule text references specific file types (e.g. "test files", "*.cpp") but has no `applies-to` glob. Broad scope will produce noise.
- `[! formatting-only examples]` — Wrong and Correct examples differ only in whitespace, formatting, or cosmetic layout, not in behavior or semantic meaning. Rule will likely produce false positives.
- `[! no concrete checkpoints]` — every requirement in the rule is subjective ("ensure quality", "keep it clean") with no concrete, testable checkpoint. Rule cannot be checked unambiguously.

These flags help the user decide which rules to exclude. Do not auto-exclude flagged rules — the user decides.

Do NOT use AskUserQuestion — just output the numbered list and let the user reply freely. Interpret their response naturally (e.g. "all", "1-5", "all but 3", "1, 2, 4"). Only apply the items the user selected.

### Step 5: Apply changes

Based on the user's decisions, directly create, edit, or delete files:

**Rules** — apply to `{rules_dir}`:
- **New rules**: Create `{rules_dir}{rule-name}.md` with the drafted content
- **Updated rules**: Edit `{rules_dir}{rule-name}.md` with the updated content
- **Removed rules** (if user chose to remove orphaned ones): Delete the file
- **Unchanged/Kept**: Do nothing

**Concerns** — apply to `{concerns_dir}`:
- **New concerns**: Create `{concerns_dir}{concern-name}.md` with the drafted content
- **Updated concerns**: Edit `{concerns_dir}{concern-name}.md` with the updated content
- **Removed concerns** (if user chose to remove orphaned ones): Delete the file
- **Unchanged/Kept**: Do nothing

Each **rule file** must follow the standard format:

```yaml
---
autofix: false
model: inherit                 # inherit (default) | sonnet | haiku (deep → semantic → mechanical)
applies-to: "glob/pattern"    # optional — omit if rule applies to all files
source: "CLAUDE.md"            # which instruction file this came from
---
# Rule Name

## Rule
One-sentence summary of what this rule checks.

## Why
Why this rule matters.

## Requirements
- Concrete, checkable requirement 1
- Concrete, checkable requirement 2

## Wrong
\`\`\`
Code example showing a violation.
\`\`\`

## Correct
\`\`\`
Code example showing compliant code.
\`\`\`
```

Each **concern file** must follow the standard format:

```yaml
---
type: concern
models: [opus]
priority: standard             # high | standard
applies-to: "glob/pattern"    # optional — omit if concern applies to all files
source: "CLAUDE.md"            # which instruction file this came from
---
# Concern Name

## Role
Describe the reviewer persona and approach.

## What to Check
- Category 1: specific things to look for
- Category 2: specific things to look for

## Evidence Standards
What constitutes valid evidence for a finding.

## Output Format
The markdown template for each finding.
```

After applying changes, tell the user what was done (files created, updated, deleted in both directories) and remind them to review and commit the changes.

### Step 6: Check for project context

Determine the review root directory — the parent of `rules_dir`. Check if `{review_root}/project.md` exists.

If it doesn't exist, tell the user:

> "No project context found. Run `/focused-review configure` to generate `{review_root}/project.md` — this tells the assessor what kind of project this is and what to prioritize (e.g., correctness vs clarity, security criticality). Reviews work without it, but assessment quality improves with project context."
