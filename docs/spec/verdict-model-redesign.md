# Verdict Model & Rule-Quality Redesign

> Feature spec. Extends the system described in `docs/spec/focused-review.md`. Source plan: `.agents/canvas/focused-review-redesign-plan.html`.

## Goals

1. **Make the review easily consumable** — every bucket maps to a single user action. The verdict answers two questions: *is it real?* and *can the agent commit to the fix, or does a human need to decide?*
2. Replace the legacy "Questionable" semantics with **"Needs your decision"** — real findings the agent cannot unilaterally commit to (unclear action *or* a human-owned worth-it call, including disproportionate cost).
3. **Invalid means false-positive only** — and is **never rendered** (no Invalid section anywhere; kept in `records.json` only for the false-positive count + rule-quality signal).
4. **Surface pre-existing net-positive findings** in their own non-gating section instead of discarding them.
5. **Close the rule-quality loop**: a rule-quality note becomes mandatory whenever a valid rule match isn't a net positive, and notes become **schedulable** — check a note, preview which findings it invalidates, and apply the rule fix.
6. **One unified, prefix-disambiguated action** that handles any mix of findings (`r#`) and rule-quality notes (`RQ#`) plus free text, with the trust boundary staying in Python.

## Verdict Model (the consumable buckets)

Scope (`introduced_by`) is a separate, orthogonal field — **not** a 4th verdict. The only severity effect is that a **Critical** issue is always must-fix (Confirmed).

| Bucket | Verb | Meaning |
|---|---|---|
| **Confirmed** | Fix | Real, fixing it is a net positive, and there's a clear single action the agent can just take — no human decision needed. **Critical/urgent bugs stay here even when the fix is large or risky** (must-fix overrides "is it worth it?"). |
| **Needs your decision** (was "Questionable") | Decide | Real and worth attention, but the agent can't unilaterally commit — either the right action is unclear (competing approaches / trade-off / ambiguity), *or* whether to do it at all is a human-owned judgment (net-positive change with disproportionate cost, or one the agent leans against). Each item **names the decision and carries the agent's recommendation** (e.g. "suggest skip"). *Critical never waits here.* |
| **Pre-existing** (Confirmed only) | Consider | Net-positive issue **not introduced by this change**. Own section, non-gating. |
| **Invalid** | — | **False-positive only.** **Never shown.** Retained internally in `records.json` for the false-positive count + the rule-quality signal. |

**Net-positive test** = benefit (correctness / security / clarity / maintainability) vs. cost (churn, regression risk, review burden). "Better off once addressed," *not* "we already have a cheap patch."

**Large/costly fixes are always tagged — orthogonally to the bucket.** A tag + subtle row tint driven by the existing `fix_complexity` field, in any bucket. The tag is not routing: a Critical bug with a big fix stays Confirmed; a non-critical net-positive whose cost makes "is it worth it?" a real question is routed to Needs your decision — both still carry the tag.

**Rule findings aren't a separate bucket.** They flow into the buckets above. Because rules are the authority, **any valid match — clear-cut or judgment-call** — lands in **Confirmed**; a false match → Invalid. Rule findings are **never** "Needs your decision". If a match is technically right but the rule is too blunt here, the agent Confirms it **and** attaches a mandatory **rule-quality note** (the only escape).

## Invariants (must always hold)

- **Invalid = false-positive only.** A real issue is never Invalid.
- **Confirmed (concerns) = real ∧ net-positive ∧ a clear action the agent can just take.**
- **Rules are the authority — the one exception to "Confirmed = net-positive".** A valid rule match is Confirmed even if the local fix isn't a net positive; the **mandatory rule-quality note** is the only escape.
- **Needs your decision = real, but the agent can't commit alone.** Net-positive findings *can* live here (e.g. disproportionate cost) — this replaces the old "nothing net-positive in Questionable" rule.
- **Severity doesn't demote.** Only effect: Critical → always Confirmed (must-fix), regardless of fix size.
- **Fix cost is a visual tag, orthogonal to the bucket.** It can additionally make a non-critical item a decision, but never hides or downgrades.
- **Pre-existing-but-doubtful findings are recorded, not shown** (only clear net-positives surface in the Pre-existing section).

## Scope Routing (verdict × where the code lives)

Scope is the orthogonal `introduced_by` flag. The "only net-positives surface" rule lives in the renderer, so verdicts stay honest.

| Verdict (on merits) | In-scope (diff) | Pre-existing (diff-independent) |
|---|---|---|
| Confirmed | Fix — Confirmed section | Consider — Pre-existing section |
| Needs your decision | Decide — decision section | recorded only — not shown |
| Invalid | never rendered — internal only | never rendered — internal only |

- Pre-existing **rule** violations stay out of scope entirely — not recorded, not shown. Only pre-existing *concerns* can surface (and only when Confirmed).
- Pre-existing items are **bounded by design**: they only ever arrive through normal discovery and happen to be pre-existing. Nothing hunts for them; the assessor simply stops discarding the net-positive ones it's handed, and weighs "this may have been intentional" as a counter-argument.

## Rule-Quality Notes → Schedulable Fixes

The rule-quality notes section already exists (assessor emits a note; reporter aggregates). Changes:

- **Assessor tweak**: make a note **mandatory** whenever a *valid* rule match isn't a net positive (today it's optional).
- **UI additions**: a **checkbox per note** (each shows its suggested change, with a distinct id prefix `RQ#`), plus preview/apply behavior.

### Which findings a rule fix invalidates

A finding dies only when **all of its sources are rules being fixed** — it has **no concern source**, and **every** rule that flagged it is in the applied set. A single-rule finding dies when that rule is applied; a two-rule finding (`rule:R, rule:S`) dies only when *both* are applied. **Any concern source keeps the finding alive** (independent justification — no "re-evaluate" middle state).

### Flow — preview, then apply

1. **Preview (deterministic, Python).** `render-review` computes, per note, the `record_id`s a fix *could* invalidate (findings whose only sources are rules), keyed so the canvas knows which rows depend on which rules. Checking boxes **live-greys** the rows that would die once *all* their rule sources are checked — instant, no agent round-trip.
2. **Schedule.** Checked boxes accumulate into a "scheduled rule fixes" set.
3. **Apply.** Clicking the action sends the current selection (findings and/or rules) plus the instructions-box text back to the agent; the **agent edits the relevant rule files in `review/`**, then writes the invalidated `record_id`s into run-state and re-renders. Re-render alone can't drop findings (it renders from `records.json`); the persisted invalidation set is what makes rows disappear deterministically.

### Run-state reuse

Each run has a `run-state.json` beside `records.json` that already persists `disregarded` record_ids (add-only, `run_id`-stamped, re-applied on every re-render). Rule-fix invalidation is a **sibling key** — no new mechanism:

```json
{ "schema_version": 1, "run_id": "20260203-100000", "disregarded": ["r3"],
  "rule_fixes_applied": [ { "rule_id": "RQ2", "rule_source": "rule--no-comments",
                            "invalidated_record_ids": ["r5", "r7"] } ] }
```

`render-review` unions `disregarded` + every `invalidated_record_ids` to decide which rows are non-actionable. Invalidated rule-findings are **dimmed with a reason** ("invalidated — rule RQ2 fixed"), reusing the disregard dim mechanism, rather than hard-dropped — keeps an audit trail.

**Implementation note (Python APIs already in place).** The preview/persist plumbing exists and is the contract the apply path wires up:
- `_rule_dependency_map(findings, notes) -> {record_id: [RQ#, …]}` is the single source of truth for *which* findings a scheduled set of rule fixes invalidates (per D-12: rule-only sources, every rule noted). Given an applied RQ-id set `A`, the dying records are `[rid for rid, deps in map.items() if set(deps) <= A]`. The canvas also threads each row's deps onto `data-rule-deps` for the live-grey preview.
- `persist_rule_fixes(run_dir, run_id, fixes) -> state` writes the `rule_fixes_applied` sibling key (add-only, merged by `rule_id`, `run_id`-stamped, preserves `disregarded`) — the mirror of `persist_disregard`. `render-review` reads it back via `_invalidated_reasons` and dims with the reason pill. The production caller is `validate-action --apply-rule-fixes` (bound to the `fix` verb, post-confirmation): it resolves the posted RQ ids → `fixes` (computing each rule's `invalidated_record_ids` against the *full* posted set per D-12) and calls `persist_rule_fixes`. Persist **all** scheduled RQ ids in one call — a multi-rule finding only dies once every one of its rules is in that call.

### Message contract — one message, prefix-disambiguated ids

One unified message handles whatever the user selected — findings, rule-quality notes, or a mix. **Ids carry a type prefix** so a single flat list is unambiguous: findings keep their `record_id` (`r1`, `r2`…); rule-quality notes get a distinct prefix (`RQ1`, `RQ2`…). The message carries the selected ids, the button pressed, and any typed text:

```json
{ "ids": ["r3", "r7", "RQ2"], "button": "fix", "text": "<free text or empty>", "run_id": "<run id>" }
```

(`run_id` is retained from today's payload because run-state matching requires it.) The **agent resolves each id by prefix and handles any combination**. Rule semantics: a rule id with **no text = accept its suggested change**; **with text = do what the text says** (the suggestion is context). Python's action-expander resolves the mixed id list deterministically → per finding (file/line/title/suggestion) and per rule (file path, suggested change, invalidated `record_id`s), so the agent has full context regardless of the mix.

**CLI surface.** The orchestrator translates the payload to `validate-action`: `ids` → `--ids` (the heterogeneous list), `text` → `--instructions`, and `button` → `--action focused-review.{button}`. The CLI keeps the **namespaced** verb (`--action`, `choices=VALID_ACTIONS`) as the allowlist/trust boundary — the payload's bare `button` is never trusted as a verb; an off-allowlist value is rejected by argparse (exit 2). `validate-action` returns both `findings[]` and `rules[]`; two gated, verb-bound flags persist run-state after a confirmed action — `--apply-disregard` (disregard verb, resolved **finding** ids) and `--apply-rule-fixes` (fix verb, resolved **rules'** `invalidated_record_ids`).

## Expected Behavior (by component)

### `agents/review-assessor.agent.md` (substantive)
- Redefine the three verdicts per the table above: Confirmed = real ∧ net-positive ∧ clear action; Needs your decision = real but agent can't commit alone; Invalid = false-positive only.
- **Critical → always Confirmed (must-fix)**, even with a large/risky fix. Route only *non-critical* net-positives with disproportionate cost (or that the assessor leans against) to Needs your decision. When leaning against, emit an explicit **recommendation** (e.g. "suggest skip — cost outweighs benefit").
- **Remove** both the "Low severity + disproportionate → Invalid" path and the severity-driven "risk genuinely low → Questionable" routing. Severity becomes a pure side field except the Critical must-fix rule.
- Rule-sourced findings flow into the normal buckets — valid match (clear-cut or judgment-call) → Confirmed, false match → Invalid; never Needs your decision. **Mandatory rule-quality note** whenever a valid rule match isn't a net positive (tighten from today's optional), identifying the rule canonically so the reporter can resolve it.
- Pre-existing concerns: **stop auto-Invalid**; assess on merits; tag `introduced_by: pre-existing`. Only the *assigned* finding; never go hunting; never raise new incidental findings. Weigh possibly-intentional history as a counter-argument.

### `agents/review-reporter.agent.md` + `records.json` schema (schema changes)
- Verdict envelope values stay `Confirmed / Questionable / Invalid` (the "Needs your decision" label is applied at render time — keeps the envelope stable).
- **Enforce id formats** so prefixes can't collide: findings `^r[0-9]+$`, rule-quality notes `^RQ[0-9]+$` (today only "non-empty unique string").
- **Structure the rule-quality notes**: add an `id` (`RQ#`) and a **canonical** `rule_source` / `rule_file` (validated to live under `rules_dir`) so Python can resolve the rule deterministically and safely — not from free-text rule names. (Today a note is `{ rule, observation, suggestion }`.)
- **Carry scope/display structurally** so hidden/pre-existing items don't break verdict counts or `display_number` validation. Recommended: a `display_bucket` field derived from `(verdict, introduced_by)` — `confirmed` | `needs-decision` | `pre-existing` | `hidden` — making the visible/counted set explicit and illegal states unrepresentable. `display_number` becomes contiguous **per visible bucket**; pre-existing Confirmed is excluded from the main Confirmed tally and is non-gating; pre-existing Needs-your-decision and all Invalid are `hidden` (recorded, not shown).

### `skills/focused-review/scripts/focused-review.py` + canvas template (substantive)
- Add a **Pre-existing** section (Confirmed-only, non-gating); **filter out** pre-existing + Needs-your-decision (record only, don't display).
- **Remove the Invalid section entirely** from the canvas and `review.md`, and drop Invalid from the summary table. Findings stay in `records.json` only.
- **Relabel** the Questionable section → **"Needs your decision"**.
- **Visually flag large/costly fixes** (tag + subtle row tint) from the existing `fix_complexity` field — pure presentation, any bucket.
- Compute the **rule-dependency map** (deterministic, from provenance): per finding, which rule sources it has and whether it has any concern source.
- **Section order** (top→bottom): Confirmed (Fix, critical first) → Needs your decision (Decide) → Pre-existing (Consider) → Rule quality (Improve the rule). No Invalid section.
- Add **run-state persistence** for applied rule fixes — a `rule_fixes_applied` sibling key in the existing `run-state.json`; `render-review` unions it with `disregarded` and **dims invalidated rows with a reason**.
- **Rule-quality section gains checkboxes**: a checkbox per note (shows its suggested change, id prefix `RQ#`); checking live-greys mapped rows. Selections ride the existing single send action.
- **Unified action contract (API migration, not just docs):** extend the canvas payload + the `validate-action` subcommand to accept a heterogeneous id list, resolve `r#` findings *and* `RQ#` rules, and **reject unknown ids/prefixes** (trust boundary stays in Python). Update the action result schema + tests.

### `skills/focused-review/SKILL.md` (substantive)
- Document handling of a **mixed selection** (findings + rules + text): resolve ids by prefix, fix the findings, and for each rule apply its **suggested change** when no text is given, otherwise **follow the typed text**; edit rule files in `review/`, write invalidated ids to run-state, then re-render.
- **Narrow the rebuttal prompt** (Phase 4 / "Step 5"): now that Invalid = false-positive only, it should contest only **factual false-positive dismissals**, and respect the new scope policy (don't reinstate a pre-existing item that should be recorded/hidden).
- Update phase / summary wording to the new labels ("Needs your decision", Pre-existing section) so the orchestrator's user-facing summary and the `assessed.md` verdict counts match.

## Technical Approach (key code anchors)

Current-code anchors gathered during planning (line numbers approximate, verify at execution):

- **Verdicts enum**: `VALID_VERDICTS = ("Confirmed", "Questionable", "Invalid")` (`focused-review.py` ~line 72) — unchanged; label mapping happens in render.
- **Records validation**: `validate_records` (~2322+); `display_number` contiguity check (~2391-2399); `introduced_by` type-only check (~2188-2192).
- **Partition / sections**: `_partition_findings` (~2702-2714); `render_canvas_html` (~3170-3242); canvas template `skills/focused-review/templates/review-canvas.html` (Invalid section template ~361-395; placeholders `<!-- FR:CONFIRMED_ROWS -->`, `<!-- FR:QUALITY_NOTES -->`, etc.).
- **Run-state**: `load_run_state` / `persist_disregard` (~3393-3445); shape `{ schema_version, run_id, disregarded[] }`; add-only, run_id-stamped; re-applied via `render_review` (~3337-3353).
- **Action contract**: `validate_action` (~3469-3592) + CLI `validate_action_command` (~3657-3719); today `VALID_ACTIONS = ("focused-review.fix", ".disregard", ".document")`, payload `{ action, run_id, record_ids[], instructions }`, result includes resolved findings.
- **Reporter envelope**: `records.json` = `{ schema_version, run, rebuttal_overrides[], rule_quality_notes[], findings[] }`; finding fields include `record_id, assessment_id, display_number, title, file, line, original_severity, severity, fix_complexity, verdict, type, introduced_by, description, assessment, suggestion, provenance, has_detail`; `rule_quality_notes` = `{ rule, observation, suggestion }`.
  - **Implemented by `focused-review-m3i` (schema foundation) — new shape, authoritative contract is `agents/review-reporter.agent.md`:** findings gain a required derived `display_bucket` (`confirmed` | `needs-decision` | `pre-existing` | `hidden`); `record_id` matches `^r[0-9]+$`; `display_number` is contiguous **per visible bucket** (each of confirmed/needs-decision/pre-existing starts at 1) and is `null` for `hidden`. `rule_quality_notes` is now `{ id (^RQ[0-9]+$), rule, rule_source, rule_file, observation, suggestion }` with `rule_file` validated to be a safe `.md` path under `rules_dir` (default `review/`). **Run counts:** `run.confirmed`/`run.questionable` are the visible-bucket tallies (`confirmed`/`needs-decision`; pre-existing excluded per Decision 16); `run.invalid` stays the Invalid-verdict tally (false-positive count). `validate_records(data, *, rules_dir=None)` and `load_and_validate_records(path, *, rules_dir=None)` thread the configured rules dir; the `validate-records`/`render-review` CLIs resolve it from config. Downstream renderer (`focused-review-cvr`) and action (`focused-review-d84`) tasks update their own fixtures/tests for this shape.
- **SKILL phases**: Discovery → Consolidation → Assessment → (Rebuttal) → Presentation; rebuttal reads `assessed.md` for Critical/High Invalid (~231-254); canvas detection via `capabilities` → `rich_html` (~179-185).
- **Tests**: `skills/focused-review/scripts/tests/` — `test_records_schema.py`, `test_validate_action.py`, `test_render_review.py`, `test_review_canvas_template.py`, `test_sanitize_detail.py`, `test_plugin_manifests.py` (version consistency across the three manifests).

**Suggested implementation order** (also encoded as task dependencies): schema/validation foundation → assessor verdict logic → renderer buckets/sections → rule-quality preview + run-state invalidation → unified mixed-id action contract → SKILL rebuttal + wording → version bump.

## Explicitly Not Changing

- Discovery agents / concerns — **no new detection**, no hunting for pre-existing issues.
- The core pipeline **phase structure** stays (Discovery → Consolidation → Assessment → Rebuttal → Presentation) — but the rebuttal prompt is narrowed.
- Rules remain absolute; pre-existing **rule** violations stay out of scope.
- Existing canvas infrastructure (section/row checkboxes, free-text box, send-to-agent) is **reused and extended**, not rebuilt.

## Decision Log (locked)

1. Invalid = false-positive only. A real issue is never Invalid.
2. Confirmed (concerns) = real ∧ net-positive ∧ a clear action the agent can just take.
3. Questionable → "Needs your decision" — real but the agent can't commit alone; net-positive findings can live here (e.g. disproportionate cost). Each item names the decision.
4. Critical → always Confirmed (must-fix), even with a large/risky fix. Severity otherwise doesn't demote.
5. Fix cost is a visual tag, orthogonal to the bucket; it can additionally make a non-critical net-positive a decision, but never hides/downgrades.
6. Rules are the authority — any valid match (clear-cut or judgment-call) → Confirmed, never Needs your decision; mandatory rule-quality note when a valid match isn't net-positive (the one exception to Confirmed=net-positive).
7. Pre-existing rule violations stay out of scope (not recorded, not shown).
8. Don't hunt pre-existing; assessor reassesses only its assigned finding; never raises new ones.
9. Pre-existing net-positive concern → own section via the `introduced_by` scope flag (not a 4th verdict); carried structurally so counts/numbering stay valid.
10. Pre-existing-but-doubtful is recorded, not shown (Confirmed-only surfaces).
11. Rule-quality checkbox = preview-then-apply; applying persists invalidated ids to run-state, then re-renders (re-render alone can't drop findings).
12. A finding dies only when all its sources are applied rules (no concern source); multi-rule findings need all their rules applied; any concern source keeps it alive.
13. Reuse + extend the canvas checkbox + send-to-agent infrastructure; the unified mixed-id action is a Python-validated API migration (trust boundary stays in Python).
14. One unified message, prefix-disambiguated ids: findings (`r#`) and rule-quality notes (`RQ#`, formats enforced) ride the same send action; payload = ids + button + text (+ run_id). Rule id with no text = accept its suggestion; with text = follow it.
15. Invalid is never rendered — no Invalid section on the canvas or in `review.md`, and not in the summary table. Kept in `records.json` only.
16. Pre-existing Confirmed: not counted in the main tally, non-gating — its own reference ids, actionable, but excluded from the Confirmed count and never blocks.
17. Canvas section order: Confirmed → Needs your decision → Pre-existing → Rule quality.
18. Run-state reuses the existing `run-state.json`: rule fixes add a `rule_fixes_applied` sibling key beside `disregarded`; invalidated rows are dimmed with a reason (not hard-dropped), re-applied on every re-render.
19. Lean-against decision items carry an explicit recommendation ("suggest skip — cost outweighs benefit") so they're one-glance actionable.
20. Scope normalization lives in Python (`_is_pre_existing` / `_derive_display_bucket`), not in the assessor vocabulary. The assessor keeps its four-value `introduced_by` (`diff | pre-existing | reclassified-pre-existing | reclassified-diff`) because the `reclassified-*` distinction is meaningful provenance; the deterministic trust boundary suffix-matches `pre-existing` (so `pre-existing` **and** `reclassified-pre-existing` route to the non-gating pre-existing/hidden buckets) and treats everything else (`diff`, `reclassified-diff`, empty/absent) as in-scope. Chosen over constraining the assessor to two values so the routing is fail-safe regardless of which spelling an agent emits.
