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


def _strip_html_comments(text: str) -> str:
    """Remove <!-- ... --> comments so doc-comments don't trip content assertions."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


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
