# Mark Fixed Findings on the Canvas

Give the focused-review canvas a persisted **`fixed`** state — a green ✓ / strikethrough row — that the orchestrator stamps onto findings it has actually fixed, mirroring the existing **disregard** machinery (persist state → bake a CSS class on every render). Full rationale and exact line-level anchors: [`.agents/fixed-findings-canvas-investigation.md`](../../.agents/fixed-findings-canvas-investigation.md).

## Goals

1. Persist which findings were fixed (`fixed` `record_id`s in `{run_dir}/run-state.json`) and bake a `.finding.fixed` class onto those rows on every (re-)render, so the mark survives reloads and same-run re-renders.
2. Make the mark fire automatically on **both** fix entry points — the canvas "🔧 Fix selected" button and a terminal "fix finding N" request — because the focused-review **orchestrator** owns both and already follows `SKILL.md`.
3. Render "fixed" as visually **done** (green ✓ + strikethrough title), distinct from disregard's 0.35 dim ("ignored").
4. Reuse the proven disregard pipeline (forgery gate, fail-open state, JS seeding, golden tests) rather than inventing new mechanisms; keep the render deterministic — no agent hand-editing of HTML.
5. Treat `fixed` and `disregarded` as **independent** states that can coexist on one finding.

## Expected Behavior

### Marking
- After the orchestrator applies user-approved fixes (either path), it runs `validate-action … --action focused-review.fix --apply-fixed --run-dir {run_dir}` with **only the `record_id`s it actually fixed**, then `render-review`. The canvas morphs the fixed rows to ✓.
- `--apply-fixed` writes state **only after a successful validation** and **only** when paired with the `focused-review.fix` verb (rejected otherwise — exit 1, no write), exactly like `--apply-disregard` is gated to `focused-review.disregard`. A forged/unknown `run_id` writes nothing.
- `fixed` is **add-only / monotonic** (first-seen order); a partial fix marks only what was resolved.

### Rendering
- `render-review` reads `fixed` from `run-state.json` (gated on the run's `run_id`) and bakes `.finding.fixed` onto matching `.finding` blocks. State is **fail-open**: missing/unreadable/`run_id`-mismatch ⇒ empty `fixed` set, never blocks the always-written canvas.
- A `.finding.fixed` row shows a green ✓ + strikethrough title (not the dim). A finding may carry both `fixed` and `dimmed`; when both apply the two treatments **stack** (`finding fixed dimmed`) — no precedence/"winner" logic.
- The action-bar JS seeds `state.fixed` **solely from server-rendered `.fixed` classes** in `restoreState()` (mirroring `dimmed`), so the mark drives both an in-session morph and a cold load. **No optimistic add** — the server round-trip is the single source of truth (the C-17 rule).

### Reset & limits
- A genuinely new review (new `run_id`) makes `load_run_state` ignore stale state ⇒ clean canvas. Regenerating from scratch on a new run is acceptable (explicitly out of scope to preserve).
- Out-of-band edits (no `SKILL.md` loaded) don't auto-mark; re-running the review regenerates the canvas. `validate-action … --apply-fixed` remains a callable primitive for any agent that knows `run_dir` + `record_id`s. (Accepted limitation.)

## Technical Approach

### Python — `skills/focused-review/scripts/focused-review.py`
> Anchors below are current-state hints (the file is ~4400 lines; the original investigation's line numbers were stale). **Locate by symbol** — earlier tasks shift later anchors.
- `load_run_state` (~4160): also parse/return `fixed: [...]` (default `[]`), same fail-open + `run_id`-gated semantics as `disregarded`. Keep returning the existing `disregarded` **and** `rule_fixes_applied` keys.
- The run-state envelope is **`{schema_version, run_id, disregarded, rule_fixes_applied}`** today, and both existing persisters route through a shared **`_write_run_state`** helper (~4196). Add a `fixed` param to `_write_run_state` (so it writes all four data keys), then add `persist_fixed(run_dir, run_id, record_ids)` mirroring `persist_disregard` (~4219; add-only/monotonic, first-seen order). All **three** persisters (`persist_disregard`, `persist_rule_fixes`, `persist_fixed`) must preserve every sibling key — routing through `_write_run_state` gives this for free. Do **not** hand-write a `{disregarded, fixed}` envelope (it would drop `rule_fixes_applied`).
- `_canvas_finding_block` (~3696; class baked ~3785 — `class_names = ["finding"]; if dimmed: append("dimmed")`) + `render_canvas_html` (~3843; params already `details, disregarded, parent_origin, *, invalidated`): add a `fixed` param (set / bool, default empty/false) and append `"fixed"` to the class list. `dimmed` already covers disregard **or** rule-fix invalidation, so `fixed` is orthogonal — a row can be `finding` / `finding dimmed` / `finding fixed` / `finding fixed dimmed`. The inner `block()` closure (~3899, `dimmed=(rid in disregarded or reason is not None)`) also passes `fixed=(rid in fixed)`.
- `render_review` (~3991): read `fixed` from `load_run_state` (beside `disregarded`/`invalidated` ~4063-4064) and pass it into the `render_canvas_html` call (~4071-4073).
- `validate_action_command` (~4689): add `--apply-fixed` (argparse ~5077 + handler) bound to `focused-review.fix`. Its **closest existing mirror is `--apply-rule-fixes`** (already fix-bound), not `--apply-disregard`: copy that flag's fail-fast gate (~4731) and co-located guard (~4791). But **persist like `--apply-disregard`** (~4775): the resolved **finding** ids (`[f["record_id"] for f in resolved["findings"]]`), **not** rules / `_accumulated_rule_fixes`. `focused-review.fix` then carries two independent side-effect flags (`--apply-rule-fixes` dims rule-invalidated rows; `--apply-fixed` marks code-fixed rows done) that may both fire and must not clobber each other.

### Template — `skills/focused-review/templates/review-canvas.html` + `review-canvas.fixture.html`
- Add a `.finding.fixed { … }` CSS rule near the `.finding.dimmed` block (template ~242, fixture ~151): green ✓ accent + strikethrough title, **not** the 0.35 dim, so "fixed" reads as done. A row may carry both `fixed` and `dimmed`.
- Add `fixed: new Set()` to the `state` object (~507) and, in `restoreState()` (template ~673-698, fixture ~536): seed `state.fixed` from server-rendered `.fixed` classes, prune to present ids, and re-apply `classList.toggle("fixed", …)`, mirroring `dimmed`; reconcile-from-server-only (no optimistic add). Leave the rule-fix preview block untouched.
- The two files' **executable JS must stay byte-identical** (`test_review_canvas_template.py::test_fixture_executable_js_identical_to_template` — only comments/attribute values may differ), so write the `fixed` seeding identically in both.

### SKILL.md — `skills/focused-review/SKILL.md`
- Step 6d's `fix` branch already splits into Findings (`f#`) and Rules (`RQ#`); the Rules sub-path already runs `--apply-rule-fixes` + `render-review`. Extend the **Findings (`f#`)** sub-path (which today stops after applying edits): after applying approved code-fixes, run `validate-action … --apply-fixed` (only the `f#` ids actually fixed) + `render-review`; relay the re-render summary. `--apply-fixed` (done) is distinct from `--apply-rule-fixes` (invalidation dim) — both may fire from one fix message. Also add `--apply-fixed` to the wrong-verb rejection note.
- Add brief **terminal-initiated fix** guidance (not a new mode): locate the latest run (Post-Mortem-style), map requested display numbers → `record_id`s from `records.json`, apply fixes with approval, then the same `--apply-fixed` + `render-review`.

### Docs & release
- `docs/spec/canvas-review-report.md` (beside the disregard entries, ≈179-184): document the `fixed` run-state key, the `--apply-fixed` flag/gate, the `.finding.fixed` class, and the JS seeding.
- Bump `version` in **all three** manifests (`plugin.json`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`) — `test_plugin_manifests.py` enforces consistency.

### Tests
- `test_validate_action.py`: `--apply-fixed` persists; gate rejects non-fix/absent verbs; forged `run_id` writes nothing.
- `test_render_review.py`: a fixed id bakes `.finding.fixed`; round-trips through `run-state.json`; `disregarded` + `fixed` coexist.
- `test_review_canvas_template.py`: both template files carry `.finding.fixed` CSS and seed `fixed` from the DOM; no optimistic add.

## Decisions

- **Option B (mirror disregard), not Option A (agent hand-edits HTML).** A deterministic server-baked class survives same-run re-renders and reloads; hand-patched HTML would be wiped by the next disregard/document re-render and is fragile.
- **`SKILL.md` is the single mechanism that makes the fixing agent "know."** Both fix paths land on the orchestrator, which already executes `SKILL.md`; the explicit `--apply-fixed` + `render-review` step there covers both. No new skill, no new `/review` mode.
- **`fixed` and `disregarded` are independent and may coexist** on the same finding — when both apply, **both treatments render (stack)**: green ✓ + strikethrough *and* the 0.35 dim (`finding fixed dimmed`). No precedence logic (user decision); the orthogonal CSS classes give this for free.
- **Release version `0.8.0 → 0.9.0` (minor bump).** Mark-fixed-findings is a new, backward-compatible feature, so it takes a minor bump per the repo convention (each feature release bumps minor: e.g. `0.7.1 → 0.8.0` was the interactive-canvas feature). All three manifests bumped together to satisfy `test_plugin_manifests.py::TestCrossFileConsistency`.
- **Finding ids are `f#` (rendered `F#`), never `r#`/`(rN)`.** The renderer/validator contract is `record_id = f"f{position}"` with `_FINDING_ID_RE = ^f[0-9]+$` (`focused-review.py`), and review.md headings are `### F#.` — globally unique, with no per-section display-number restart and no `(rN)` anchor. `validate-action --apply-fixed` rejects an `r#` id (exit 1, *before* the persist runs), so any mark-fixed instruction that constructs `r#` ids silently never writes the `fixed` state. The Step 6d Findings sub-path, the Step 6e terminal-fix path, and the Post-Mortem heading description must therefore use `f#`/`### F#.`. (Corrected in `tm-mark-fixed-findings-rv1`. Remaining pre-existing `r#` labels in the *descriptive* canvas-action intro prose — `SKILL.md` ~345/353/355 — and in `focused-review.py` error/docstring text are cosmetic, do not break behavior, and are tracked as separate cleanup.)
