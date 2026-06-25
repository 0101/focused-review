# Unified Finding & Rule Numbering (Python-Owned Display Layer)

> Feature spec. Refines `docs/spec/verdict-model-redesign.md` and **supersedes** several of its decisions (D-16 per-bucket numbering, D-23 record_id heading anchor, Decision-20 LLM-emitted `display_bucket`, the `r#`/`RQ#` id formats, the separate `display_number` field). Driven by user report + the `20260624-141540` focused-review findings (r1, r3, r6, r8, r10).

## Problem

Two parallel numbering schemes disagree, so the number a user **sees** rarely matches the id used to **reference** a finding:

- `record_id` (`r1, r2…`) — globally unique, assigned by the **reporter LLM** across *all* findings including never-shown Invalid/hidden ones (so it has gaps), and used as the canvas/action key and post-mortem anchor.
- `display_number` (the visible `#2`) — **restarts per visible bucket** (D-16), so `#1` appears in every section; not globally unique.

Live evidence (`20260624-141540/records.json`): finding `r10` renders as `#1`, `r8` as `#2`. The LLM also scrambles `record_id` order vs display order because it is **forced to emit derivable fields** (`display_bucket`, `display_number`, ordering) — finding **r6**. Per-bucket numbering additionally broke two downstream consumers: the terminal summary shows duplicate `#` (**r3**) and **post-comments** silently drops findings (**r1**, High) and keys on the now-non-unique integer (**r8**); rule-quality notes also mis-resolve chunk-suffixed provenance (**r10**).

## Goals

1. **One identifier**: what the user sees == what's in `review.md` == what's used to reference a finding. No second number.
2. **Findings** use id `f1…fN`; **rule-quality notes** use `rq1…rqN`. The id *is* the visible label (rendered uppercase `F2` / `RQ1`).
3. **Globally gap-free over visible findings**, in display order. Invalid/hidden findings get trailing ids and are never shown.
4. **Python owns the entire display layer** (r6): the reporter LLM emits only *semantic* fields; Python deterministically assigns ids, bucket, ordering, and counts — making `f# == visible position` a guaranteed invariant, not an LLM instruction.
5. Fix the coupled regressions the rename/renumber introduced: **post-comments** (r1, r8) and **rule-source chunk-suffix resolution** (r10).

## Expected Behavior

### Identifier scheme
- Finding id format: `^f[0-9]+$` (lowercase in data); rule-quality note id: `^rq[0-9]+$`.
- Rendered label is **uppercase**: `F2`, `RQ1` — on the canvas badge, `review.md` headings, the summary table, and the terminal table.
- User/agent references are resolved **case-insensitively** (`F2`, `f2` → `f2`).
- `f#` numbers are assigned **visible-first, gap-free `1..N`, in display order** (section order: Confirmed → Needs Your Decision → Pre-existing; within a section, sorted deterministically by `file` then `line`). Invalid + pre-existing-needs-decision (the `hidden` bucket) receive **trailing** `f#` ids (`f{N+1}…`) and are never rendered.
- The separate `display_number` field is **removed** — the numeric part of `f#` *is* the display number. `record_id` is **renamed** to the `f#` scheme (one id field, not two).

### Python-owned display layer (r6)
The reporter LLM emits per finding only the **semantic** fields it genuinely owns: `verdict`, `introduced_by`, `severity` (+ `original_severity`), `type`, `provenance`, `title`, `description`, `assessment`, `suggestion`, `fix_complexity`, and detail-sidecar markers (`assessment_id`/`has_detail`). For rule-quality notes it emits `observation`, `suggestion`, and canonical **rule identity** (`rule_file` + the list of provenance `rule_sources` it covers). It does **not** emit `record_id`/`f#`, `display_number`, `display_bucket`, `rq#`, or `run.*` counts.

Python then deterministically (no parsing of prose — purely from structured fields):
1. derives `display_bucket` from `(verdict, introduced_by)` (`_derive_display_bucket`),
2. orders findings (bucket order, then `file`/`line`),
3. assigns `f1…fN` to visible findings and trailing `f#` to hidden ones,
4. assigns `rq1…rqN` to rule-quality notes,
5. computes `run.confirmed/questionable/invalid`,
6. persists the enriched `records.json`.

This eliminates the whole "LLM emitted a wrong derivable field → envelope rejected → retry" class and removes the corresponding prompt sections from `review-reporter.agent.md`.

### Rendering
- Canvas: `.num` badge shows the uppercase id (`F2`); `data-record-id` carries the lowercase id (`f2`); aria labels use the id.
- `review.md`: heading is `### F2. [Severity] Title` (the id is the leading token; the old `### {n}. (rN)` shape and the redundant `(rN)` anchor are gone).
- Summary table and **terminal** actionable table both key on `F#` — unique across buckets, aligned with `review.md` (fixes r3, r8).

### Action contract / run-state
- Canvas payload `{ids, button, text, run_id}` carries `f#`/`rq#` ids. `validate-action` prefix-dispatches `f` → finding, `rq` → rule (case-insensitive), rejects unknown prefixes/ids.
- `run-state.json` `disregarded` / `rule_fixes_applied` reference `f#`/`rq#` ids (pre-release — no migration).
- Resolved-rule / accumulated-fix / persisted-run-state rule provenance is `rule_sources` (a **list**), aligning with the note schema (notes carry `rule_sources`, a non-empty list of `rule--<name>` labels — a singular field would always be lossy). It is vestigial audit data: only the `rule_id` (map key) and `invalidated_record_ids` drive invalidation/dimming, so the field has no behavioral impact (`_resolve_action_rule`, `_accumulated_rule_fixes`, `persist_rule_fixes`, `_sanitize_rule_fixes`, `_collect_rule_file_errors`).

### post-comments (r1, r8)
- `POST-COMMENTS.md` Step 4a reads the **renamed** sections (`Confirmed Findings`, `Needs Your Decision`, `Pre-existing`) and maps section → verdict; the stale `Questionable Findings` literal is removed.
- Inline-comment identity and the `--exclude` selector switch from the non-unique positional integer to the globally-unique **`f#`** id (the `--exclude` CLI changes from integer-typed to `f#`-string-typed; update the test-locked contract).
- **Pre-existing findings post to the PR review body** (not inline), consistent with the current out-of-diff handling — they are never suppressed entirely.
- Add the renamed sections to the golden/shape lock so a future producer rename can't silently desync the consumer again.

### Rule-source chunk-suffix resolution (r10)
- Provenance labels are chunk-suffixed (`rule--general-review--1`) while rule files are not (`general-review.md`). Normalize a trailing `--<digits>` when relating a provenance label to a rule file, in both `_rule_file_source_mismatch` and `_rule_dependency_map` (a note's `rule_sources` may list several chunk labels of the same rule). Retain the C-12 protection: the normalized stem must resolve to a real rule file under `rules_dir`.
  - **Implementation (chunk suffix = `--<digits>`, matching the dispatch `findings_path` `rule--<name>--<chunk_index>`, SKILL.md Phase 1):** a shared `_strip_chunk_suffix` (regex `--[0-9]+$`) reduces both a label (`rule--general-review--1` → `rule--general-review`) and a bare stem (`general-review--1` → `general-review`) to canonical form. In `_rule_file_source_mismatch` the canonical source name must equal the `rule_file` stem (the **raw** name is *also* accepted, so a rule whose filename legitimately ends in `--<digits>` still matches its own `--<digits>.md`); in `_rule_dependency_map` both the note `rule_sources` keys and the finding provenance labels are canonicalized so every chunk of one rule resolves to its single note. The "real rule file under `rules_dir`" guarantee is the **existing path-safety gate** (`_validate_rule_file` runs before the cross-check in both callers, proving `rule_file` is a safe `.md` under `rules_dir`) — *not* a new filesystem-existence check (which would break the path-only validation contract and the relative-path test fixtures); normalization forgives only the chunk suffix, never a genuinely different rule name (a real mismatch like `rule--general-review--1` vs `simplicity.md` is still rejected). The `_validate_rule_quality_note` uniqueness check canonicalizes the label the same way (chunk suffix stripped, matching `_rule_dependency_map`) so two **separate** notes naming the same rule via different chunk labels (`rule--gr--1` vs `rule--gr--2`) are rejected as a cross-note duplicate (Finding F1) — without that, the later note collapses to a phantom whose checkbox invalidates nothing and chunk-2 findings mis-attribute to the first note. A **single** note may still list every chunk (`rule--gr--1`, `rule--gr--2`): within-note canonical repeats all resolve to that one note, so they are de-duplicated rather than flagged.
    - **One note per rule — reporter contract (Finding F2):** a note edits exactly one `rule_file`, and the per-source cross-check (`_rule_file_source_mismatch`, run for *every* label in `_collect_rule_file_errors` / `_validate_rule_quality_note`) requires that file's stem to match each `rule_sources` label after chunk-suffix stripping. So all labels in one note must name the **same** rule (their only legitimate variation is the `--<chunk>` suffix); a note covering two *different* rules (e.g. `["rule--no-comments", "rule--simplicity"]` + `rule_file no-comments.md`) is rejected on the mismatching label, and a distinct rule needs its own note. The reporter prompt (`agents/review-reporter.agent.md`) must therefore advertise this — *"list several chunk labels when one rule was split across discovery chunks"*, **not** *"a single fix touches multiple rules"* — so a prompt-following reporter never emits an envelope validation must reject (which would force a retry/fallback). Tests `test_note_rule_file_match_runs_per_source` (different-rule label rejected) and `test_note_rule_file_matches_every_chunk_label_of_one_rule` (every chunk of one rule accepted) lock the contract.

## Technical Approach (anchors, verify at execution)

- Numbering/validation: `focused-review.py` `_derive_display_bucket` (~80-118), `validate_records` (~2322+), `_validate_finding` display_number block (~2344-2375), `_partition_findings` (~3025-3034), counts (`_validate_run_counts`).
- New deterministic assignment step (assign `f#`/`rq#`/bucket/order/counts and rewrite `records.json`) — likely folded into the validate/render entry path or a dedicated `finalize-records` subcommand invoked by the reporter phase.
  - **Decision (D-F3, finalize ownership):** folded into **each consumer's entry path**, *not* a standalone `finalize-records` subcommand. `render-review` persists the finalized file; `validate_records_command` **and** `validate_action_command` call `finalize_records` **in memory** (idempotent, never persisted) before reading the display layer. So every command is self-sufficient and the implicit "render-review must run first" coupling is gone — validate-action resolves `f#`/`rq#` against any validated `records.json`, not only an already-rendered one. A dedicated subcommand was rejected: it would only *relocate* the ordering dependency (now "finalize-records must run first") into the orchestrator and SKILL.md for no behavioral gain.
  - **Decision (D-F4, atomic persistence):** `_write_text` writes to a same-directory temp file and `os.replace`s it over the target (atomic on POSIX + Windows), so render-review's in-place `records.json` overwrite — the reporter's semantic source of truth — can never be truncated to a half-write by an interrupt; this also hardens the `review.md` / canvas / `run-state.json` writes that share the helper. Existing-file mode is preserved (else umask-default) so artifacts keep `open(path, "w")` permissions despite `mkstemp`'s 0o600.
- Render: `_canvas_finding_block` (`.num` ~3508/3580, `data-record-id` ~3575), `_md_finding_block` (~3109-3127), summary table (~3233-3251), terminal table (~3235).
- Action: `validate_action` (~3473-3592) + `validate_action_command`; canvas payload (`review-canvas.html` ~625) + fixture twin.
- post-comments: `post-comments` subcommand + `POST-COMMENTS.md` Step 4a/6/7/8; `--exclude` CLI + `comments.json` schema; `test_post_comments.py` (integer `--exclude` lock ~:400).
- Rule-source: `_rule_file_source_mismatch`, `_rule_dependency_map` (~3004).
- Reporter contract: `agents/review-reporter.agent.md` (remove the display-layer schema notes + Step 4 + Step 7 self-check).
- SKILL: `SKILL.md` post-mortem step + Step 6d mixed-selection.
- Tests: `test_records_schema.py`, `test_render_review.py`, `test_validate_action.py`, `test_review_canvas_template.py`, `test_post_comments.py`, `test_plugin_manifests.py`.

## Superseded Decisions (from verdict-model-redesign)

- **D-16** (per-bucket `display_number`) → replaced by **global gap-free** numbering.
- **D-23** (keep leading integer + `(rN)` anchor for post-comments) → replaced by the single `f#` id everywhere; post-comments keys on `f#`.
- **Decision 20** (reporter emits `display_bucket`) → Python derives it.
- `r#`/`RQ#` id formats → `f#`/`rq#`; the separate `display_number` field is removed.

## Explicitly Not in Scope

The other `20260624-141540` findings unrelated to numbering — r4 (pending-invalid stays selected), r5 (nested rule_file stem), r7 (CSS duplication), r9 (validate-action validation/mutation split), and the security/Invalid items (r2, r11, r12) — are **out of scope** for this feature.

## Out-of-Run Verification

Deterministic CLI + pytest: assignment produces gap-free `f1..fN` in display order with `f# == #N`; hidden findings never rendered and never consume a visible number; canvas/`review.md`/terminal all show the same `F#`; mixed-id `validate-action` resolves `f#`/`rq#` case-insensitively and rejects unknown prefixes; post-comments reads the renamed sections and excludes by `f#`; chunk-suffixed rule provenance resolves to its rule file; full suite green.
