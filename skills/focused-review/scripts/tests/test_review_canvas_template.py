"""Structural contract for the canvas review-report template (Phase 1).

The template is a static, version-controlled page shell that Python's future
`render-review` subcommand fills with pre-rendered HTML. These tests lock the
properties Phase 1 is responsible for so later phases (and morph/CSP behaviour)
can rely on them. The live interactivity is verified separately in a real browser
against ``review-canvas.fixture.html``.
"""

from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
TEMPLATES = REPO_ROOT / "skills" / "focused-review" / "templates"
TEMPLATE = TEMPLATES / "review-canvas.html"
FIXTURE = TEMPLATES / "review-canvas.fixture.html"

# Treemon reserved postMessage action names we must never emit as our own actions.
RESERVED_ACTIONS = ["navigate-canvas-doc", "morph-complete", "content-updated"]

# Placeholders Python fills in `render-review`.
PLACEHOLDERS = [
    "{{RUN_ID}}",
    "{{PARENT_ORIGIN}}",
    "<!-- FR:META -->",
    "<!-- FR:SUMMARY_BADGES -->",
    "<!-- FR:CONFIRMED_COUNT -->",
    "<!-- FR:CONFIRMED_ROWS -->",
    "<!-- FR:NEEDS_DECISION_COUNT -->",
    "<!-- FR:NEEDS_DECISION_ROWS -->",
    "<!-- FR:PREEXISTING_COUNT -->",
    "<!-- FR:PREEXISTING_ROWS -->",
    "<!-- FR:QUALITY_SUMMARY -->",
    "<!-- FR:QUALITY_NOTES -->",
]

# Rich-detail CSS palette the assessor sidecars rely on.
PALETTE_CLASSES = [".code-block", ".highlight-line", ".callout", ".before-after", ".flow"]

NAMESPACED_ACTIONS = [
    "focused-review.fix",
    "focused-review.disregard",
    "focused-review.document",
]

# The trusted Treemon parent-app origin the canvas pins its postMessage channel to
# (mirrors DEFAULT_PARENT_ORIGIN in focused-review.py); the fixture bakes it in by hand.
TRUSTED_PARENT_ORIGIN = "http://localhost:5000"


def _strip_html_comments(text: str) -> str:
    """Remove <!-- ... --> comments so doc-comments don't trip content assertions."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _executable_js(html_text: str) -> list[str]:
    """Return the action-bar script's executable lines (comments + blanks stripped).

    HTML comments are removed first so the doc-comment's literal ``<script>`` mention
    cannot confuse extraction; then JS block/line comments and blank lines are dropped
    and each surviving line's internal whitespace is collapsed. The template and the
    fixture must share byte-identical executable JS — only their comments and their
    filled-in attribute values (run id, parent origin) are allowed to differ — so a
    browser test of the fixture genuinely exercises the template's behaviour. (Safe
    line-comment stripping relies on the action-bar JS holding no ``//`` string
    literals, which the no-URL-in-JS design guarantees.)
    """
    no_html_comments = _strip_html_comments(html_text)
    body = re.search(r"<script>(.*?)</script>", no_html_comments, re.DOTALL).group(1)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    lines = []
    for raw in body.splitlines():
        line = re.sub(r"\s+", " ", re.sub(r"//.*$", "", raw)).strip()
        if line:
            lines.append(line)
    return lines


@pytest.fixture(scope="module")
def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# ── files exist ──────────────────────────────────────────────────────────────


def test_template_exists():
    assert TEMPLATE.is_file(), f"missing template: {TEMPLATE}"


def test_fixture_exists():
    assert FIXTURE.is_file(), f"missing fixture: {FIXTURE}"


# ── placeholders / page-shell (no client-side rendering) ─────────────────────


@pytest.mark.parametrize("placeholder", PLACEHOLDERS)
def test_template_has_placeholder(template_text: str, placeholder: str):
    assert placeholder in template_text, f"template missing placeholder {placeholder!r}"


def test_template_has_no_client_side_data_blob(template_text: str):
    # All markup is pre-rendered server-side; there is no client-side data blob.
    # (The doc-comment may name the token to say it is absent, so check real markup.)
    assert "__REVIEW_DATA__" not in _strip_html_comments(template_text)


def test_template_has_no_inline_event_handlers(template_text: str):
    # Morph-safety: interactivity is document-level delegation, never inline handlers
    # (which depend on globals that a body morph would orphan).
    for handler in ("onclick=", "onchange=", "onload=", "oninput="):
        assert handler not in template_text, f"unexpected inline handler {handler!r}"


# ── Content Security Policy (CSP = exfil, nh3 = script) ──────────────────────


def test_template_has_csp_meta(template_text: str):
    assert 'http-equiv="Content-Security-Policy"' in template_text


@pytest.mark.parametrize(
    "directive",
    [
        "script-src 'unsafe-inline'",
        "style-src 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self' data:",
        "connect-src 'self' data:",
    ],
)
def test_template_csp_directives(template_text: str, directive: str):
    assert directive in template_text, f"CSP missing {directive!r}"


# ── rich-detail palette ──────────────────────────────────────────────────────


@pytest.mark.parametrize("cls", PALETTE_CLASSES)
def test_template_defines_palette_class(template_text: str, cls: str):
    assert cls in template_text, f"palette class {cls!r} not defined in template CSS"


# ── verdict-model sections: confirmed / needs-decision / pre-existing, no invalid ──


VISIBLE_SECTIONS = ["confirmed", "needs-decision", "pre-existing"]


@pytest.mark.parametrize("section", VISIBLE_SECTIONS)
def test_template_has_visible_section(template_text: str, section: str):
    # The three visible buckets each get a labelled <section> shell.
    assert f'data-section="{section}"' in template_text, f"template missing section {section!r}"


@pytest.mark.parametrize("dropped", ["invalid", "questionable"])
def test_template_drops_old_sections(template_text: str, dropped: str):
    # Invalid is records-only (D-15); "Questionable" was relabelled "Needs your decision".
    assert f'data-section="{dropped}"' not in template_text


def test_template_has_no_invalid_markers(template_text: str):
    # No Invalid section header, badge, or fill markers survive anywhere in the shell.
    assert "FR:INVALID" not in template_text
    assert "FR:QUESTIONABLE" not in template_text
    assert ".badge-invalid" not in template_text
    assert ".invalid-table" not in template_text


def test_template_section_order_confirmed_then_decision_then_preexisting(template_text: str):
    # D-17 canvas order: Confirmed → Needs your decision → Pre-existing.
    i_conf = template_text.index('data-section="confirmed"')
    i_need = template_text.index('data-section="needs-decision"')
    i_pre = template_text.index('data-section="pre-existing"')
    assert i_conf < i_need < i_pre


# ── fix-cost tag (visual large-fix marker) ───────────────────────────────────


def test_template_defines_fix_tag_css(template_text: str):
    # The large-fix tag pill and its row tint must be styleable in the shell.
    assert ".fix-tag" in template_text
    assert ".finding.costly" in template_text


def test_fixture_defines_fix_tag_css(fixture_text: str):
    assert ".fix-tag" in fixture_text
    assert ".finding.costly" in fixture_text


def test_fixture_exercises_fix_tag(fixture_text: str):
    # The hand-filled twin marks at least one finding as a large fix so the tag +
    # tint are exercised by the browser test.
    assert 'class="finding costly"' in fixture_text
    assert 'class="fix-tag"' in fixture_text


@pytest.mark.parametrize("section", VISIBLE_SECTIONS)
def test_fixture_has_visible_section(fixture_text: str, section: str):
    assert f'data-section="{section}"' in fixture_text, f"fixture missing section {section!r}"


def test_fixture_has_no_invalid_section(fixture_text: str):
    # The fixture renders no Invalid section or table (records-only, D-15).
    assert 'data-section="invalid"' not in fixture_text
    assert "invalid-section" not in fixture_text
    assert "invalid-table" not in fixture_text


# ── accordion via <details>/<summary> ────────────────────────────────────────


def test_template_uses_details_summary(template_text: str):
    assert "<details" in template_text and "<summary" in template_text


# ── morph-safe action-bar script in <head> ───────────────────────────────────


def test_script_lives_in_head(template_text: str):
    head_end = template_text.index("</head>")
    script_start = template_text.index("<script")
    assert script_start < head_end, "interactivity <script> must be in <head> (morph-safe)"


def test_script_uses_document_level_delegation(template_text: str):
    assert "document.addEventListener" in template_text


def test_script_posts_unified_action_payload(template_text: str, fixture_text: str):
    assert "window.parent.postMessage" in template_text
    # Unified, prefix-disambiguated payload (verdict-model redesign) plus the top-level
    # `action` Treemon's inbound canvas gate requires:
    #   { action: "<namespaced verb>", ids: [f#/rq#], button: "<bare verb>", text, run_id }
    for key in ("action:", "ids:", "button:", "text:", "run_id:"):
        assert key in template_text, f"postMessage payload missing {key!r}"
    # `action` is the FULL namespaced verb (the data-action value), not the bare button —
    # Treemon drops any inbound message whose `action` isn't a string, so it must ride
    # along verbatim. (`button` still carries the namespace-sliced bare verb.) Asserted on
    # BOTH ends of the channel so the fix can never drift between template and fixture.
    for text in (template_text, fixture_text):
        assert "action: action," in text, "postMessage payload missing top-level `action`"
    # The truly-legacy keys stay gone — this was an API migration, not an additive change,
    # so the old shape's `record_ids` / `instructions` must not linger.
    for legacy in ("record_ids:", "instructions:"):
        assert legacy not in template_text, f"legacy payload key {legacy!r} still present"
    # ids unions the selected findings (f#) with the scheduled rule fixes (rq#),
    # button is the bare verb (the namespace prefix sliced off), text is the box.
    assert "Array.from(state.selected).concat(Array.from(state.scheduledRules))" in template_text
    assert "button: action.slice(NS.length)" in template_text
    assert "text: instructions()" in template_text


def test_script_enables_bar_for_findings_or_rules(template_text: str, fixture_text: str):
    # The action bar must enable on a findings-only, rules-only, OR mixed selection so
    # a rule-quality-only fix can be dispatched; both ends count the union of the two.
    for text in (template_text, fixture_text):
        assert "state.selected.size + state.scheduledRules.size" in text


def test_script_clears_scheduled_rules_on_dispatch(template_text: str, fixture_text: str):
    # Dispatch resets BOTH selections (findings + scheduled rule fixes) and re-derives
    # the live preview, so a follow-up action starts clean and the persisted dim (from
    # the agent's re-render) becomes the only post-action source of truth.
    for text in (template_text, fixture_text):
        assert "state.scheduledRules.clear()" in text


def test_script_shares_postaction_helper(template_text: str, fixture_text: str):
    # The action buttons and the Enter-to-ask handler both post through one helper so the
    # payload shape (and the top-level `action` Treemon requires) can never drift between
    # the two paths. Asserted on both ends of the channel.
    for text in (template_text, fixture_text):
        assert "function postAction(action, ids)" in text
        assert "postAction(action, ids);" in text


def test_script_enter_submits_free_text_question(template_text: str, fixture_text: str):
    # A follow-up question must be submittable from the free-text box alone — pressing Enter,
    # with no finding selected and no action button pressed. This is the `ask` verb.
    for text in (template_text, fixture_text):
        # Enter-key handler wired to the free-text box via document-level delegation.
        assert 'document.addEventListener("keydown"' in text
        assert 'e.key !== "Enter"' in text
        assert 'classList.contains("instructions-input")' in text
        # Posts the namespaced ask verb through the shared helper; an empty box is a no-op,
        # and the box is cleared after the question is sent.
        assert 'postAction(NS + "ask"' in text
        assert "if (!instructions()) return;" in text
        assert 'box.value = ""' in text


def test_script_ask_path_needs_no_selection(template_text: str, fixture_text: str):
    # The ask handler must NOT gate on a selection (that is the buttons' contract): it
    # guards on an empty text box instead, so a plain question with zero selected findings
    # still posts. Guard against a regression that copies the buttons' `if (!ids.length)`.
    keydown_block = re.search(
        r'addEventListener\("keydown".*?\}\);', template_text, re.DOTALL
    )
    assert keydown_block, "keydown (Enter-to-ask) handler not found"
    assert "if (!ids.length) return;" not in keydown_block.group(0)


@pytest.mark.parametrize("action", NAMESPACED_ACTIONS)
def test_template_action_buttons_namespaced(template_text: str, action: str):
    assert f'data-action="{action}"' in template_text


@pytest.mark.parametrize("reserved", RESERVED_ACTIONS)
def test_template_avoids_reserved_action_buttons(template_text: str, reserved: str):
    assert f'data-action="{reserved}"' not in template_text


# ── C-17: no optimistic disregard dimming (server round-trip is the source of truth) ──


def test_template_has_no_optimistic_disregard_dimming(template_text: str):
    # The disregard click must not optimistically apply the `dimmed` class before the
    # orchestrator validates/confirms. Dimming is seeded only from server-rendered
    # run-state via restoreState(), so a declined/rejected confirmation can never leave
    # a finding dimmed out of step with persisted run-state. `dimmed` may only be read
    # (classList.contains) or reconciled (classList.toggle) — never added on dispatch.
    assert 'classList.add("dimmed")' not in template_text
    assert 'action === NS + "disregard"' not in template_text


def test_fixture_has_no_optimistic_disregard_dimming(fixture_text: str):
    # Checked on the fixture explicitly too: test_fixture_executable_js_identical_to_template
    # only proves the two ends match each other, so without this both could re-introduce
    # the optimistic dim in lockstep and still pass.
    assert 'classList.add("dimmed")' not in fixture_text
    assert 'action === NS + "disregard"' not in fixture_text


# ── fixed: green-check / strikethrough "done" mark (distinct from the disregard dim) ──


def test_template_defines_fixed_css(template_text: str):
    # A fixed finding renders as DONE — a strikethrough title + a green check glyph, NOT
    # the 0.35 dim — so "fixed" never reads as "ignored". Orthogonal to .dimmed: a row may
    # carry both `fixed` and `dimmed` and the two treatments stack.
    assert ".finding.fixed" in template_text


def test_fixture_defines_fixed_css(fixture_text: str):
    assert ".finding.fixed" in fixture_text


def test_template_seeds_fixed_from_dom(template_text: str):
    # restoreState() seeds state.fixed SOLELY from server-rendered .fixed classes
    # (mirroring dimmed), so the persisted mark drives both an in-session morph and a
    # cold load. fixed: new Set() must exist in the state object for this to land.
    assert "fixed: new Set()" in template_text
    assert 'f.classList.contains("fixed")' in template_text
    assert "state.fixed.add(" in template_text


def test_fixture_seeds_fixed_from_dom(fixture_text: str):
    assert "fixed: new Set()" in fixture_text
    assert 'f.classList.contains("fixed")' in fixture_text
    assert "state.fixed.add(" in fixture_text


def test_template_has_no_optimistic_fixed_marking(template_text: str):
    # Single source of truth (the C-17 rule, extended to `fixed`): the mark is seeded only
    # from server-rendered run-state via restoreState(), never optimistically added on a
    # fix click. `fixed` may only be read (classList.contains) or reconciled
    # (classList.toggle) — never added on dispatch.
    assert 'classList.add("fixed")' not in template_text


def test_fixture_has_no_optimistic_fixed_marking(fixture_text: str):
    # Checked on the fixture explicitly too: test_fixture_executable_js_identical_to_template
    # only proves the two ends match each other, so without this both could re-introduce
    # an optimistic fix-marking in lockstep and still pass.
    assert 'classList.add("fixed")' not in fixture_text


# ── F2 regression: restoreState rebuilds the server-derived sets fresh (no stale re-add) ──


def _restore_state_js(html_text: str) -> str:
    """restoreState()'s executable JS (comments stripped) onward, as one normalized string.

    The reset/seed/toggle tokens checked below are unique to restoreState(), so slicing from
    its definition to the end of the script is enough to assert their presence and ordering.
    """
    js = "\n".join(_executable_js(html_text))
    return js[js.index("function restoreState()") :]


@pytest.mark.parametrize("end", ["template", "fixture"])
def test_restore_state_rebuilds_server_sets_fresh(template_text: str, fixture_text: str, end: str):
    # F2 regression. The canvas is one morph-in-place file whose JS state survives across runs
    # (window.__frCanvasInit), and record_ids are positional (f1..fN every run). If restoreState()
    # UNIONED the server-derived sets into prior client state (the old add-only seed), a finding
    # fixed/dimmed in an earlier run would be RE-applied to the same-position id in a genuinely
    # new run (new run_id) whose server render correctly dropped the class — a false done/ignored
    # mark that defeats the "new run_id => clean canvas" goal. The two server-derived sets must be
    # rebuilt FRESH from the current DOM each call (state.<set> = new Set() before the reseed), so
    # they exactly mirror the server-baked classes and never accumulate cross-run state. (selected
    # is genuine client state and is intentionally NOT reset.)
    body = _restore_state_js(template_text if end == "template" else fixture_text)
    for reset in ("state.disregarded = new Set()", "state.fixed = new Set()"):
        assert reset in body, f"restoreState must rebuild {reset!r} fresh (F2 stale re-add)"
    # The fresh rebuild must precede the reseed adds, else it would wipe the just-seeded ids.
    assert body.index("state.disregarded = new Set()") < body.index("state.disregarded.add(")
    assert body.index("state.fixed = new Set()") < body.index("state.fixed.add(")


# ── origin-validated postMessage channel (C-03 outbound / C-15 inbound) ──────


def test_template_outbound_postmessage_pins_parent_origin(template_text: str):
    # C-03: the action post must target the trusted parent origin, never the wildcard
    # "*" (which would broadcast run_id + free-text instructions to any framing parent).
    assert '}, "*")' not in template_text, "outbound postMessage still uses wildcard origin"
    assert "}, getParentOrigin());" in template_text
    assert "function getParentOrigin()" in template_text
    assert "[data-parent-origin]" in template_text


def test_template_inbound_listener_validates_origin(template_text: str):
    # C-15: the inbound message listener must reject foreign origins before restoring
    # state, the counterpart contract to the pinned outbound target.
    assert "e.origin !== getParentOrigin()" in template_text


def test_template_body_carries_parent_origin_placeholder(template_text: str):
    # Threaded as data, mirroring data-run-id, so the executable JS stays identical to
    # the fixture's (only the filled-in origin differs).
    assert 'data-parent-origin="{{PARENT_ORIGIN}}"' in template_text


def test_fixture_outbound_postmessage_pins_parent_origin(fixture_text: str):
    assert '}, "*")' not in fixture_text, "fixture outbound postMessage still uses wildcard origin"
    assert "}, getParentOrigin());" in fixture_text


def test_fixture_inbound_listener_validates_origin(fixture_text: str):
    assert "e.origin !== getParentOrigin()" in fixture_text


def test_fixture_embeds_trusted_parent_origin(fixture_text: str):
    assert f'data-parent-origin="{TRUSTED_PARENT_ORIGIN}"' in fixture_text


def test_origin_helper_normalizes_value(template_text: str, fixture_text: str):
    # getParentOrigin() must trim + strip trailing slashes so a misconfigured
    # --parent-origin like "http://host:5000/" can't break the inbound e.origin
    # comparison (e.origin is never trailing-slashed) or the postMessage target.
    for text in (template_text, fixture_text):
        assert ".trim()" in text
        assert r".replace(/\/+$/" in text


def test_instructions_input_has_accessible_name(template_text: str, fixture_text: str):
    # The free-text action-bar input needs an accessible name (not just placeholder text),
    # matching the aria-labels already on the row/section checkboxes.
    for text in (template_text, fixture_text):
        assert 'class="instructions-input"' in text
        assert 'aria-label="Instructions for the selected findings"' in text


def test_fixture_executable_js_identical_to_template(template_text: str, fixture_text: str):
    # The fixture is the hand-filled twin of the template; their action-bar JS must stay
    # byte-identical (modulo comments + filled attribute values) so the origin-validation
    # fix can never drift between the two ends of the channel.
    assert _executable_js(fixture_text) == _executable_js(template_text)


# ── fixture: fully hand-filled and exercises the contract ────────────────────


def test_fixture_has_no_unfilled_placeholders(fixture_text: str):
    assert "{{" not in fixture_text, "fixture has an unfilled scalar placeholder"
    assert "<!-- FR:" not in fixture_text, "fixture has an unfilled block placeholder"


def test_fixture_embeds_run_id(fixture_text: str):
    assert 'data-run-id="test-run-20260616-112540"' in fixture_text


def test_fixture_uses_exclusive_accordion(fixture_text: str):
    # native one-open-at-a-time accordion via the shared name attribute
    assert 'name="findings"' in fixture_text


def test_fixture_checkbox_outside_summary(fixture_text: str):
    # the per-row checkbox must precede <details>, never inside <summary>,
    # so selecting a finding never toggles its accordion.
    block = fixture_text.split('data-record-id="f1"', 1)[1]
    cb = block.index('class="row-cb"')
    details = block.index("<details")
    assert cb < details


@pytest.mark.parametrize("cls", ["code-block", "highlight-line", "callout", "before-after", "flow"])
def test_fixture_exercises_palette(fixture_text: str, cls: str):
    assert f'class="{cls}' in fixture_text


def test_fixture_has_allowlisted_svg(fixture_text: str):
    assert "<svg" in fixture_text
