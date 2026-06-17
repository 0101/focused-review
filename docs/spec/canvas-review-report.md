# Canvas Review Report

Render the focused-review final report from a single structured source into three artifacts — `review.md`, the terminal summary, and an interactive Treemon **canvas** HTML page — with optional per-finding rich detail (code snippets, before/after, SVG diagrams) and a `postMessage` action bar (fix / disregard / document).

Full rationale, alternatives, and verified runtime facts: `.agents/canvas-review-report-investigation.md`.

## Goals

1. Give review findings an interactive, scannable UI (table + accordion + action bar) instead of only a terminal table + markdown file.
2. Drive `review.md`, the terminal summary, and the canvas from **one structured source** (`records.json`) so they never drift.
3. Keep Python's role strictly **mechanical** — validate a schema-defined contract and template it; never interpret free-form prose.
4. Let the assessor attach **optional** rich visuals where they clarify a non-obvious finding, safely embedded in the canvas.
5. Degrade with no hard failure: existing `review.md` + terminal summary are always produced; canvas and rich HTML are additive.
6. Preserve existing downstream modes (`post-comments`, `post-mortem`) unchanged.

## Expected Behavior

### Pipeline (only the final compile + render stage changes)

Discovery → Consolidation → Assessment → (Rebuttal) are **unchanged** and keep emitting markdown (`consolidated.md`, per-finding `A-XX.md`, `assessed.md`). The change is at presentation:

1. **Reporter** reads `assessed.md` / `consolidated.md` / `findings/` (as today), applies rebuttal overrides, classifies verdicts, synthesizes rule-quality notes — then **writes `{run_dir}/records.json`** (an envelope, to disk) instead of hand-authoring `review.md` + the terminal summary.
2. **Python `render-review`** loads `records.json`, validates it against the schema, sanitizes any referenced detail sidecars, and renders **server-side**:
   - `review.md` (same heading/field shape as today — locked by a golden-file test),
   - the terminal summary string,
   - `.agents/canvas/focused-review.html` (always written; appears as a tab when Treemon is running).
3. **Orchestrator** relays the Python-produced terminal summary verbatim; if Treemon/canvas is available, tells the user the pane is open and stays available for actions.

### Rich detail (optional, assessor-authored)

When the `rich_html` capability is on (an early `nh3`-import check, performed **before** assessment), the **assessor** may write `{run_dir}/assessments/A-XX-detail.html` — a raw HTML/SVG fragment — **only when a visual genuinely clarifies** a non-obvious finding (code snippet with highlighted lines, before/after, call-chain or flow/timing SVG). Trivial findings get no sidecar and the canvas panel falls back to the record's text fields. Prompt guidance is explicitly **balanced** (neither "always diagram" nor "never bother").

### Canvas UI

Derived from the validated prototype (`.agents/canvas/focused-review-prototype.html`). Python pre-renders all markup server-side (no client-side rendering, no `window.__REVIEW_DATA__`):

- Summary table (terminal-styled): `#`, severity, "Found by", issue title.
- Accordion rows (one detail panel open at a time) showing description / assessment / suggestion, upgraded with the sanitized detail fragment when present.
- Confirmed / Questionable sections, each with select-all.
- Per-row checkboxes + sticky action bar (instructions input + Fix / Disregard / Document).
- Collapsible invalid-findings table and rule-quality notes.

The only JavaScript is a small **static**, **morph-safe** action-bar script (in `<head>`, document-level event delegation). Accordion uses `<details>`/`<summary>`.

### postMessage actions

The canvas posts `{ action, run_id, record_ids: [...], instructions }`:

- Actions are **namespaced** — `focused-review.fix` / `.disregard` / `.document` (avoid Treemon's reserved `navigate-canvas-doc` / `morph-complete` / `content-updated`).
- `record_ids` are **stable** `record_id`s, never positional display numbers.
- The orchestrator calls Python to **validate/expand** the action against `records.json` (confirm `run_id`, confirm ids exist, resolve to file/line/title/suggestion), then **requires human confirmation** before executing. Nothing auto-runs.
- `fix` → propose fixing the selected findings with the instructions. `disregard` → mark as persisted run state (re-applied across re-renders). `document` → write a tracking doc.

### Graceful degradation

| Capability | Present | Absent |
|---|---|---|
| `nh3` | Assessor authors rich HTML/SVG; Python sanitizes + embeds | Plain escaped text only (no raw HTML) |
| Treemon running | Always-written canvas shows as a live tab | Canvas file written but inert; `review.md` + terminal carry the result |

If the reporter's `records.json` fails validation: Python returns an actionable per-record error, the orchestrator retries the reporter **≤2×**, then **falls back to the legacy hand-authored markdown path** (canvas skipped). The pipeline never hard-fails on a bad serialization.

### `records.json` schema (envelope)

```json
{
  "schema_version": 1,
  "run": { "run_id": "...", "scope": "branch", "date": "...",
           "rule_count": 12, "concern_count": 4, "consolidated_count": 30,
           "confirmed": 7, "questionable": 5, "invalid": 18 },
  "rebuttal_overrides": [ { "record_id": "...", "original_severity": "...", "severity": "...", "reasoning": "..." } ],
  "rule_quality_notes": [ { "rule": "...", "observation": "...", "suggestion": "..." } ],
  "findings": [ { "record_id": "...", "assessment_id": "A-03", "display_number": 1, "title": "...",
                  "file": "...", "line": 42, "original_severity": "High", "severity": "High",
                  "fix_complexity": "moderate", "verdict": "Confirmed", "type": "rule",
                  "introduced_by": "diff", "description": "...", "assessment": "...",
                  "suggestion": "...", "provenance": [ ... ], "has_detail": true } ]
}
```

It is an envelope (not a bare array) because run metadata, rule-quality notes, and rebuttal overrides are cross-cutting. Stable `record_id` / `assessment_id` are distinct from positional `display_number`; both `original_severity` and final `severity` are kept so the renderer shows the final verdict with an audit trail.

#### Validation contract (Phase 2 — `validate_records` / `validate-records`)

`focused-review.py` validates the parsed envelope with stdlib `json` + field checks (no third-party schema lib). `validate_records(data)` returns a list of **structured per-record errors** (empty = valid; it never raises). `load_and_validate_records(path)` adds file-not-found / JSON-decode handling (both reported as one `envelope`-scoped error). The `validate-records --records PATH` subcommand prints a summary JSON to **stdout** on success (exit 0) and the structured errors as JSON to **stderr** on failure (exit 1) — Phase 3's `render-review` reuses `load_and_validate_records` and emits the same error shape.

Each error is `{scope, index, path, field, record_id, assessment_id, display_number, message}` where `scope` ∈ `envelope`/`run`/`finding`/`rebuttal_override`/`rule_quality_note`, `path` is a JSON-ish locator (e.g. `findings[2].severity`), and finding errors carry the finding's stable ids so the orchestrator can hand the reporter an actionable, per-record retry message.

Enforced rules (what the renderer depends on, so it's validated strictly):
- **Top-level**: all five keys required (`schema_version` must equal `1`; `run` object; `findings`/`rebuttal_overrides`/`rule_quality_notes` arrays — the latter two may be `[]`).
- **Strict enums**: `severity`/`original_severity` ∈ {Critical, High, Medium, Low}; `fix_complexity` ∈ {quickfix, moderate, complex}; `verdict` ∈ {Confirmed, Questionable, Invalid}; `type` ∈ {rule, concern, mixed}; `run.scope` ∈ {branch, commit, staged, unstaged, full}.
- **Stable ids**: `record_id` required, non-empty, **unique**. `assessment_id` is nullable but, when present, must be **unique** (it keys the Invalid-findings table and locates the detail sidecar). `display_number` required (int ≥ 1, **unique among Confirmed/Questionable**) for the numbered sections, optional/nullable for Invalid (which render in a separate table keyed by `assessment_id`, so an Invalid finding's number doesn't join the uniqueness set).
- **Nullable**: `assessment_id` (non-empty string or null; **required non-empty when `has_detail` is true**, since the detail sidecar is located by it) and `line` (int ≥ 0 or null). `has_detail` is a required boolean. `introduced_by` is optional (type-checked only, no enum — it's display metadata).
- **provenance**: required, non-empty array; each entry is a non-empty source-label string **or** an object with a non-empty `source` field (both give the "Found by" renderer a label).
- **Text fields** `description`/`assessment`/`suggestion`: required strings, may be `""` (avoids hard-failing on legitimately empty content).
- **Count cross-checks**: `run.confirmed`/`questionable`/`invalid` must equal the verdict tallies in `findings[]` (suppressed when any finding has a malformed verdict, so the reporter sees the real per-finding error instead of a misleading count error), and `run.consolidated_count` must equal `len(findings)` — together these catch reporter miscounts and **truncated JSON** (a serialization failure mode the spec calls out).

## Security Model

Reviewed content is **untrusted** (diffs may come from untrusted PR contributors). Treemon serves the canvas over `http://127.0.0.1:5002` in an iframe with `sandbox="allow-scripts allow-same-origin allow-forms allow-popups"`; the parent app is a different origin (`localhost:5000`), so injected JS cannot reach the parent DOM — `postMessage` (origin-validated, `action` string required, 64 KB cap) is the only channel. Treemon injects its own inline `<script>` tags and sets no CSP header. Defenses:

- **`html.escape`** every structured text field.
- **`nh3`** sanitizes each detail sidecar (allowlist tags/attrs incl. an SVG allowlist; strip `<script>`/`on*`/`javascript:`/`animate`/`set`/`foreignObject`/external `href`; keep inline `style`). **Fail-closed**: no `nh3` → plain escaped text only, never raw HTML.
- **CSP `<meta>`**: `script-src`/`style-src` must be `'unsafe-inline'` (Treemon injects inline scripts; template + sidecars use inline styles), so CSP cannot restrict scripts — that is `nh3`'s job. CSP locks `img-src`/`font-src`/`connect-src` to `'self' data:`, blocking the `url()`/beacon/fetch **exfil** vector for HTML and SVG alike. Split of duties: **CSP = exfil, `nh3` = script.**
- **postMessage = privilege boundary**: namespaced actions, stable id validation via Python, **human confirmation** before any execution.

## Key Design Decisions

- **Reporter changes output format, not role.** It still does the semantic compile work; only serialization flips markdown → `records.json`. The substantive agent change.
- **Assessor change is additive.** Optional sidecar + `rich_html` flag; its primary `A-XX.md` markdown is unchanged, so `assessed.md` / rebuttal / `post-mortem` / `post-comments` keep working.
- **No Python parsing of prose.** Python validates/templates a schema-defined contract (`records.json`) and renders markdown/HTML; it never scrapes `consolidated.md`/`assessed.md`. Intermediate markdown files stay as agent inputs and the human trail.
- **Canvas write is unconditional.** Always write the file (gitignored, inert when Treemon is down); an LLM can't reliably introspect skill presence, so the best-effort skill check only gates messaging/awaiting.
- **No client-side rendering.** Python builds all markup server-side; the only JS is the static, morph-safe action bar.
- **Rich detail = sanitized raw HTML/SVG**, not a block-language. SVG allowed via the `nh3` allowlist; inline styles allowed (CSP covers exfil) — no separate CSS sanitizer.
- **Downstream keeps reading `review.md`.** `post-comments`/`post-mortem` are LLM readers; migrating to `records.json` wouldn't reduce prompt-injection (content reaches the LLM either way) and only hardens structural forgery, which an LLM resists. Lock `review.md`'s shape with a golden-file test instead.
- **`nh3` ships out the gate**, fail-closed.

## Technical Approach

### `skills/focused-review/scripts/focused-review.py` — new `render-review` subcommand (mechanical)

1. Load `{run_dir}/records.json`; **validate** against the schema (stdlib `json` + field checks); emit a structured per-record error on failure.
2. **Sanitize** each referenced `A-XX-detail.html` via `nh3` (capability-detected, fail-closed).
3. **Render** `review.md`, the terminal summary, and `.agents/canvas/focused-review.html` (template + injected pre-rendered HTML), escaping all text via `html.escape`. Embed `run_id`.
4. Plus a small **capability / validate-expand** surface the orchestrator calls: a `capabilities` subcommand reporting `nh3` availability up front, and a `validate-action` subcommand that validates/expands a `postMessage` action against `records.json` (confirm `run_id` matches, confirm each `record_id` exists, resolve to file/line/title/suggestion; reject forged `run_id` or unknown ids).

### HTML template — `skills/focused-review/templates/review-canvas.html`

Static, version-controlled page shell (head, CSS class palette for detail HTML, CSP `<meta>`, morph-safe action-bar script) with placeholders Python fills with pre-rendered rows/accordions/panels.

**Placeholder contract** (built in Phase 1; `render-review` fills it). Block-injection sites are **HTML comments** — `html.escape` turns `<` into `&lt;`, so escaped finding text can never forge a `<!-- FR:* -->` marker:

- `{{RUN_ID}}` — scalar in `<body data-run-id>`. Fill **first** (before injecting escaped content) so finding text can't forge it; a single-pass regex substitution over the whole set is safest.
- `<!-- FR:META -->`, `<!-- FR:SUMMARY_BADGES -->` — header meta line + count badges.
- `<!-- FR:CONFIRMED_COUNT -->` / `<!-- FR:CONFIRMED_ROWS -->`, `<!-- FR:QUESTIONABLE_COUNT -->` / `<!-- FR:QUESTIONABLE_ROWS -->` — section counts + one `.finding` block per finding.
- `<!-- FR:INVALID_SUMMARY -->` / `<!-- FR:INVALID_ROWS -->`, `<!-- FR:QUALITY_SUMMARY -->` / `<!-- FR:QUALITY_NOTES -->` — collapsibles.

Each `.finding` block is `checkbox + <details class="finding-d" name="findings">` where the checkbox sits **outside** `<summary>` (so selecting never toggles the accordion) and `name="findings"` makes the accordion natively exclusive (one open at a time). The exact block shape is documented in the template head comment and demonstrated in `review-canvas.fixture.html`. Accordion + the invalid/quality collapsibles are pure-CSS `<details>`/`<summary>` (no JS). The only script is the morph-safe action bar: `<head>`, document-level delegation (`click`/`change`, plus capture-phase `toggle`), holding selection/disregard/open state in JS keyed by `record_id` and re-applying it on a defensive `morph-complete` window-message/DOM-event. On each restore it **prunes** that state to the `record_id`s still rendered, so a finding that leaves the selectable set across a morph (e.g. re-categorized invalid → no checkbox) can't desync the selection count, leave action buttons enabled with nothing visible, or leak an off-screen id into a payload. It posts `{ action: "focused-review.*", run_id, record_ids[], instructions }` to `window.parent`.

The hand-filled `review-canvas.fixture.html` is the Phase-1 browser-test fixture (also a useful render target for Phase 3 golden tests); `test_review_canvas_template.py` locks the structural contract (placeholders, CSP directives, palette classes, namespaced actions, no client-side data blob, no inline handlers).

### Agents

- `agents/review-reporter.agent.md` — write `records.json` envelope to disk (keep verdicts, rebuttal overrides, rule-quality notes).
- `agents/review-assessor.agent.md` — optional `A-XX-detail.html` sidecar (primary markdown unchanged) + CSS-palette/balanced guidance, gated on `rich_html`.

### Orchestrator — `skills/focused-review/SKILL.md`

Early `nh3`/`rich_html` detection before assessment; always-write canvas via `render-review`; ≤2 retries then legacy markdown fallback; relay terminal summary; handle namespaced `postMessage` actions (Python validate/expand + human confirmation).

### Dependency

New optional `nh3` (Rust `ammonia` binding), declared in `pyproject.toml` and installed out the gate; fail-closed when absent.

## Related Specs

- `docs/spec/focused-review.md` — the overall plugin and pipeline.
- `docs/spec/post-comments.md` — downstream consumer of `review.md` (kept unchanged).
