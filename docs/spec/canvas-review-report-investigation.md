# Canvas Review Report Investigation

## Problem Statement

The focused review plugin currently presents findings as a terminal summary table + a markdown report file (`review.md`). While functional, this is hard to consume: the user must mentally parse markdown, switch between terminal and file, and has no way to interact with findings (dismiss, prioritize, batch-fix). The new canvas skill offers an HTML pane with `postMessage` interactivity — a natural fit for a richer review experience.

## Symptoms

- Review output is text-only: terminal table + markdown file
- No way to dismiss/deprioritize findings without manually editing or running post-mortem
- No interactive triage: user can't select findings and say "fix these" or "ignore these"
- Expandable detail sections aren't possible in terminal/markdown
- No visual severity indicators beyond emoji

## Evidence Gathered

### Canvas Skill Capabilities
- Canvas skill profile: `skills/canvas/SKILL.md` (installed Copilot CLI `canvas` skill)
- HTML files in `.agents/canvas/` auto-detected by Treemon and shown as tabs
- `window.parent.postMessage({...}, '*')` sends data back to the agent session
- Dark-themed IDE styling with recommended base CSS
- Inline CSS only, self-contained HTML, auto-reloads on content hash change
- Each HTML file = separate tab; descriptive filename = tab name

### Current Review Pipeline (5 Phases)
- `skills/focused-review/SKILL.md:114-273`
- Phase 1: Discovery (parallel rule + concern agents) → raw findings
- Phase 2: Consolidation → `consolidated.md` (deduped, capped at 30)
- Phase 3: Assessment → `assessed.md` (verdicts: Confirmed/Questionable/Invalid)
- Phase 4: Rebuttal (optional, Critical/High Invalid only)
- Phase 5: Presentation → `review.md` + terminal summary

### Finding Data Structure (Available at Report Time)
From `agents/review-assessor.agent.md:199-239` and `agents/review-consolidator.agent.md:122-159`:

Each assessed finding contains:
- `id` (A-XX)
- `title`
- `file` + `line`
- `severity` (Critical/High/Medium/Low) — both original and assessed
- `fix_complexity` (quickfix/moderate/complex)
- `verdict` (Confirmed/Questionable/Invalid)
- `type` (rule/concern/mixed)
- `introduced_by` (diff/pre-existing)
- `description`
- `evidence`
- `investigation` (introduced by diff?, code verified, evidence requirements)
- `pro_arguments`
- `counter_arguments`
- `rule_applicability`
- `assessment_reasoning`
- `suggestion`
- `provenance` (source list with counts)

### Current Report Format
From `agents/review-reporter.agent.md:34-105`:
- Markdown with verdict sections (Confirmed, Questionable, Invalid)
- Findings grouped by file path, ordered by line number
- Each finding: title, file:line, fix complexity, found-by, description, assessment, suggestion
- Invalid findings in `<details>` block
- Rule quality notes section

### Reporter Terminal Summary
From `agents/review-reporter.agent.md:108-147`:
- Report path line
- Pipeline stats line
- Findings table (all confirmed + questionable)
- Rule quality notes
- Verbatim relay trailer

### Post-Review Actions (Current)
- `post-mortem`: trace invalid findings back to rules, suggest edits
- `post-comments {pr-url}`: post as PR comments
- Manual rule editing + re-run
- No "fix finding" or "dismiss finding" workflow exists

## Root Cause Analysis

The review pipeline was designed output-first (markdown report + terminal summary) without an interaction layer. The reporter is a terminal step that produces static text. There's no feedback loop where the user can act on individual findings without starting a new conversation.

### Contributing Factors
- Canvas skill didn't exist when the pipeline was designed
- The "relay verbatim" pattern (`SKILL.md:273`) assumes text-only output
- No structured data format exists between reporter and consumer (only markdown)
- Post-mortem is the only feedback mechanism, and it's rule-focused not finding-focused

## Proposed Solution

### Architecture: Reporter Emits Structured Records → Python Renders Everything Server-Side

**Core idea:** The pipeline is **unchanged through assessment** — discovery, consolidation, and assessment agents keep emitting **markdown** (`consolidated.md`, per-finding `A-XX.md`, `assessed.md`). These remain the agent-readable inputs and the human-readable trail; Python never parses them. The change is at the **final compile step**: the **reporter** agent (which already reads `assessed.md` and compiles the final output) now emits a **structured, schema-defined JSON record set** (`records.json`) instead of hand-authoring markdown. Python then **deterministically renders all presentation artifacts** from that single source — `review.md`, the terminal summary, and the canvas HTML — **fully server-side** (no client-side rendering JS, no `window.__REVIEW_DATA__`). Rich per-finding visuals are carried as optional **raw-HTML sidecar files** that the **assessor** authors (it has the code context) and Python **sanitizes** before embedding.

This keeps the existing workflow intact (the reporter stays as the compile step; only its output format flips markdown → JSON), gives a **single source of truth + deterministic rendering** (no drift between `review.md` and the canvas), and keeps Python's role strictly mechanical (validate a known schema + template — never interpret prose).

**Why writing is unconditional:** Python **always** writes `.agents/canvas/focused-review.html` — it's gitignored and inert if Treemon isn't running, and an LLM can't reliably introspect whether the `canvas` skill is "loaded." If Treemon is up, the tab simply appears. The orchestrator may still use a best-effort `canvas`-skill-presence check, but only to decide whether to *tell the user* about the pane and *await* a `postMessage` — never to gate the write.

#### Validated UI prototype

The target UI is already built and validated in `.agents/canvas/focused-review-prototype.html` (populated with real data from a prior review run, `tm-canvas48/.agents/focused-review/20260604-112540`). It establishes the layout the template will be derived from:

- **Summary table** styled like the terminal output — rows with `#`, severity, "Found by", issue title (no file column; not every finding has a single location)
- **Accordion rows** — click to expand one detail panel at a time
- **Section grouping** — Confirmed / Questionable, each with a select-all checkbox
- **Per-row checkboxes** + sticky **action bar** (instructions input + Fix / Disregard / Document buttons → `postMessage`)
- **Collapsible** invalid-findings table and rule-quality notes (notes expanded by default)
- All **fixed `px` font sizes** for cohesion

The detail panel in the prototype currently shows plain text fields (description, assessment, suggestion). The rich-HTML detail below is what upgrades those panels.

#### Refined design principle: structured output is fair game for Python

The repo rule *"No Python parsing of LLM output"* is about **free-form prose** — Python must not try to *understand* a markdown report. It does **not** forbid Python from validating and templating a **schema-defined contract** the agent was told to emit. We refine it:

- ✅ Python may **parse/validate structured output against a known schema** and render it into MD/HTML/terminal.
- ❌ Python still must not scrape **free-form report prose** (e.g. parsing `assessed.md` markdown to recover findings).

The test: *"is there a contract the agent was told to honor?"* If yes, Python parsing it is mechanical work, not interpretation.

#### Why the assessor authors the rich detail

The assessor is the only agent that can make **semantic decisions** about what to show: which 5 code lines matter, whether a finding wants a call-chain, a before/after, or a flow diagram. It has already read the source and traced callers. It runs **in parallel** (one per finding), so authoring detail is incremental work, not a new bottleneck. A mechanical template can lay out fields; it cannot decide *what content best explains each finding*.

#### Component Changes

**1. Reporter emits the structured records** (`agents/review-reporter.agent.md`)

The reporter keeps its role as the final compile step (read `assessed.md` / `consolidated.md` / `findings/`, apply rebuttal overrides, classify by verdict, synthesize rule-quality notes). Its **output contract changes**: instead of hand-authoring `review.md` markdown + a terminal summary, it **writes** (to disk, not chat) **one JSON envelope** — `{run_dir}/records.json` — with this shape:

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

It is an **envelope, not a bare array** because the report/terminal need run-level metadata, **rule-quality notes** (per-rule, not per-finding), rebuttal overrides, and invalid counts that don't fit on a finding row. Each finding carries **stable IDs** (`record_id`, `assessment_id`) separate from the positional `display_number`, plus both `original_severity` and (post-override) `severity` so the renderer always shows the **final** verdict while keeping the original for the audit trail. Python derives `review.md`, the terminal summary, and the canvas from this one source — no double output, no drift.

**Reliability:** the reporter writes JSON containing prose fields, so it can still emit malformed JSON (unescaped quotes/newlines, truncation on ~30 findings). Python validates and, on failure, returns a structured per-record error; the orchestrator retries the reporter **at most twice**, then **falls back to today's hand-authored markdown path** (the reporter writes `review.md` directly, canvas is skipped). The pipeline never hard-fails on a bad serialization.

The assessor's primary output (`A-XX.md` markdown) is **unchanged**, so `assessed.md`, rebuttal, post-mortem, and post-comments all keep working against the same markdown they read today.

**1b. Assessor emits an optional rich-detail sidecar** (`agents/review-assessor.agent.md`)

The assessor — the only agent with the source code in context — **voluntarily** authors a raw-HTML detail fragment `{run_dir}/assessments/A-XX-detail.html` **only when a visual genuinely adds value** (a code snippet with highlighted lines, a before/after, a call-chain or flow/timing SVG). For trivial findings it writes no sidecar and the canvas panel falls back to the record's text fields. The assessor prompt must be **balanced** — explicitly neither "always produce a diagram" nor "never bother"; generate rich detail when it clarifies a non-obvious issue, skip it otherwise. Python locates the sidecar by assessment id, sanitizes it, and embeds it in that finding's panel.

**2. Rich detail = sanitized raw HTML (not a block language)**

The assessor writes ordinary HTML/SVG for the detail panel. We considered a structured "block vocabulary" that Python would render to HTML, but rejected it: it's rigid, expensive to maintain renderers for, and LLMs are already fluent in HTML. Instead:

- The assessor authors HTML freely (code snippets with highlights, callouts, before/after, even an SVG flow/timing diagram).
- A **prompt-provided CSS class palette** (`.code-block`, `.highlight-line`, `.callout`, `.before-after`, `.flow`, …) keeps styling cohesive, but **inline `style` is allowed** — we don't add a CSS sanitizer. That's safe because the two real controls already cover it: the **CSP** locks `img-src`/`font-src`/`connect-src` so a `url(http://evil)` beacon can't fetch, and **`nh3`** strips anything that executes. A dedicated CSS sanitizer would be gold-plating (and the agent reading untrusted diffs is a far larger injection target anyway).
- **SVG is allowed** for flow/call-path/timing diagrams, sanitized by an `nh3` SVG allowlist (`svg/g/path/rect/line/circle/text/marker/defs` + geometry/paint attrs; **exclude** `script`/`on*`/`animate`/`set`/`foreignObject`/external `href`). SVG adds no new *exfil* channel (CSP covers it like HTML) — its only extra risk is script-carrying tags, which the allowlist removes.
- **Python sanitizes** every detail blob before embedding (see Security Model). This is the control that makes accepting raw HTML safe.

**Example detail (null-safety finding):**

```html
<div class="detail-fragment">
  <div class="code-block">
    <div class="code-header">src/auth/login.cs:40-44</div>
    <pre><code>40 public async Task&lt;AuthResult&gt; Login(LoginRequest request)
41 {
42 <span class="highlight-line">    var user = await _auth.Authenticate(request.Username, request.Password);</span>
43     return AuthResult.Success(user);
44 }</code></pre>
  </div>
  <div class="callout callout-warning">
    <strong>No null guard in the call chain.</strong>
    <code>request.Username</code> → <code>Authenticate()</code> → <code>FindByName()</code> — none check for null.
  </div>
  <div class="before-after">
    <div class="before"><div class="ba-label">Current</div><pre><code>var user = await _auth.Authenticate(request.Username, request.Password);</code></pre></div>
    <div class="after"><div class="ba-label">Suggested</div><pre><code>ArgumentNullException.ThrowIfNull(request.Username);
var user = await _auth.Authenticate(request.Username, request.Password);</code></pre></div>
  </div>
</div>
```

For flow/call-path diagrams (like an auth request path), the assessor may emit inline `<svg>` directly — sanitized against an SVG allowlist (see Security Model). No special block schema needed.

**3. Python: validate + render server-side** (`focused-review.py`, new subcommand e.g. `render-review`)

Python is the deterministic engine:

1. **Load** `{run_dir}/records.json` (the reporter's record set).
2. **Validate** against the schema. On failure, write a structured error (which record, which field, what's wrong) that the orchestrator relays back to the **reporter** for a retry — the validation-error feedback loop.
3. **Sanitize** each referenced `A-XX-detail.html` sidecar (capability-detected; see below).
4. **Render** three artifacts from the single source, **fully server-side**:
   - `review.md` (markdown report — replaces the reporter's previously hand-authored output; format preserved so post-mortem/post-comments keep parsing it)
   - terminal summary string
   - `.agents/canvas/focused-review.html` (**always written**; appears as a tab only when Treemon is running) — Python pre-renders **every** row, accordion, and detail panel as static HTML; nothing is rendered client-side. The page embeds the `run_id` so actions can be attributed to the right run.

Python escapes all text fields with stdlib `html.escape` and embeds only sanitized detail HTML. No prose parsing anywhere.

**4. HTML Template** (`skills/focused-review/templates/review-canvas.html`)

Derived from the validated prototype. A static, version-controlled **page shell** (head, CSS class palette, CSP `<meta>`, and one small **static** interactivity script) with placeholders that Python fills by injecting **pre-rendered HTML** for the summary table, accordion rows, and detail panels. There is **no client-side rendering JS and no `window.__REVIEW_DATA__`** — Python builds the markup. The only JavaScript is data-independent and fixed: it wires the action bar (collect checked finding ids + the instructions box → `postMessage`). Accordion expand/collapse is pure CSS via `<details>`/`<summary>` where possible. Dropping the data-in-JS blob also removes a script-injection surface.

**Morph-safety.** Treemon live-reloads by morphing `document.body` in place (idiomorph) whenever Python rewrites the file. So the interactivity script lives in `<head>` (survives body morphing) and uses **document-level event delegation** — never per-row `addEventListener`, which would be lost when rows are replaced. Transient UI state (which findings are checked, which are dimmed/disregarded, which accordions are open) is keyed by `record_id` and re-applied after the `morph-complete` event, so a re-render doesn't reset the user's selection. "Disregarded" is treated as **persisted run state** (written back via an action), not merely a CSS class that a morph would wipe.

**5. Reporter stays — it just emits JSON instead of markdown**

The reporter is **not removed**. It remains the compile step (verdict classification, rebuttal-override application, rule-quality-note synthesis) — the genuinely semantic work stays in an agent. Only its serialization changes: it writes `records.json` (data) rather than `review.md` (presentation). Python owns presentation. This keeps the workflow shape identical to today while giving Python a clean structured contract to render from.

**6. Orchestrator** (`skills/focused-review/SKILL.md`)

- **Before assessment**, run an early Python capability check (`import nh3`) and pass `rich_html: true|false` into each assessor prompt — the assessor needs this *before* it runs, so it isn't decided in the later render phase.
- **Canvas is always rendered** (Python always writes the file). A best-effort `canvas`-skill-presence check only decides whether to tell the user about the pane and whether to keep the turn open awaiting a `postMessage`.
- After the reporter writes `records.json`: call Python `render-review`; on a validation error, re-dispatch the **reporter** with the structured error (**max 2 retries**), then fall back to the legacy markdown path.
- Relay the Python-produced terminal summary verbatim.
- On an inbound `postMessage`, **call Python to validate/expand the action** against `records.json` (don't rely on context memory), then require human confirmation (see below).

**7. postMessage actions** (orchestrator receives)

The canvas posts `{ action, run_id, record_ids: [...], instructions }` where `action` is **namespaced** to avoid Treemon's reserved names (`focused-review.fix` / `.disregard` / `.document`, not bare `fix`) and `record_ids` are the **stable** `record_id`s (never positional display numbers, which shift when findings are filtered/disregarded). The orchestrator treats this as **untrusted input**: it calls Python to confirm `run_id` matches the current run and every `record_id` exists, and to expand each id back to its `file`/`line`/`title`/`suggestion` from `records.json`. It then **requires human confirmation before executing** — actions compose a proposed plan the user confirms in chat; they never auto-run:

- `focused-review.fix` → propose fixing the selected findings with the given instructions
- `focused-review.disregard` → mark them disregarded as **persisted run state** (re-applied across re-renders); optionally update `review.md`
- `focused-review.document` → write a separate doc tracking the selected findings for later

### Security Model

Reviewed content is **untrusted** (focused-review runs on diffs, including PRs from untrusted contributors via `post-comments {pr-url}`). A malicious diff can embed `</script><script>…</script>` in a snippet, which would flow diff → finding → HTML → execute in the canvas. iframe + postMessage-origin controls are **necessary but not sufficient** (origin control protects the agent from rogue pages; it doesn't stop injected JS inside the canvas from calling the handler).

**Verified Treemon behavior** (from `tm-canvas48`, not yet in main — confirm before relying):
- Canvas docs are served over HTTP from `http://127.0.0.1:5002` (`CanvasPane.fs:8`, `CanvasDocServer.fs`); the iframe uses `sandbox="allow-scripts allow-same-origin allow-forms allow-popups"` (`CanvasPane.fs:273`) — **not** an opaque origin.
- The parent app runs on `localhost:5000` (`Program.fs`), a **different origin** from the canvas (5002), so injected JS **cannot reach the parent DOM** — `postMessage` is the only channel. Treemon validates `me.origin === CanvasOrigin`, requires `action` to be a string, and **caps payloads at 64 KB** (`CanvasPane.fs:306-322`).
- The server sets **no CSP header**, so our `<meta>` CSP **is honored**. **But** Treemon injects its own inline `<script>` tags (link interceptor, idiomorph, morph controller) into every served doc (`buildInjection`), so our CSP **must keep `script-src 'unsafe-inline'`** — CSP therefore cannot restrict script execution; `nh3` sanitization is the real defense.
- Live-reload morphs `document.body` in place on update; the static interactivity script must be morph-safe.

Layered defenses:

1. **Escape all text-derived fields** — Python renders every structured field through stdlib `html.escape` (zero dependencies). Covers the bulk of the surface.

2. **Sanitize the raw detail HTML — capability-detected, fail-closed.**
   - Use **`nh3`** (Rust `ammonia` binding; `bleach` is deprecated). Allowlist tags/attrs; strip `<script>`, `on*=` handlers, `javascript:` URLs.
   - **Keep inline `style`** (CSS can't execute JS in a modern webview) and the class palette — preserves styling flexibility.
   - **SVG allowlist** for diagrams: permit `svg/g/path/rect/line/circle/polyline/polygon/text/marker/defs` + geometry/paint attrs; **forbid** `script`, `on*`, `animate`, `set`, `foreignObject`, and external `href`/`xlink:href`. SVG adds no exfil channel beyond HTML (CSP covers it); the allowlist removes only its script-carrying tags.
   - **Fail closed:** Python `try`/`except` on `import nh3`. If present → sanitize and embed rich HTML. If absent → embed **plain escaped text only**, never raw HTML. The no-dependency path is always safe; sanitization can never be silently skipped.
   - `nh3` ships **out the gate** (installed on the primary dev machine); detect availability up front and tell the assessor whether rich HTML will be accepted (`rich_html: true|false`), so machines without `nh3` don't waste tokens producing HTML that would be rejected.

3. **CSP in the generated page** — concrete policy:

   ```
   default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
   img-src 'self' data:; font-src 'self' data:; connect-src 'self';
   object-src 'none'; base-uri 'none'
   ```

   `script-src` **and** `style-src` must allow `'unsafe-inline'` — Treemon injects inline scripts, and the template + sidecars use inline styles. So CSP **cannot** restrict scripts or inline CSS; that is `nh3`'s job. What CSP *does* enforce is **exfiltration**: a CSS `url(http://evil/…)` or `<img>` beacon is governed by `img-src`/`font-src`, and any `fetch`/XHR by `connect-src` — all locked to `'self'`/`data:`. This holds for HTML and SVG alike. Net split of duties: **CSP = exfil, `nh3` = script.** (Removing the data-in-JS blob — see Component 4 — closes the matching script-injection surface.)

4. **postMessage = the privilege boundary** — action allowlist + validate finding IDs + **human confirmation before any execution**. Even if XSS ran, it could only post messages the handler accepts, and those require the user to approve in chat. `instructions` is never interpreted as an agent command without a human in the loop. **Avoid Treemon's reserved action names** (`navigate-canvas-doc`, `morph-complete`, `content-updated`) and stay within the 64 KB payload cap; `fix`/`disregard`/`document` are safe.

### Graceful Degradation

Two independent capability switches, both fail-safe:

| Capability | Present | Absent |
|---|---|---|
| **`nh3` (sanitizer)** | Assessor authors rich HTML/SVG *when it adds value*; Python sanitizes + embeds | Assessor emits plain text fields; Python renders escaped text (no raw HTML) |
| **Treemon running** | The always-written `.agents/canvas/focused-review.html` shows as a live tab | The file is still written but inert (gitignored); `review.md` + terminal carry the result |

`review.md` + the terminal summary are **always** produced, and the canvas file is **always written** (harmless when Treemon is down). Rich HTML/SVG is additive on top, gated only by `nh3`. On the primary dev machine `nh3` is installed and Treemon is running; elsewhere it degrades to today's text experience with no hard failure.

### Alternative Approaches Considered

**A. Agents emit both markdown and JSON (double output).** Rejected — wastes tokens and risks the two representations drifting. Single structured source + Python rendering avoids duplication.

**B. Structured "block vocabulary" the agent emits and Python renders to HTML.** Considered seriously (zero-dependency, fully deterministic, no sanitizer needed). Rejected — rigid, limits expressiveness, and forces us to maintain a renderer per block type plus a layout engine for diagrams. LLMs are already fluent in HTML/SVG; sanitized raw HTML is more flexible with less code to maintain. (The block approach remains the fallback design if a zero-dependency hard constraint ever appears.)

**C. Hand-rolled HTML sanitizer (no dependency).** Rejected — rolling your own sanitizer is a known security footgun (mutation XSS, malformed-tag and encoding bypasses, SVG `foreignObject`). Use the maintained `nh3` and fail closed when it's absent.

**D. LLM generates the entire page HTML each run.** Rejected — the page shell is identical every time; regenerating it is fragile, slow, and non-deterministic. Only per-finding detail benefits from LLM authoring.

**E. Python scrapes `assessed.md` markdown to build the canvas.** Rejected — that *is* parsing free-form LLM prose (violates the principle) and can't carry rich visuals. The structured record is the contract instead.

### Why This Is Best

1. **Single source of truth** — structured records drive `review.md`, terminal, and canvas. No double output, no drift.
2. **Deterministic rendering** — Python templating is fast, testable, and consistent; the LLM does the semantic work (the reporter compiles records; the assessor authors rich detail).
3. **Best context for visuals** — the assessor already has the code in context and runs in parallel; authoring detail is incremental.
4. **Flexible yet safe** — raw HTML/SVG gives full expressiveness; `nh3` sanitization + CSP + escaping make it safe; fail-closed degradation keeps the no-dependency path secure.
5. **Validated UI** — the table/accordion/action-bar interaction is already proven in the prototype.
6. **Contained blast radius** — a malformed detail sidecar affects only that finding's panel; schema-validation errors are caught and the reporter is retried with a structured error.
7. **Respects the refined principle** — Python validates a schema-defined contract, never scrapes prose.

## Impact Assessment

- **Scope:** Reporter agent (writes the `records.json` envelope to disk instead of hand-authored `review.md`), assessor agent (optional rich `A-XX-detail.html` sidecar; primary markdown unchanged), `focused-review.py` (new `render-review`: validate + sanitize + server-side render md/terminal/canvas, plus a small capability/validate/expand surface for the orchestrator), orchestrator SKILL.md (early `nh3` detection, always-write canvas, ≤2-retry-then-fallback, namespaced postMessage handling via Python), new HTML template + CSS palette + morph-safe interactivity script (with `run_id`), new `nh3` dependency (installed out the gate, fail-closed)
- **Risk Level:** Medium — moves report rendering from the reporter agent into Python and changes the reporter's serialization (markdown → JSON). Mitigated by: discovery/consolidation/assessment phases untouched (still markdown), existing `review.md` + terminal outputs preserved (now Python-generated, format locked by tests), additive canvas, and fail-closed degradation.
- **Breaking Changes:** Reporter output format changes (markdown → `records.json`); assessor gains an optional sidecar. These are internal pipeline contracts, not user-facing. User-facing outputs (`review.md`, terminal summary) keep their shape.
- **Testing Requirements:**
  - Python: schema validation accepts valid records and rejects invalid ones with actionable errors
  - Python: `review.md` + terminal summary rendered from structured records match expected format
  - Python: text fields escaped (XSS payload in a finding title does not execute)
  - Sanitization: `nh3` present → `<script>`/`on*=`/`javascript:` stripped, safe tags + SVG allowlist preserved, inline `style` kept
  - Sanitization: script-carrying SVG (`<svg onload>`, `<foreignObject>`, external `href`, `<animate>`) stripped; a benign diagram survives
  - Sanitization: `nh3` absent → no raw HTML embedded, plain escaped text only (fail-closed)
  - Reporter reliability: malformed/truncated `records.json` → Python returns actionable error → ≤2 retries → legacy markdown fallback (canvas skipped)
  - Template renders correctly with sample data (with and without `detail_html`)
  - postMessage payloads validated/expanded via Python (namespaced action, `run_id` matches, `record_id`s exist) and require confirmation
  - CSP present: `script-src`/`style-src` `'unsafe-inline'`; `img/font/connect-src` block external `url()`/fetch
  - Graceful degradation: Treemon down → canvas file inert; no `nh3` → plain text
  - (manual) Morph-safety: a re-render preserves checked/disregarded/expanded state via document-level delegation

## Verification Strategy

1. Build a fixture set of structured records (valid + intentionally invalid) and assert Python's validation verdicts
2. Render `review.md` + terminal from fixtures; diff against expected
3. Feed an XSS payload through a finding field and a detail blob; confirm escaping + sanitization neutralize it (both `nh3`-present and `nh3`-absent paths)
4. Open the template with fixture data in a browser; verify table/accordion/section-checkboxes/action-bar (already validated in the prototype)
5. Verify postMessage validation + human-confirmation path
6. Run the full pipeline on a real branch; confirm canvas appears in Treemon and matches `review.md`
7. Degradation: no `nh3` → escaped text; Treemon not running → canvas file written but inert; malformed `records.json` → ≤2 retries then legacy markdown fallback

## Implementation Plan

### Phase 1: Template from prototype (standalone, testable)
1. Promote `.agents/canvas/focused-review-prototype.html` into `skills/focused-review/templates/review-canvas.html`
2. Convert it to a **page shell with placeholders** that Python fills with pre-rendered HTML (no client-side rendering, no `window.__REVIEW_DATA__`)
3. Add the CSS class palette (`.code-block`, `.highlight-line`, `.callout`, `.before-after`, `.flow`, …) for detail HTML
4. Add the CSP `<meta>` (keep **both** `script-src` and `style-src` at `'unsafe-inline'`; lock `img/font/connect-src` to `'self' data:`) and a small **morph-safe** action-bar script — in `<head>`, document-level event delegation — that posts `{ run_id, record_ids[], instructions }` under namespaced actions (`focused-review.*`, avoiding reserved names); accordion via `<details>`/`<summary>`
5. Test in browser with a hand-filled fixture

### Phase 2: Record schema + Python validation
1. Define the `records.json` **envelope** (`schema_version`, `run` metadata + counts, `rebuttal_overrides[]`, `rule_quality_notes[]`, `findings[]`); each finding has stable `record_id`/`assessment_id` + positional `display_number`, `original_severity` + final `severity`, and `has_detail`
2. Add `focused-review.py` validation (stdlib `json` + field checks) with structured, per-record error output for the reporter retry loop
3. Unit tests for accept/reject cases

### Phase 3: Python rendering (`render-review`, server-side)
1. Render `review.md` + terminal summary from `records.json` (replaces reporter's hand-authored output; **preserve the current `review.md` heading/field shape** and lock with a golden-file test so `post-mortem`/`post-comments` keep parsing it)
2. Render the full canvas HTML server-side (fill the template with pre-rendered rows/accordions/panels), **always** writing the file; embed `run_id` for action attribution
3. Escape all text fields (`html.escape`)
4. Tests: output format, golden `review.md`, escaping

### Phase 4: Sanitization (fail-closed) + capability detection
1. `try/except import nh3`; configure HTML **and** SVG allowlists (keep inline `style`; exclude `script`/`on*`/`animate`/`set`/`foreignObject`/external `href`)
2. Fail-closed: no `nh3` → strip detail HTML, plain escaped text only
3. Run the `nh3` detection **before assessment** (in orchestration) and pass the `rich_html` flag into assessor prompts, so sidecars aren't authored when they'd be rejected
4. Tests: HTML + SVG allowlist (script-carrying SVG stripped) + fail-closed path + XSS payload neutralized

### Phase 5: Reporter JSON output + assessor sidecar
1. Change `agents/review-reporter.agent.md` to **write** `{run_dir}/records.json` (the envelope) to disk instead of `review.md` + terminal text (keep all semantic work: verdicts, rebuttal overrides, rule-quality notes)
2. Add an **optional** rich `A-XX-detail.html` sidecar to `agents/review-assessor.agent.md` (primary markdown unchanged); provide the CSS class palette + **balanced** guidance (generate a visual only when it clarifies a non-obvious finding — neither always nor never)
3. Wire the orchestrator validation-error retry loop back to the reporter (**≤2 retries**, then fall back to the legacy hand-authored markdown path with canvas skipped)

### Phase 6: Orchestrator integration + postMessage actions
1. SKILL.md: early `nh3`/`rich_html` detection **before** assessment; always call `render-review` (canvas always written); ≤2 retries on validation errors then legacy fallback; relay Python's terminal summary
2. Handle `focused-review.fix`/`.disregard`/`.document`: validate/expand `{run_id, record_ids[]}` via Python against `records.json`, human confirmation, disregard persisted as run state
3. End-to-end test; version bump across all three manifests; `copilot plugin update`

## Resolved Design Decisions

The open questions from the original investigation were resolved in planning discussion:

- **Canvas write is unconditional.** Python always writes `.agents/canvas/focused-review.html` (gitignored, inert when Treemon is down). An LLM can't reliably introspect whether the `canvas` skill is loaded, so a best-effort skill-presence check only gates whether to *mention* the pane and *await* a `postMessage` — never the write. (`nh3` is a separate Python-import check.)
- **Treemon sandbox verified (in `tm-canvas48`, not yet main).** Iframe is `sandbox="allow-scripts allow-same-origin allow-forms allow-popups"` (not opaque), served from `127.0.0.1:5002`; parent app is `localhost:5000` (different origin) so the parent DOM is unreachable and `postMessage` is the only channel (origin-validated, `action` must be string, 64 KB cap). No server CSP header → our `<meta>` CSP is honored, **but** Treemon injects inline scripts so `script-src` must keep `'unsafe-inline'`; `nh3` is the real defense. Reserved action names to avoid: `navigate-canvas-doc`, `morph-complete`, `content-updated`. See Security Model.
- **Intermediate files stay markdown, unchanged.** `consolidated.md` and `assessed.md` remain agent inputs and the human-readable trail; Python never parses them. JSON appears **only** as the reporter's `records.json`, consumed solely by Python — no agent is forced to read JSON, and no new human-facing artifacts are added.
- **Reporter stays; it emits JSON.** The semantic compile work (verdict classification, rebuttal overrides, rule-quality notes) stays in the reporter agent; only its serialization changes (markdown → `records.json`). Python owns all presentation rendering. Minimal workflow change.
- **Format: a JSON envelope + raw-HTML sidecars.** `records.json` is an **envelope** (`schema_version`, `run` metadata, `rebuttal_overrides[]`, `rule_quality_notes[]`, `findings[]`) — not a bare array — because rule-quality notes, run counts, and overrides are cross-cutting. Findings carry **stable IDs** (`record_id`, `assessment_id`) distinct from positional `display_number`, and both `original_severity` + final `severity`. Validated with stdlib `json`. Rich detail rides in optional per-finding `A-XX-detail.html` sidecars (raw HTML/SVG, no escaping, sanitized by `nh3`). **XML + CDATA rejected** — moot once HTML is a sidecar, and a CDATA `]]>` in an untrusted diff would corrupt the record.
- **Reporter JSON reliability.** The reporter **writes to disk** (not chat); Python validates; on failure the orchestrator retries the reporter **≤2×**, then **falls back to the legacy hand-authored markdown path** (canvas skipped). The pipeline never hard-fails on a bad serialization.
- **Rebuttal overrides modeled explicitly.** `original_severity`/`severity` + `rebuttal_overrides[]` with reasoning; the renderer always shows the **final** verdict (and suppresses/annotates a sidecar that argues the overturned one).
- **postMessage contract.** Namespaced actions (`focused-review.fix`/`.disregard`/`.document`), payload `{ run_id, record_ids[], instructions }` using **stable** ids; the orchestrator validates/expands via Python against `records.json` (not context memory) and requires human confirmation. Avoids Treemon's reserved names; ≤64 KB.
- **Morph-safety + run attribution.** Interactivity script in `<head>` with document-level delegation; checked/disregarded/expanded state keyed by `record_id` and restored after `morph-complete`; "disregard" is persisted run state; `run_id` embedded in the HTML and every action payload (also fixes the global-filename staleness risk).
- **SVG allowed.** Via an `nh3` SVG allowlist (CSP handles exfil, `nh3` strips script-carrying tags). Inline `style` allowed without a CSS sanitizer — CSP `img/font/connect-src` already block the `url()` exfil vector.
- **`nh3`/`rich_html` detected before assessment.** An early Python capability check sets the `rich_html` flag passed into assessor prompts, so sidecars aren't authored where they'd be rejected.
- **No client-side rendering JS.** Python pre-renders the entire canvas HTML server-side (rows, accordions, detail panels, all `html.escape`d). No `window.__REVIEW_DATA__`. The only JS is a small **static** action-bar script for `postMessage` selection; accordion is CSS `<details>`/`<summary>`. Simpler and removes a script-injection surface.
- **Rich detail is voluntary.** The assessor authors a sidecar only when a diagram/snippet/before-after genuinely clarifies a non-obvious finding; trivial findings get none and fall back to text fields. The assessor prompt must be explicitly balanced — neither "always diagram" nor "never bother."
- **`post-comments` / `post-mortem` keep reading `review.md` (no migration).** These are **LLM** steps that read the markdown natively — so the "a forged `### 1.` heading fools the parser" risk is a *regex/Python* concern, not an LLM one. Migrating them to `records.json` would **not** fix prompt-injection either (the malicious field content reaches the consumer LLM's context regardless); it would only harden *structural* forgery, which an LLM already resists when Python renders each field into a clearly delimited block. So we keep `review.md` as the consumed format, render fields in delimited blocks, and lock the shape with a **golden-file test**. `records.json` stays available if we ever automate these modes (Python/no-LLM), but that's not needed now.
- **`nh3` ships out the gate (v2 from the start).** Sanitization + CSP + assessor sidecars are in scope for the initial implementation, not deferred — with fail-closed degradation if `nh3` is ever absent.

## Related Artifacts

- **`.agents/canvas/focused-review-prototype.html`** — the validated, data-faithful UI prototype (real data) that Phase 1 promotes into the template. This is the canonical UI reference.

## Next Steps

Use this investigation as input to `/bd-plan` for implementation planning.
