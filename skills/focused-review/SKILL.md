---
name: review
description: Run unified code reviews through a 5-phase discovery-consolidation-assessment pipeline
argument-hint: "[branch|commit|staged|unstaged|full|refresh|configure|post-mortem|post-comments] [--no-autofix]"
---

You are the orchestrator for the focused-review plugin. Your mode depends on the argument.

## Step 0: Resolve Configuration

Before any mode-specific logic, locate the Python helper and resolve all configuration.

The Python helper script is at `scripts/focused-review.py` within this skill file's directory. Construct the full path from this skill file's location.

Run:

```bash
python {script_path} resolve-config --repo .
```

Parse the JSON output and store these values for use throughout:

- `script_path` — confirmed full path to the Python helper
- `rules_dir` — directory containing review rule files (e.g. `review/rules/`)
- `concerns_dir` — directory containing concern files (e.g. `review/concerns/`)
- `defaults_dir` — built-in defaults shipped with the plugin
- `sources` — explicit source files from `focused-review.json` (may be empty)

## Mode Selection

Parse the user's argument (available as `$ARGUMENTS`):

- First, check if `--no-autofix` is present anywhere in the arguments. If so, set `no_autofix = true` and remove it from the argument string before parsing the mode. This flag suppresses all autofix behavior — violations are reported but never fixed. Useful for CI runs, remote PR reviews, or read-only contexts.
- `configure` or `refresh` → Read `REFRESH.md` from the same directory as this skill file and follow its instructions. Pass along the resolved **Script path**, **Rules directory**, **Concerns directory**, **Defaults directory**, and **Configured sources** values.
- `post-comments` (followed by a PR URL) → Read `POST-COMMENTS.md` from the same directory as this skill file and follow its instructions. Pass along the resolved **Script path** and the **PR URL** (the remaining argument text after `post-comments`).
- `post-mortem` (with optional finding numbers) → **Post-Mortem Mode**
- `branch`, `commit`, `staged`, `unstaged`, `full` → **Review Mode** with that scope
- Empty or missing → **Review Mode** with scope `branch`

---

## Review Mode — Unified Pipeline

Five-phase pipeline: Discovery → Consolidation → Assessment → (Rebuttal) → Presentation.

Rules and concerns run in parallel during Phase 1. Subsequent phases validate and refine findings.

**Your role is orchestration only.** You do not generate diffs, discover files, read rules, or review code yourself. You run the Python script, then dispatch subagents using its output. Every step below either runs the Python script or launches subagents — if you find yourself running `git` commands or reading source files, you are doing it wrong.

### Step 1: Prepare dispatch

Run the Python helper with the scope from the argument (default `branch`):

```bash
python {script_path} prepare-review --repo . --scope {scope} {no_autofix_flag}
```

Where `{no_autofix_flag}` is `--no-autofix` if `no_autofix` was set during mode selection, or omitted entirely otherwise.

The script handles everything: git operations, diff generation, file discovery, rule matching, and chunking. It writes all artifacts to `.agents/focused-review/` and prints a JSON summary to stdout.

**Error handling:**
- **No rules found**: Tell the user "No review rules found — collecting rules from instruction files" and automatically proceed with Refresh Mode in `REFRESH.md` (same directory as this skill file). After refresh completes, re-run this prepare-review step with the same scope.
- **Other errors**: Report the error to the user and stop.

Parse the JSON summary. If `agents` is 0 and `concern_prompts` is 0, tell the user no rules matched and no concerns apply, and stop.

### Step 2: Phase 1 — Discovery (parallel)

Read `.agents/focused-review/dispatch.json` (rule dispatch) and `.agents/focused-review/concern-dispatch.json` (concern dispatch).

**IMPORTANT: Do NOT read the rule files, diff/chunk files, or concern prompt files yourself.** The subagents and concern runner read their own files. You only need the metadata from dispatch.json (paths, model, autofix flag) to construct the agent prompts — never `view` or `cat` the rule or diff content.

**In a single response**, launch all of the following in parallel:

**Rule agents** — For each entry in `dispatch.json`, launch a `review-runner` Task agent. Each agent's prompt must contain exactly:

```
rule_path: {entry.rule_path}
chunk_path: {chunk_path_value}
scope: {entry.scope}
chunk: {chunk_index} of {total_chunks}
autofix: {entry.autofix}
findings_path: .agents/focused-review/findings/rule--{rule-name}.md
```

Where:
- `chunk_path_value` is `entry.chunk_path` when not null, or `.agents/focused-review/changed-files.txt` when null (for `full` scope)
- `chunk` line: include as `{chunk_index} of {total_chunks}` when both are present. Omit the line entirely when `chunk_index` is null.
- `autofix` line: include as `true` or `false` from the dispatch entry.
- `{rule-name}` is the rule filename without extension from `rule_path` (e.g. `null-handling` from `review/rules/null-handling.md`). When a rule has multiple chunks, append the chunk index: `rule--null-handling--2.md`.

Use the model specified in each entry's `model` field. If `"inherit"`, pass **your own model** (the model you are currently running as — check your system prompt's `<model>` tag for the `id` attribute) to the Task tool's `model` parameter. This ensures subagents run at the same quality level as the orchestrator.

**Concern runner** — If `concern-dispatch.json` has entries, start the Python concern runner **in the same response** as the rule agent launches. Use the `powershell` tool with `mode="sync"` and `initial_wait: 300` (concern sessions can take several minutes):

```bash
python {script_path} run-concerns --repo .
```

This launches `copilot -p` sessions in parallel via ThreadPoolExecutor. It reads `concern-dispatch.json` internally and writes findings to `.agents/focused-review/findings/concern--{name}--{model}.md`.

**Batching**: Max 12 Task agents per message. Include the `run-concerns` bash command in the same response as the first batch of rule agents. If rules need multiple batches (>12 entries), the concern runner is already running from the first batch — subsequent batches only contain rule agents.

Wait for all rule agents and the concern runner to complete before proceeding.

If `dispatch.json` is empty (no rules matched), only run concerns. If `concern-dispatch.json` is empty (no concerns), only run rules.

Rule agents write their findings directly to `.agents/focused-review/findings/`. The concern runner does the same. After all agents complete, verify the findings directory has the expected files before proceeding.

### Step 3: Phase 2 — Consolidation

Launch a `general-purpose` Task agent with this prompt:

```
Read and follow the agent profile at agents/review-consolidator.agent.md

findings_dir: .agents/focused-review/findings
```

This agent reads all finding files from Phase 1 (`rule--*.md` and `concern--*.md`), deduplicates semantically (same location + same issue = one finding), merges provenance, and writes `.agents/focused-review/consolidated.md` with up to 30 prioritized findings.

Wait for completion. If the agent fails, report the error and skip to Step 6 with whatever findings are available. If the consolidated report shows 0 findings, skip to Step 6 and report "no findings".

### Step 4: Phase 3 — Assessment

**Skip this step for `full` scope** (no diff to assess against). Proceed directly to Step 6 using the consolidated report.

Launch a `general-purpose` Task agent with this prompt:

```
Read and follow the agent profile at agents/review-assessor.agent.md

consolidated_path: .agents/focused-review/consolidated.md
diff_path: .agents/focused-review/diff.patch
rules_dir: {rules_dir}
```

This agent validates each finding: checks if truly introduced by the diff, constructs adversarial counter-arguments, and assigns verdicts (Confirmed / Questionable / Invalid). Writes `.agents/focused-review/assessed.md`.

Wait for completion. If the agent fails, report the error and skip to Step 6 using the consolidated report as the data source.

### Step 5: Phase 4 — Rebuttal (optional)

**Skip this step for `full` scope** (no diff to assess against).

Read `.agents/focused-review/assessed.md`. Find any findings with **Severity Critical or High** that received a verdict of **Invalid**.

If none exist, skip to Step 6.

For each such finding, launch a `general-purpose` Task agent (launch all rebuttals in parallel in a single response):

```
You are a rebuttal agent. A high-priority finding was assessed as Invalid. Challenge this assessment.

Read the assessed finding and the diff. Construct arguments for why this finding IS valid despite the assessor's counter-arguments. Consider edge cases, race conditions, subtle interactions, and error paths the assessor may have missed.

Finding ID: {A-XX}
File: {file path from finding}
Title: {title from finding}
Description: {description from finding}
Assessment reasoning: {assessment reasoning from finding}
Counter-arguments: {counter-arguments from finding}

Diff path: .agents/focused-review/diff.patch

Write your rebuttal to .agents/focused-review/rebuttals/{A-XX}.md with:
- Your counter-counter-arguments
- Whether the assessor's dismissal holds up under scrutiny
- Final recommendation: Reinstate (with what severity) or Uphold Invalid
```

After all rebuttals complete, read each rebuttal file. For any finding where the rebuttal recommends "Reinstate", note the finding ID and reinstated severity — you will apply these overrides when compiling the report in Step 6.

### Step 6: Phase 5 — Presentation

Determine the data source and type (check in order, use the first that exists):
- If `.agents/focused-review/assessed.md` exists → `data_source_type: assessed`
- Else if `.agents/focused-review/consolidated.md` exists → `data_source_type: consolidated`
- Else → `data_source_type: raw_findings` (path: `.agents/focused-review/findings`)

Build the `rebuttal_overrides` value: if Step 5 produced any "Reinstate" recommendations, format as a JSON list of `{"id": "A-XX", "severity": "High", "reasoning": "..."}`. Otherwise omit the line.

Launch a `general-purpose` Task agent:

```
Read and follow the agent profile at agents/review-reporter.agent.md

data_source: {path to assessed.md, consolidated.md, or findings/}
data_source_type: {assessed|consolidated|raw_findings}
report_path: .agents/focused-review/review-{timestamp}.md
scope: {scope}
rule_count: {rule_count}
concern_count: {concern_count}
rebuttal_overrides: {JSON list or omit}
```

Where `{timestamp}` is `YYYYMMDD-HHmmss`.

Wait for completion. **Relay the agent's text output directly to the user — copy-paste it verbatim.** Do not read the report file. Do not rephrase, reformat, or create your own summary table. The reporter agent's output already contains the report path, pipeline stats, findings table with "Found by" column, and rule quality notes. Your only job is to pass it through.

---

## Post-Mortem Mode — Trace Invalid Findings

User-triggered analysis after reviewing a report. The user marks findings they consider invalid. The system traces each back to its source rule or concern via provenance, analyzes the false-positive pattern, and suggests specific adjustments. **Suggest-only** — does not edit rule or concern files.

### Step 1: Locate latest report

Find the most recent `review-*.md` file in `.agents/focused-review/`:

```bash
python -c "from pathlib import Path; files=sorted(Path('.agents/focused-review').glob('review-*.md')); print(str(files[-1]).replace(chr(92),'/') if files else 'NONE')"
```

If NONE, tell the user no review report exists and stop.

Read the report file.

### Step 2: Identify invalid findings

Parse the arguments after `post-mortem` for finding numbers. Accept comma-separated, space-separated, or mixed (e.g., `1,3,5` or `1 3 5` or `1, 3, 5`).

If no numbers provided, present the numbered summary table from the report and ask the user which findings they consider invalid. Wait for their response before continuing.

For each specified number, locate the corresponding finding in the report (match the `### {n}.` heading in the Confirmed or Questionable sections). Extract:
- **Title** and **severity**
- **File** path
- **Provenance** line (e.g., `rule:sealed-classes, concern:bugs (opus)`)
- **Description** and **assessment reasoning**

If a number doesn't match any finding, warn the user and skip it.

### Step 3: Trace provenance to sources

Parse each finding's **Provenance** field into individual sources. Each source follows one of these formats:
- `rule:{name}` → source file is `{rules_dir}/{name}.md`
- `concern:{name} ({model})` → source file is `{concerns_dir}/{name}.md`, falling back to `{defaults_dir}/concerns/{name}.md`

Build a map: **source → list of findings** traced to it.

For each unique source, read the source file. If the file doesn't exist, note it as "source removed or renamed" and still include it in the analysis (the finding provenance itself is sufficient context).

### Step 4: Analyze patterns and generate recommendations

For each source that produced one or more invalid findings, analyze the false-positive pattern:

**For rule sources:**
- Read the rule's `## Rule`, `## Requirements`, and `## Wrong` / `## Correct` sections
- Compare the invalid findings against the rule definition: what did the rule flag that shouldn't have been flagged?
- Classify the root cause:
  - **Scope too broad**: rule's `applies-to` glob matches files where the rule doesn't apply → suggest narrowing the glob
  - **Missing exclusion**: rule is generally valid but misses a legitimate exception pattern → suggest adding to Requirements section (e.g., "Exclude factory methods" or "Skip when X pattern is present")
  - **Vague criteria**: rule definition is ambiguous, causing the agent to over-flag → suggest tightening the rule description with concrete boundaries
  - **Model mismatch**: rule requires deep semantic understanding but runs on a fast model → suggest changing model to `sonnet` or `inherit`
  - **Fundamentally noisy**: rule produces multiple false positives in this review, suggesting it is fundamentally noisy → suggest removal

**For concern sources:**
- Read the concern's `## What to Check` and `## Evidence Standards` (including its Anti-patterns to avoid list)
- Compare the invalid findings against the concern prompt: what was flagged without sufficient evidence?
- Classify the root cause:
  - **Missing anti-pattern**: concern doesn't warn against this class of false positive → suggest adding a specific anti-pattern entry
  - **Evidence standards too loose**: concern accepted weak evidence for this type of finding → suggest strengthening the evidence requirement
  - **Scope too broad**: concern checks an area that doesn't apply to this codebase → suggest adding `applies-to` restriction
  - **Prompt too aggressive**: concern's role description encourages over-flagging → suggest softening language (e.g., "prove it's broken" instead of "find potential issues")

### Step 5: Write recommendation report

Write to `.agents/focused-review/post-mortem-{timestamp}.md` where `{timestamp}` is `YYYYMMDD-HHmmss`:

```markdown
# Post-Mortem: Invalid Finding Analysis

**Date:** {ISO timestamp}
**Review report:** {report filename}
**Findings analyzed:** {count}

---

## Source Analysis

### {Rule|Concern}: {source name}

**Invalid findings traced here:** {count}
{for each finding:}
- Finding #{n}: [{severity}] {title} (`{file}`)

**Pattern:** {what these false positives have in common — 1-2 sentences}

**Root cause:** {category from Step 4}

**Recommendation:** {specific, actionable suggestion — 1-3 sentences describing what to change}

**Suggested edit:**
> In `{source file path}`, {section to modify}:
> ```
> {show the specific text to add or change}
> ```

---

{repeat for each source}

## Summary

| Source | Type | Invalid Findings | Root Cause | Action |
|--------|------|-----------------|------------|--------|
| {name} | rule/concern | {count} | {root cause} | {one-line action} |
```

### Step 6: Present results

Tell the user:

1. How many findings were traced and how many unique sources identified
2. The summary table from the recommendation report
3. The path to the full post-mortem report
4. Remind: "These are suggestions only. To apply changes, use `/focused-review refresh` and incorporate the recommendations, or edit the rule/concern files directly."
