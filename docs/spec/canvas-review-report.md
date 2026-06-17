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
- The channel is **origin-pinned**: the outbound post targets the trusted parent origin (never `"*"`) and the inbound `message` listener (Treemon's morph-restore signal) validates `event.origin` + `event.source`, so a hostile framing parent can neither read the `run_id`/`instructions` payload nor drive the re-render path. The origin is threaded into the template via `{{PARENT_ORIGIN}}` (`render-review --parent-origin`, default `http://localhost:5000`).
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

Reviewed content is **untrusted** (diffs may come from untrusted PR contributors). Treemon serves the canvas over `http://127.0.0.1:5002` in an iframe with `sandbox="allow-scripts allow-same-origin allow-forms allow-popups"`; the parent app is a different origin (`localhost:5000`), so injected JS cannot reach the parent DOM — `postMessage` (origin-validated against the trusted parent origin `http://localhost:5000`, `action` string required, 64 KB cap) is the only channel. Treemon injects its own inline `<script>` tags and sets no CSP header. Defenses:

- **`html.escape`** every structured text field.
- **`nh3`** sanitizes each detail sidecar (allowlist tags/attrs incl. an SVG allowlist; strip `<script>`/`on*`/`javascript:`/`animate`/`set`/`foreignObject`/external `href`; keep inline `style`). **Fail-closed**: no `nh3` → plain escaped text only, never raw HTML.
- **CSP `<meta>`**: `script-src`/`style-src` must be `'unsafe-inline'` (Treemon injects inline scripts; template + sidecars use inline styles), so CSP cannot restrict scripts — that is `nh3`'s job. CSP locks `img-src`/`font-src`/`connect-src` to `'self' data:`, blocking the `url()`/beacon/fetch **exfil** vector for HTML and SVG alike. Split of duties: **CSP = exfil, `nh3` = script.** (Neither filters layout CSS, so inline-`style` UI-redress within the iframe is an accepted residual risk — see the Phase 4 decisions.)
- **postMessage = privilege boundary**: the channel is **origin-pinned** — the action bar targets the trusted parent origin (`{{PARENT_ORIGIN}}`, default `http://localhost:5000`) instead of the wildcard `"*"`, and the inbound `message` listener rejects any `event.origin`/`event.source` that is not the embedding parent before re-rendering; plus namespaced actions, stable id validation via Python, **human confirmation** before any execution.

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

#### Phase 3 render decisions (implemented)

Phase 3 implements steps 1 and 3 above. Phase 4 (`focused-review-icp`) added step 2 (`nh3` sidecar sanitization) and the `capabilities` half of step 4; the `validate-action` surface remains deferred to a later phase. Phase 3 left `render-review` rendering the **text-field fallback only** (the fail-closed "plain escaped text" baseline) and `_canvas_finding_block` marking the sidecar extension point — Phase 4 fills that point in.

- **One `provenance` source list → three "Found by" views.** Source labels follow `rule--<name>` and `concern--<name>--<model>` (`rsplit("--", 1)` peels the model, so multi-word concern names survive; entries may be bare strings or `{"source": …}` objects). Rendered three ways: review.md is **ungrouped, count-prefixed** (`3 sources: concern:bugs (opus), …` — the `rule:{name}` / `concern:{name} ({model})` labels the post-mortem parser keys on); the terminal is **grouped by concern** (`bugs(opus,codex), rule:null-safety`); canvas emits **grouped color-coded tag spans** (`found-tag-concern` / `found-tag-rule`, the rule tag dropping its prefix since the CSS class conveys kind).
- **Only the canvas is `html.escape`d.** review.md and the terminal summary are Markdown/plain text and keep **raw** field values; Markdown table cells additionally collapse newlines and escape `|` so a value can't break the table. The single-pass regex fill substitutes via a `lambda` (no backref injection) and never re-scans injected content, so escaped finding text can't forge a `<!-- FR:* -->` marker or `{{RUN_ID}}`.
- **Template head doc comment is stripped before filling.** Every `<!-- FR:* -->` marker and `{{RUN_ID}}` appears twice in the template — once in the leading "VERSION-CONTROLLED TEMPLATE" head comment and once in the body — so `render-review` removes everything from the first `<!--` up to `<head>` (matching the hand-filled fixture) so each placeholder fills exactly once.
- **Empty sections/fields are omitted** in review.md: the Confirmed / Questionable / Invalid / Rule-Quality blocks are dropped when they have no entries, and a finding's `Assessment` / `Suggestion` lines are dropped when blank. Confirmed + Questionable share one numbered sequence ordered by `display_number`; Invalid render in a table keyed by `assessment_id`. Section counts/badges are computed `len()`s of the partitioned lists (validation guarantees they equal the `run.*` counts).
- **No relay trailer in the terminal summary.** `render-review` emits pure data; the reporter agent's "relay everything above this line verbatim" trailer was an LLM-control instruction. **Phase 6 (`focused-review-3z4`) must own the relay-verbatim guarantee** in `SKILL.md` when it wires `render-review` in.
- **Terminal summary is written to stdout as UTF-8 bytes**, independent of the ambient console encoding. The orchestrator captures (pipes) stdout, which defaults to cp1252 on Windows; a plain `print` would raise `UnicodeEncodeError` on the summary's emoji / `→` glyphs on every successful render. A real-subprocess regression test (`TestRenderReviewSubprocess`) runs under `PYTHONIOENCODING=cp1252` to lock this.
- **Render hardening (`focused-review-n6l`, Phase 3 follow-up).** Two boundary fixes keep the escape-everything invariant total and the render all-or-nothing: (1) `_sev_class(severity)` — the one finding-derived value reaching an HTML *attribute* — is now `html.escape(…, quote=True)`d at both canvas sites (`_canvas_finding_block` / `_canvas_invalid_row`) so a hostile severity can't break out of `class="…"`, rather than relying solely on the enum-validation gate ~700 lines away in `render_review` (the helpers are public). (2) `render_review` reads the canvas template — the only post-validation step that can raise an uncaught `OSError` — *before* writing any artifact, and renders both `review.md` + canvas in memory then writes, so a bad `--template` writes nothing (matching the validation-failure path) instead of leaving a half-written run. Both are locked by `test_render_review.py` (hostile-severity class breakout; `test_bad_template_path_writes_no_artifacts`).

#### Phase 4 decisions (implemented — nh3 sanitization + capability detection)

- **`nh3` is a module-level optional import (`_nh3`), fail-closed.** `import nh3 as _nh3` with an `ImportError` fallback to `None`. `sanitize_detail_html(raw)` returns the cleaned fragment, or **`None` when `_nh3 is None`** — the single fail-closed gate. Tests force `_nh3 = None` via monkeypatch to exercise degradation without uninstalling the dependency. Declared as a **core** dependency in `pyproject.toml` (`nh3>=0.2.15`, installed out the gate), not an extra.
- **One broad allowlist, three threat controls.** `_DETAIL_TAGS` = an HTML palette set (`div/span/pre/code/table/...`) ∪ an SVG set (`svg/path/rect/text/linearGradient/...`); `script`, SVG animation (`animate`/`animateTransform`/`animateMotion`/`set`/`mpath`), and `foreignObject` are **absent from the allowlist and listed in `clean_content_tags`** so their *content* (not just the tag) is dropped. Attributes use `"*"`-keyed globals (HTML + ARIA + the full SVG presentation/geometry set, all inert on HTML and none script-bearing) plus `href`/`xlink:href` only on `a`/`use`. Inline `style` is kept on every tag (CSP owns exfil). Event handlers are simply never allowlisted, so nh3 strips them.
- **"External href" = fragment-only, via an `attribute_filter`.** `_detail_attribute_filter` keeps `href`/`xlink:href`/`src` **only** when the value starts with `#` (same-document refs: SVG `<use>`, gradient/marker `url(#…)` targets); every external, `javascript:`, or `data:` URL is dropped. This is stricter and more explicit than a `url_schemes` tweak (which would still permit `http(s)`).
- **Sidecars are loaded + sanitized in the CLI handler, then threaded as data.** `render_review` builds `details: {record_id → sanitized_html}` by resolving `{run_dir}/assessments/{assessment_id}-detail.html` for each `has_detail` finding (`_resolve_finding_detail`), and passes it to `render_canvas_html(data, template, details)`. Keeping `render_canvas_html`/`_canvas_finding_block` **pure** (a `details` dict, no file IO / nh3 coupling) preserves the Phase 3 tests and lets the embedding be tested with pre-sanitized fragments. A finding falls back to text-only when: no nh3, `has_detail` false, missing/unreadable sidecar, or the fragment sanitizes to empty.
- **`assessment_id` is path-guarded at the point of use.** It is interpolated into a filesystem path, and `records.json` is LLM-authored from untrusted diff content, so `_resolve_finding_detail` rejects any `assessment_id` not matching `[A-Za-z0-9_-]+` (returns `None` → text fallback). This blocks directory traversal (`../…`, absolute/drive paths) and a null-byte `open()` `ValueError` that would otherwise escape `except OSError` and abort the always-written canvas. The guard lives in the resolver (not the Phase 2 validator) so the protection holds regardless of the validation contract; the read also catches `(OSError, ValueError)` to fail closed.
- **The sanitized fragment is embedded raw** (already cleaned — re-`html.escape`ing it would defeat the feature), wrapped in `<div class="rich-detail">` after the escaped structural fields. `strip_comments=True` means a sidecar can't smuggle a forged `<!-- FR:* -->` canvas marker.
- **`capabilities` subcommand mirrors `rich_html` onto `nh3`.** Emits `{"nh3", "rich_html", "nh3_version", "schema_version"}` as pure-ASCII JSON to stdout (plain `print` is encoding-safe); `rich_html == nh3` because rich HTML is unusable without the sanitizer. The orchestrator runs it **before** assessment to gate the assessor's optional sidecar.
- **Accepted residual risk — inline `style` is not CSS-filtered.** Per the "no separate CSS sanitizer" decision, nh3 passes `style` through verbatim. CSP neutralizes the *exfil* vector (`img/font/connect-src 'self' data:`), but **layout/UI-redress** CSS (e.g. `position:fixed;z-index:…` painting a full-viewport overlay) survives within the sandboxed iframe. This is accepted because: (a) `review.md` + the terminal summary are CSS-free authoritative channels (the canvas is additive), and (b) the action bar resolves ids/targets from `records.json` server-side and requires human confirmation, so a visual overlay cannot forge an executed action. If UI-redress is ever in scope, the surgical mitigation is nh3's `filter_style_properties=` with a palette-aligned property allowlist (must be coordinated with the Phase 5b assessor palette guidance so legitimate diagrams keep working).

#### Phase 5a decisions (implemented — reporter writes records.json)

- **Reporter input contract changed; role unchanged.** `report_path: {run_dir}/review.md` is replaced by `run_dir` (plus optional `records_path`, default `{run_dir}/records.json`, and optional `run_id`, default the `run_dir` basename). The reporter still reads `assessed.md` / `consolidated.md` / `findings/`, classifies verdicts, applies rebuttal overrides, and synthesizes rule-quality notes — it just serializes to the Phase 2 envelope instead of hand-authoring `review.md` + the terminal summary. **Phase 6 (`focused-review-3z4`) must pass `run_dir`/`run_id`, stop passing `report_path`, and stop relaying the reporter's stdout** (it now relays render-review's terminal summary instead).
- **Rebuttal-override input shape is preserved.** The orchestrator still hands the reporter `{id: "A-XX", severity, reasoning}` (assessment-id keyed). The reporter resolves `id`→finding, flips it to Confirmed at the override severity, appends the reasoning to the finding's `assessment`, and writes the envelope's `rebuttal_overrides[]` as `{record_id, original_severity (the pre-override assessed severity), severity (the override), reasoning}` — so `original_severity` preserves the audit trail and `severity` shows the reinstated value.
- **Provenance is emitted canonical, not display-formatted.** The reporter passes through (assessed/consolidated) or derives (raw_findings drops the `.md`) the `rule--<name>` / `concern--<name>--<model>` source labels; Python owns the three "Found by" views, which keeps the post-mortem `rule:` / `concern:` parser working off render-review's `review.md`.
- **`record_id` is a per-run sequential token (`r1`, `r2`, …)** assigned to every finding (incl. Invalid, for stable canvas-action targeting); `assessment_id` stays the `A-XX` (null when unassessed). `has_detail` is set by probing `{run_dir}/assessments/{assessment_id}-detail.html` existence — `false` everywhere until the Phase 5b assessor sidecar lands.
- **Reporter emits no user-facing text.** Its stdout is a short internal confirmation (records path + verdict counts); the user-facing summary and the relay-verbatim guarantee are render-review's terminal output, owned by Phase 6 (mirrors the Phase 3 "no relay trailer" decision).

#### Phase 5b decisions (implemented — assessor optional sidecar)

- **`rich_html` is a new optional assessor input, default-off.** The assessor authors `{assessment_id}-detail.html` (a new, additive Step 8 in `agents/review-assessor.agent.md`) **only** when the flag is set. Phase 6 (`focused-review-3z4`) wires the orchestrator to detect `nh3`/`rich_html` before assessment and pass it into the assessor prompts; until then the flag is absent, no sidecar is written, and `has_detail` stays `false` everywhere (consistent with the Phase 5a note).
- **The assessor writes the *inner* fragment — it must not wrap it in `<div class="rich-detail">`.** `_canvas_finding_block` already wraps the sanitized fragment in the `.rich-detail` div, so a self-wrap would double the wrapper and its divider. The prompt states this explicitly.
- **Sidecar path is the `output_path` sibling**, `{run_dir}/assessments/{assessment_id}-detail.html`, matching the Phase 4 resolver — so no new `run_dir`/path field is threaded into the assessor prompt; it derives the location from `output_path` + `assessment_id`, and overwrites on a re-run (`Remove-Item` then `create`, like the markdown).
- **Palette + allowlist guidance mirrors the source of truth, it is not a second copy of it.** The prompt lists the Phase 1 template's palette classes (`.code-block`/`.highlight-line`/`.callout`/`.before-after`/`.flow`) and the Phase 4 `nh3` allowlist (HTML+SVG tags, inline `style` kept; `script`/`on*`/`<style>`/`foreignObject`/animation/external `href`/`src` stripped) so the assessor stays inside what the renderer keeps. The authoritative definitions remain the template CSS and the `_DETAIL_*` sets; the prompt guidance is balanced ("author a visual only when it clarifies a non-obvious finding").
- **Primary `A-XX.md` markdown is unchanged.** The sidecar is purely additive; `assessed.md` / rebuttal / `post-mortem` / `post-comments` keep reading the markdown, and the reporter sets `has_detail` by probing the sidecar's presence on disk (Phase 5a).

#### Phase 6 decisions (implemented — orchestrator render integration)

- **`rich_html` is detected once, before assessment, and threaded per-assessor.** `SKILL.md` Step 4 runs `python {script_path} capabilities` and parses the `rich_html` boolean *before* launching any assessor, then passes `rich_html: {true|false}` into **every** assessor prompt. A command/parse error degrades to `rich_html: false`. `full` scope skips assessment entirely, so it never runs the check. This closes the Phase 5b handoff.
- **`render-review` is always called and owns the user-facing relay.** Step 6 splits into 6a (reporter writes `records.json` — pass `run_dir`/`run_id`, **drop `report_path`**, do **not** relay the reporter's internal stdout), 6b (`python {script_path} render-review --records {run_dir}/records.json --repo .`), 6c (relay). On success the orchestrator **captures `render-review`'s stdout and relays it verbatim** — that terminal summary (report path + pipeline stats + Found-by table + rule-quality notes) is the single user-facing result, fulfilling the Phase 3 "Phase 6 owns the relay-verbatim guarantee" handoff. `--run-dir` is left to its default (the `records.json` directory) so the detail-sidecar resolver and `review.md` land under `{run_dir}`.
- **Retry on validation failure, ≤2×, handing the errors back.** `render-review` exits 1 and writes the structured per-record error JSON to stderr (and writes no artifacts). The orchestrator re-launches the reporter with a new optional **`validation_errors`** input (the stderr JSON) so it fixes the exact offending fields and rewrites the envelope, then re-runs `render-review` — at most twice. The `validation_errors` input was added to `agents/review-reporter.agent.md` to make the retry corrective rather than a blind regenerate.
- **Never-fail fallback = legacy hand-authored markdown via a `general-purpose` agent, canvas skipped.** After 2 failed retries the orchestrator launches a `general-purpose` agent that authors `{run_dir}/review.md` in the legacy report shape and emits the user-facing summary directly from the data source (the canvas is skipped for that run). The reporter agent itself stays records-only (it no longer hand-authors markdown), so the fallback's legacy instructions live inline in `SKILL.md` rather than re-introducing a dual-mode reporter.
- **Canvas messaging is best-effort; action-handling wired separately.** The canvas file is always written (`.agents/canvas/focused-review.html`, gitignored); `SKILL.md` tells the user the live tab is open when Treemon is present and notes the file is inert otherwise. The `postMessage` action bar (`focused-review.fix`/`.disregard`/`.document`) was out of scope for the render-integration task and is implemented by `focused-review-har` — see the *action-bar round-trip* decisions below.
- **Version bumped `0.6.4` → `0.7.0`** across `plugin.json`, `.claude-plugin/plugin.json`, and `.claude-plugin/marketplace.json` (a feature minor bump, matching the `0.5.0`/`0.6.0` convention); `test_plugin_manifests.py::TestCrossFileConsistency` keeps the three in sync.

#### Phase 6 decisions (implemented — action-bar round-trip, `focused-review-har`)

- **`validate-action` is a pure validate/expand surface; Python never executes.** The canvas posts `{action, run_id, record_ids[], instructions}`; `python {script_path} validate-action --records {run_dir}/records.json --run-id … --record-ids r1,r3 [--action …] [--instructions …]` confirms the posted `run_id` equals `records.json`'s `run.run_id` (the **forgery gate** — injected canvas JS can't act on a different run), confirms every `record_id` exists, and resolves each to file/line/title/suggestion (plus severity/verdict/fix_complexity/ids for context). Success → resolved JSON to **stdout** (exit 0); a forged/blank `run_id`, any unknown `record_id`, or an unreadable `records.json` → structured per-record errors (same `_records_error` shape, `scope:"action"`) to **stderr** + exit 1. `file`/`line`/`suggestion` are resolved **only** from the trusted local `records.json`, never from the payload (the CLI surface gives the payload no way to inject a path). It runs against the already-render-validated `records.json` via `_load_records_only` (read+parse, **no** re-run of the full schema validation, so an unrelated field error can't block a legitimate action). stdout uses `json.dumps` (default `ensure_ascii=True` → pure-ASCII), so a plain `print` is encoding-safe under the orchestrator's piped, possibly-cp1252 stdout (unlike `render-review`'s glyph-bearing summary, which needs `_emit_stdout`).
- **Human confirmation gates every action; nothing auto-runs.** `SKILL.md` Step 6d: validate/expand first, **reject** on exit 1 (relay the error, do nothing), then **require an explicit human go-ahead** before dispatching — `fix` proposes/apply­s edits scoped to the resolved findings with the `instructions`; `disregard` persists run state then re-renders; `document` writes a tracking doc. Each message is **re-validated** (never cached) so a stale/forged `run_id` or a re-categorized id can't leak into an action.
- **`disregard` is the one stateful action — persisted in `{run_dir}/run-state.json`.** After confirmation the orchestrator re-runs `validate-action … --action focused-review.disregard --apply-disregard --run-dir {run_dir}`; `--apply-disregard` is the **only** side effect and fires **only after a successful validation** (a forged/unknown action can never write state), merging the ids into `run-state.json` monotonically (add-only, first-seen order). `render-review` reads it back (`load_run_state`, gated on the run's `run_id`) and bakes the `dimmed` class onto those `.finding` blocks, so the dim **persists across every re-render** — not just the in-session JS morph. The state file is **fail-open**: missing/unreadable/non-dict/JSON-error/mismatched-or-missing `run_id` all yield an empty disregarded set, so it never blocks the always-written canvas and never dims another run's findings.
- **The action-bar JS seeds its disregarded set from server-rendered `.dimmed` classes** (`review-canvas.html` + `.fixture.html`, in `restoreState`), so a persisted disregard baked into the HTML survives a **cold** canvas load, not only an in-session morph where the click handler already populated the set. Disregard being add-only makes this union safe; the seeded ids are still pruned to those currently rendered.
- **`render_canvas_html`/`_canvas_finding_block` stay pure** — the `disregarded` set is threaded as data (mirroring the Phase 4 `details` dict), no file IO inside the renderer; the CLI handler owns reading `run-state.json`. The new `disregarded`/`dimmed` params default empty, so existing callers/tests are unaffected.


#### Phase 6 decisions (security fix — origin-validated postMessage channel, `focused-review-di6`)

- **The action-bar channel is origin-pinned, not wildcard.** The quality-gate review (findings C-03 Medium / C-15 Low) flagged the prototype's `window.parent.postMessage(payload, "*")` — which broadcasts the `run_id` + free-text `instructions` to **any** framing parent — and its inbound `message` listener that re-rendered on **any** sender. Fix: a new `{{PARENT_ORIGIN}}` placeholder is threaded into `<body data-parent-origin>` (filled by `render_canvas_html`, exposed as `render-review --parent-origin`, default `http://localhost:5000` — the Treemon parent app's origin, distinct from the canvas iframe's `127.0.0.1:5002`). The JS reads it via a `getParentOrigin()` helper (mirroring `getRunId()`) and (a) pins it as the outbound `postMessage` target origin and (b) drops inbound messages whose `event.origin !== getParentOrigin()` or `event.source !== window.parent` before calling `restoreState()`.
- **Threaded as data so the fixture stays a faithful twin.** The origin lives in a DOM attribute, not a JS string literal, so the template's and fixture's executable JS remain byte-identical — `test_review_canvas_template.py::test_fixture_executable_js_identical_to_template` now enforces that (comments and filled-in attribute values may differ; statements may not), locking both ends of the channel against drift. The value is `html.escape`d for the attribute context exactly like `{{RUN_ID}}`. No version bump — same `0.7.0` release.


### HTML template — `skills/focused-review/templates/review-canvas.html`

Static, version-controlled page shell (head, CSS class palette for detail HTML, CSP `<meta>`, morph-safe action-bar script) with placeholders Python fills with pre-rendered rows/accordions/panels.

**Placeholder contract** (built in Phase 1; `render-review` fills it). Block-injection sites are **HTML comments** — `html.escape` turns `<` into `&lt;`, so escaped finding text can never forge a `<!-- FR:* -->` marker:

- `{{RUN_ID}}` — scalar in `<body data-run-id>`. Fill **first** (before injecting escaped content) so finding text can't forge it; a single-pass regex substitution over the whole set is safest.
- `{{PARENT_ORIGIN}}` — scalar in `<body data-parent-origin>`; the trusted Treemon parent origin the action bar pins as its `postMessage` target and validates inbound `message` events against. Threaded as data (mirroring `{{RUN_ID}}`, `html.escape`d for the attribute) so the executable JS stays identical to the fixture; `render-review` fills it from `--parent-origin` (default `http://localhost:5000`).
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
