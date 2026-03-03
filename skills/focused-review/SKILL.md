---
name: focused-review
description: Run parallel focused code reviews using committed review rules
argument-hint: "[branch|commit|staged|unstaged|full|refresh|configure]"
---

<!-- Resolve the Python helper path at load time. Works for plugin installs (CLAUDE_PLUGIN_ROOT), user/project skill dirs, and direct repo use. -->
**Script path:** !`python -c "import os; from pathlib import Path; pr=os.environ.get('CLAUDE_PLUGIN_ROOT',''); candidates=[Path(pr)/'skills/focused-review/scripts/focused-review.py'] if pr else []; candidates+=[Path.home()/'.claude/skills/focused-review/scripts/focused-review.py',Path('.claude/skills/focused-review/scripts/focused-review.py'),Path('skills/focused-review/scripts/focused-review.py')]; p=next((c.resolve() for c in candidates if c.exists()),None); print(p or 'ERROR_SCRIPT_NOT_FOUND')"`

<!-- Resolve the rules directory from config file. Scans .claude/focused-review.json, .focused-review.json, .github/focused-review.json, ~/.claude/focused-review.json, ~/.copilot/focused-review.json. First match wins, fallback review/. -->
**Rules directory:** !`python -c "import json,os; from pathlib import Path; locs=[Path('.claude/focused-review.json'),Path('focused-review.json'),Path('.github/focused-review.json')]+[Path(os.path.expanduser(p)) for p in ['~/.claude/focused-review.json','~/.copilot/focused-review.json']]; d=next((json.loads(f.read_text()).get('rules_dir','review/') for f in locs if f.is_file()),'review/'); d=d.replace(chr(92),'/').rstrip('/')+'/'; print(d)"`

You are the orchestrator for the focused-review plugin. You have three modes based on the argument.

## Mode Selection

Parse the user's argument (available as `$ARGUMENTS`):

- `configure` → **Configure Mode** (below)
- `refresh` → **Refresh Mode** (below)
- `branch`, `commit`, `staged`, `unstaged`, `full` → **Review Mode** with that scope
- Empty or missing → **Review Mode** with scope `branch`

---

## Configure Mode

Interactive flow to create or update a `focused-review.json` config file that controls where rules are stored.

### Step 1: Detect platform

Check if `CLAUDE_PLUGIN_ROOT` is set:
- **Set** → running under Copilot CLI
- **Not set** → running under Claude Code

### Step 2: Ask for rules directory

Tell the user the current resolved rules directory (from **Rules directory** above) and ask what it should be. If the user presses Enter or says "keep", use the current value.

### Step 3: Ask where to save

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

### Step 4: Write the config file

Write the config file using a Python one-liner (using the **Script path** resolved above for consistency):

```bash
python -c "import json,os; from pathlib import Path; p=Path('{chosen_path}'); p.parent.mkdir(parents=True,exist_ok=True); d=json.loads(p.read_text()) if p.is_file() else {}; d['rules_dir']='{rules_dir_value}'; p.write_text(json.dumps(d,indent=2)+'\n')"
```

Where `{chosen_path}` is the path selected in Step 3 (expand `~` for user-wide paths) and `{rules_dir_value}` is the directory from Step 2.

### Step 5: Confirm

Tell the user:
- What was written and where
- If a project-shared location was chosen (`.claude/`, `focused-review.json`, `.github/`), remind them to commit the file

---

## Review Mode

Run a parallel code review using committed rules from the repo's `{rules_dir}` directory (using the **Rules directory** resolved above).

### Step 1: Prepare dispatch

Determine the scope from the argument (default `branch`), then run the Python helper using the **Script path** and **Rules directory** resolved above:

```bash
python {script_path} prepare-review --repo . --scope {scope} --rules-dir {rules_dir}
```

The script will:
- Read all rule files from `{rules_dir}`
- Generate the diff for the requested scope
- Chunk large diffs at file boundaries
- Filter rules by `applies-to` globs
- Write `dispatch.json` to `.agents/focused-review/`
- Print a JSON summary to stdout on success

**Error handling:**
- **No rules found**: If the script reports no rules in `{rules_dir}`, this is likely the user's first run. Do NOT ask — tell the user "No review rules found — collecting rules from instruction files" and automatically proceed with **Refresh Mode** below. After refresh completes, re-run this prepare-review step with the same scope.
- **Other errors**: If the script prints nothing to stdout, or prints an error to stderr, or exits non-zero for any other reason, report the error to the user and stop.

### Step 2: Read dispatch plan

Read `.agents/focused-review/dispatch.json`. It contains an array of dispatch entries:

```json
[
  {
    "rule_path": "{rules_dir}sealed-classes.md",
    "model": "haiku",
    "autofix": false,
    "chunk_path": ".agents/focused-review/diff.patch",
    "chunk_index": 1,
    "total_chunks": 3,
    "scope": "branch"
  }
]
```

If the dispatch array is empty, tell the user no rules matched the changed files and stop.

**IMPORTANT: Do NOT read the rule files or diff/chunk files yourself.** The subagents will read them. You only need the paths and metadata from dispatch.json.

### Step 3: Launch parallel review agents

For **each** entry in the dispatch array, launch a `review-runner` agent **in parallel**. Each agent's prompt must contain exactly:

```
rule_path: {entry.rule_path}
chunk_path: {chunk_path_value}
scope: {entry.scope}
chunk: {chunk_index} of {total_chunks}
autofix: {entry.autofix}
```

Where:
- `chunk_path_value` is `entry.chunk_path` when not null, or `.agents/focused-review/changed-files.txt` when null (for `full` scope)
- `chunk` line: include as `{chunk_index} of {total_chunks}` when both are present (e.g. "2 of 5"). Omit the line entirely when `chunk_index` is null (single chunk or full scope).
- `autofix` line: include as `true` or `false` from the dispatch entry.

Use the model specified in each dispatch entry's `model` field (typically `haiku`). If the model is `"inherit"`, do NOT pass a model parameter to the Task tool (this inherits the parent model).

**Do NOT use `run_in_background`.** Launch agents as inline Task calls — multiple calls in one message run in parallel automatically.

**Batch size: max 12 agents per message.** If the dispatch has more than 12 entries, launch the first 12, wait for their results, then launch the next batch. Continue until all entries are dispatched.

**Keep agent prompts minimal.** The prompt must contain ONLY the fields above. Do NOT inline rule content, diff content, or review instructions into the prompt — the agent reads its own files and has its own instructions.

### Step 4: Compile report

Once all agents have completed, compile their results into a single report file at:

```
.agents/focused-review/review-{timestamp}.md
```

Where `{timestamp}` is the current date-time in `YYYYMMDD-HHmmss` format.

Report format:

```markdown
# Focused Review Report

**Scope:** {scope}
**Date:** {ISO timestamp}
**Rules checked:** {count of dispatch entries}

---

## {rule name from rule_path filename, e.g. "sealed-classes"}

{agent output verbatim — either "NO VIOLATIONS FOUND" or VIOLATION/FIXED blocks}

---

## {next rule name}

{next agent output}
```

Group results by rule (if a rule ran against multiple chunks, combine the agent outputs under one heading). Preserve the exact `VIOLATION`, `FIXED`, and `NO VIOLATIONS FOUND` output from agents — do not reformat or summarize.

After writing the report, tell the user:
- How many rules were checked
- How many violations were found (count `VIOLATION:` blocks)
- How many fixes were applied (count `FIXED:` blocks)
- The path to the full report file

---

## Refresh Mode

Re-scan instruction files and update review rules in `{rules_dir}` (using the **Rules directory** resolved above).

### Step 1: Discover instruction files

Run the Python helper using the **Script path** resolved above:

```bash
python {script_path} discover --repo .
```

This outputs a JSON array of instruction file paths (relative to repo root). If the output is empty or an error, tell the user no instruction files were found and stop.

### Step 2: Read all sources

Read **every** instruction file returned by discover. Also read **all** existing rule files from `{rules_dir}*.md` (if any exist).

You need both sets of content to compare what instructions say vs. what rules currently enforce.

### Step 3: Compare and categorize

Analyze the instruction files against the existing rules. Produce a categorized action plan:

- **New rules**: Instructions contain guidance that has no matching committed rule. For each, draft the full rule content in the standard format (YAML frontmatter with `autofix: false`, `model: haiku|sonnet|inherit`, `source: {instruction file}`, optional `applies-to` glob, then Markdown body with `# Rule Name`, `## Rule`, `## Why`, `## Requirements`, `## Wrong`, `## Correct` sections).

  **Choosing `model`** — pick the model that matches the rule's complexity:
  - **haiku**: syntactic/mechanical checks — keyword presence/absence, operator usage, structural patterns (e.g. "no `mutable`", "no `break`/`continue`", "use `|>` operator", "no `null`")
  - **sonnet**: rules requiring semantic judgment, design reasoning, or understanding intent (e.g. "simplicity over complexity", "prefer expressions over statements", code duplication detection)
  - **inherit**: rules requiring deep understanding — architectural evaluation, nuanced trade-off analysis, or holistic design review (e.g. "model domain with DUs", "simplicity over complexity" when applied to architecture-level decisions)

- **Updated rules**: A committed rule exists but the source instruction has changed in a way that affects the rule's requirements. Include the updated content.

- **Orphaned rules**: A committed rule whose `source` instruction file no longer exists or no longer contains the relevant guidance. These may be intentionally kept — flag but default to keeping them.

- **Unchanged rules**: A committed rule that still accurately reflects its source instruction. No action needed.

**IMPORTANT — extract ALL rules.** Do NOT skip rules because they seem "subjective", "design-level", or "hard to check mechanically". Review agents are LLMs — they can evaluate style, design patterns, idiomatic usage, and architectural guidance just as well as mechanical checks. If an instruction file says to prefer a certain pattern, that becomes a rule. The only instructions to skip are those that are purely about tooling/workflow (e.g. "run tests with pytest") rather than code quality.

### Step 3b: Built-in bootstrap rules (first use only)

If `{rules_dir}` was empty before this refresh (i.e. no existing rule files were found in Step 2), include the following built-in rules in the proposal alongside the rules extracted from instructions. If `{rules_dir}` already had rules, skip this step — built-ins are only offered on first use.

**First-run message:** When `{rules_dir}` is empty, before presenting the summary, tell the user:

> "Rules will be stored in `{rules_dir}`. Run `/focused-review configure` to change the rules directory."

**Built-in: code-duplication**

Use this exact content for the `{rules_dir}code-duplication.md` file:

~~~yaml
---
autofix: false
model: sonnet
source: "built-in"
---
# Code Duplication

## Rule
Flag new or changed code that duplicates logic already present in the codebase.

## Why
Duplicated code increases maintenance burden — bugs must be fixed in multiple places, and behavior diverges over time. Catching duplication at review time prevents it from accumulating.

## Requirements
- New code should not replicate logic that already exists elsewhere in the codebase
- If a diff adds code similar to an existing function/method/pattern, flag it and suggest reusing or extracting a shared abstraction
- Use grep to search the codebase for similar patterns when reviewing added code
- Minor duplication (a single repeated line, common boilerplate) is acceptable — focus on duplicated logic or algorithms

## Wrong
```
// In UserService.cs — new code in diff
public string FormatUserName(User user) =>
    $"{user.LastName}, {user.FirstName} ({user.Email})";

// Already exists in DisplayHelper.cs
public string FormatName(Person p) =>
    $"{p.LastName}, {p.FirstName} ({p.Email})";
```

## Correct
```
// Reuse the existing helper
public string FormatUserName(User user) =>
    DisplayHelper.FormatName(user);
```
~~~

### Step 4: Present summary to user

Show the user a **numbered** summary of proposed changes. Each entry shows the rule name, description, autofix status, and model. Default action is to apply everything.

```
## Refresh Summary

### New rules (will be added):
1. rule-name-here — one-line description [autofix: no, model: haiku] (source: CLAUDE.md)
2. another-rule — one-line description [autofix: yes, model: sonnet] (source: .claude/CLAUDE.md)

### Built-in rules (will be added):
3. code-duplication — flag new code that duplicates existing codebase patterns [autofix: no, model: sonnet] (built-in)

### Updated rules (will be updated):
4. existing-rule — what changed [autofix: no, model: haiku] (source: CLAUDE.md)

### Orphaned rules (will be kept, no source match):
5. old-rule — source file removed/changed

### Unchanged rules (no action):
- good-rule — still matches source

Enter numbers to INCLUDE (e.g. "1, 3, 4"), "all", or "all but 3, 5":
```

Do NOT use AskUserQuestion — just output the numbered list and let the user reply freely. Interpret their response naturally (e.g. "all", "1-5", "all but 3", "1, 2, 4"). Only apply the rules the user selected.

### Step 5: Apply changes

Based on the user's decisions, directly create, edit, or delete rule files in `{rules_dir}`:

- **New rules**: Create `{rules_dir}{rule-name}.md` with the drafted content
- **Updated rules**: Edit `{rules_dir}{rule-name}.md` with the updated content
- **Removed rules** (if user chose to remove orphaned ones): Delete the file
- **Unchanged/Kept**: Do nothing

Each rule file must follow the standard format:

```yaml
---
autofix: false
model: haiku                   # haiku | sonnet | inherit (mechanical → semantic → deep)
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

After applying changes, tell the user what was done (files created, updated, deleted) and remind them to review and commit the changes in `{rules_dir}`.
