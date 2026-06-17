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
    "<!-- FR:QUESTIONABLE_COUNT -->",
    "<!-- FR:QUESTIONABLE_ROWS -->",
    "<!-- FR:INVALID_SUMMARY -->",
    "<!-- FR:INVALID_ROWS -->",
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


def test_script_posts_namespaced_action_payload(template_text: str):
    assert "window.parent.postMessage" in template_text
    # payload shape: { action, run_id, record_ids[], instructions }
    for key in ("action:", "run_id:", "record_ids:", "instructions:"):
        assert key in template_text, f"postMessage payload missing {key!r}"


@pytest.mark.parametrize("action", NAMESPACED_ACTIONS)
def test_template_action_buttons_namespaced(template_text: str, action: str):
    assert f'data-action="{action}"' in template_text


@pytest.mark.parametrize("reserved", RESERVED_ACTIONS)
def test_template_avoids_reserved_action_buttons(template_text: str, reserved: str):
    assert f'data-action="{reserved}"' not in template_text


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
    block = fixture_text.split('data-record-id="A-01"', 1)[1]
    cb = block.index('class="row-cb"')
    details = block.index("<details")
    assert cb < details


@pytest.mark.parametrize("cls", ["code-block", "highlight-line", "callout", "before-after", "flow"])
def test_fixture_exercises_palette(fixture_text: str, cls: str):
    assert f'class="{cls}' in fixture_text


def test_fixture_has_allowlisted_svg(fixture_text: str):
    assert "<svg" in fixture_text
