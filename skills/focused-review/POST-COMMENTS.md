# Post Comments — Post Review Findings to PR

This file handles posting focused-review findings as inline PR comments on GitHub or Azure DevOps. It is invoked by SKILL.md when the user runs `post-comments`.

You need these values from the calling skill (SKILL.md resolves them via `resolve-config` in Step 0 and passes them to you):
- **Script path** (`script_path`) — full path to `focused-review.py`
- **PR URL** (`pr_url`) — the PR URL extracted from the arguments

---

## Step 1: Parse PR URL

Run:

```bash
python {script_path} parse-pr-url --url "{pr_url}"
```

Parse the JSON output. Store:
- `platform` — `"github"` or `"ado"`
- `owner` (GitHub) or `org` + `project` (ADO)
- `repo`
- `pr_number`

If the command fails, report the error to the user and stop. Common errors:
- Unrecognised URL format → tell the user which formats are supported (see below)

Supported formats:
```
# GitHub
https://github.com/{owner}/{repo}/pull/{number}
https://github.com/{owner}/{repo}/pull/{number}/files
https://github.com/{owner}/{repo}/pull/{number}/commits

# Azure DevOps
https://dev.azure.com/{org}/{project}/_git/{repo}/pullrequest/{id}
https://{org}.visualstudio.com/{project}/_git/{repo}/pullrequest/{id}
```

## Step 2: Locate latest review report

Find the most recent review report across all run directories in `.agents/focused-review/`:

```bash
python -c "from pathlib import Path; dirs=sorted(d for d in Path('.agents/focused-review').iterdir() if d.is_dir()); reports=[(d / 'review.md') for d in dirs if (d / 'review.md').exists()]; print(str(reports[-1]).replace(chr(92),'/') if reports else 'NONE')"
```

- If `NONE`, tell the user: "No review report found. Run `/focused-review branch` first to generate a review, then re-run post-comments."
- If multiple run directories contain review reports, list them and ask the user which one to use. Wait for their response.

Store the chosen report path as `report_path`.

## Step 3: Get user identity

Run:

```bash
python {script_path} get-pr-user --platform {platform}
```

Parse the JSON output. Store:
- `username` — login name (e.g. `"octocat"`)
- `display_name` — human-readable name

If the command fails, report the error:
- `gh` CLI not found → "Install `gh` CLI and run `gh auth login`"
- `az` CLI not found → "Install Azure CLI and run `az login`"
- Not authenticated → "Run `gh auth login` (or `az login` for ADO) first"

## Step 4: Read report and classify findings

### 4a: Read the review report

Read `{report_path}` natively. Extract all findings from the three finding sections — **Confirmed Findings**, **Needs Your Decision**, and **Pre-existing**. A section is absent when it has no findings; read whichever of the three are present and do **not** stop at the first one. (Never look for a `Questionable Findings` section — that heading no longer exists; the questionable-verdict findings live under `Needs Your Decision`. Reading only a subset of these sections silently drops findings.)

For each finding, extract:
- **ID** (`f#`) — the finding id from the `### F{n}.` heading (e.g. `F2`). Store it lowercase (`f2`); it is globally unique across all sections and is the key used for inline-comment identity and exclusion.
- **Severity** — from `[{severity}]` in the heading (Critical, High, Medium, Low)
- **Title** — from the heading text after severity
- **File path** — from `**File:** \`{path}:{line}\`` line
- **Line number** — from the same `**File:**` line
- **Fix complexity** — from `**Fix complexity:**` line
- **Found by** — from `**Found by:**` line (provenance)
- **Description** — the body text
- **Assessment** — from `> **Assessment:**` blockquote
- **Suggestion** — from `**Suggestion:**` text
- **Section** — `Confirmed`, `Needs Your Decision`, or `Pre-existing`, based on which `## ` section heading the finding appears under. This is the finding's verdict/section label.

### 4b: Read the diff

Read the `diff.patch` from the same run directory as the report (e.g. if report is at `.agents/focused-review/20260402-103000/review.md`, read `.agents/focused-review/20260402-103000/diff.patch`).

Parse the diff to build a map of which files and line ranges are covered by the PR diff. For each diff hunk, record:
- File path (the `+++ b/{path}` line, without the `b/` prefix)
- Line ranges from `@@ ... +{start},{count} @@` hunks — the new-file line range `[start, start+count-1]`

### 4c: Classify findings

**Pre-existing findings always go in the overall review body, never inline** — regardless of whether their line falls inside the diff. They describe code that predates this PR, so an inline comment would wrongly imply the change introduced the issue; they are surfaced in the review body alongside the out-of-diff findings (and are never suppressed entirely). Classify every `Pre-existing` finding as `out_of_diff`.

For each `Confirmed` and `Needs Your Decision` finding, check whether its file path and line number fall within any diff hunk:
- **In-diff**: file path matches a diff file AND line number falls within a `+` hunk range → this finding can be posted as an inline comment
- **Out-of-diff**: file path not in diff, or line number outside any hunk range → this finding goes in the overall review body

Store each finding with its classification (`in_diff` or `out_of_diff`).

## Step 5: Format comment bodies

### Severity emoji mapping

| Severity | Emoji |
|----------|-------|
| Critical | 🔴 |
| High | 🔴 |
| Medium | 🟡 |
| Low | 🔵 |

### Inline comment body (for in-diff findings)

Format each in-diff finding as:

```markdown
### {emoji} [{severity}] {title}

`{file_path}:{line}` — {description (first sentence or brief summary)}

**Suggestion:** {suggestion}

---
<sub>Generated by [focused-review](https://github.com/0101/focused-review), approved by @{username}</sub>
```

Keep the description concise— use the first sentence or a brief summary of the full description. The reader can see the code context in the PR diff.

### Out-of-diff finding body (for overall review body)

Format each out-of-diff finding as:

```markdown
### {emoji} [{severity}] {title}

**File:** `{file_path}:{line}`

{full description}

**Suggestion:** {suggestion}
```

### Overall review body

Build the review body that accompanies the inline comments:

```markdown
## Focused Review Summary

| F# | Severity | File | Finding |
|----|----------|------|---------|
{for each finding (Confirmed, Needs Your Decision, and Pre-existing — in F# order):}
| {F#} | {emoji} {severity} | `{file_path}:{line}` | {title} |

{if there are out-of-diff findings (includes all Pre-existing findings):}
---

### Findings in Review Body

These findings are reported here rather than as inline comments — either they reference code outside the changed lines, or they are Pre-existing (predating this PR).

{each out-of-diff finding formatted as above}

---
<sub>Generated by [focused-review](https://github.com/0101/focused-review), approved by @{username}</sub>
```

If there are no out-of-diff findings, omit the "Findings in Review Body" section entirely — just include the summary table and the attribution footer.

## Step 6: Write `comments.json`

Write the file to the same run directory as the report (e.g. `.agents/focused-review/20260402-103000/comments.json`) using the `create` tool (or `edit` if it already exists).

### Schema for GitHub

```json
{
  "platform": "github",
  "owner": "{owner}",
  "repo": "{repo}",
  "pr_number": {pr_number},
  "review_body": "## Focused Review Summary\n...",
  "inline_comments": [
    {
      "id": "f1",
      "path": "src/foo.cs",
      "line": 42,
      "body": "### 🔴 [High] Null reference risk\n..."
    }
  ]
}
```

### Schema for ADO

```json
{
  "platform": "ado",
  "org": "{org}",
  "project": "{project}",
  "repo": "{repo}",
  "pr_number": {pr_number},
  "review_body": "## Focused Review Summary\n...",
  "inline_comments": [
    {
      "id": "f1",
      "path": "src/foo.cs",
      "line": 42,
      "body": "### 🔴 [High] Null reference risk\n..."
    }
  ]
}
```

### Field reference

| Field | Type | Description |
|-------|------|-------------|
| `platform` | string | `"github"` or `"ado"` |
| `owner` | string | GitHub repository owner (GitHub only) |
| `org` | string | Azure DevOps organization (ADO only) |
| `project` | string | Azure DevOps project (ADO only) |
| `repo` | string | Repository name |
| `pr_number` | integer | Pull request number/ID |
| `review_body` | string | Markdown body for the overall review comment |
| `inline_comments` | array | List of inline comment objects |
| `inline_comments[].id` | string | Finding `f#` id from the report (e.g. `"f2"`), globally unique across all sections |
| `inline_comments[].path` | string | File path relative to repo root (no leading slash) |
| `inline_comments[].line` | integer | Line number in the new version of the file |
| `inline_comments[].body` | string | Full markdown body of the inline comment |

**Important:** The `inline_comments` array contains ONLY in-diff Confirmed/Needs-Your-Decision findings. Out-of-diff findings — and **all** Pre-existing findings — are included in `review_body` only.

**Important:** The `path` field must be a repo-relative path (e.g. `src/foo.cs`), not an absolute path. Strip any leading `./` or `/`.

**Important:** The `id` field must be the finding's `f#` id from the review report (lowercase, e.g. `"f2"`). It is globally unique, so the user/script can reference a specific finding unambiguously during the exclusion step (the script matches it case-insensitively, so `F2` and `f2` both resolve).

## Step 7: Preview and user approval

Present the findings to the user in a table:

```
## Comment Preview

Will post to: {platform} PR #{pr_number} ({owner}/{repo} or {org}/{project}/{repo})
Posting as: @{username} ({display_name})
Report: {report_path}

### Inline Comments ({count})

| F# | Severity | File | Finding |
|----|----------|------|---------|
| f1 | 🔴 High | `src/foo.cs:42` | Null reference risk |
| f2 | 🟡 Medium | `src/bar.cs:88` | Missing error handling |

### Overall Review Body
{if out-of-diff findings exist:}
Includes summary table + {count} review-body findings (out-of-diff + Pre-existing)

{if no out-of-diff findings:}
Includes summary table only

---

Enter "post" to post all comments, or "post all but f2, f5" to exclude specific findings by their F# id.
To cancel, enter "cancel".
```

Wait for the user's response. Parse their input:
- `"post"` or `"post all"` → post everything
- `"post all but f#, f#, ..."` or `"exclude f#, f#"` → exclude the listed finding ids (the F#/f# tokens; case does not matter)
- `"cancel"` or `"no"` → stop without posting

If the user excludes findings, remove those entries from the inline_comments array. If an excluded finding was out-of-diff, also remove it from the review_body. Re-write `comments.json` with the updated data.

## Step 8: Post comments

Run:

```bash
python {script_path} post-comments --comments {run_dir}/comments.json
```

If the user excluded findings, pass the excluded `f#` ids (comma-separated, e.g. `f2,f5`):

```bash
python {script_path} post-comments --comments {run_dir}/comments.json --exclude {comma_separated_f_ids}
```

Wait for the command to complete. Parse the output (JSON with posting results).

If the command fails:
- CLI not installed → report which CLI is needed
- Auth failure → tell user to authenticate
- Partial failure (ADO) → report which comments succeeded and which failed
- Complete failure → report the error

## Step 9: Report results

Tell the user:

```
## ✅ Comments Posted

Posted {n} inline comments + review summary to {platform} PR #{pr_number}
{if excluded:} ({excluded_count} findings excluded by user)
{if out_of_diff:} ({out_of_diff_count} out-of-diff findings included in review body)

PR: {pr_url}
```

If there were any failures (partial ADO failure), list them:

```
## ⚠️ Partial Success

Posted {success_count}/{total_count} comments.

Failed:
- Finding {f#}: {error message}
```
