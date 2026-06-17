"""Tests for the ``render-review`` subcommand (Phase 3).

Covers the three server-side artifacts rendered from a validated ``records.json``
envelope: ``review.md`` (heading/field shape locked by a golden-file test, since
the post-mortem mode parses it), the terminal summary string, and the canvas
HTML (template fill + ``html.escape`` of every structured text field). Also
covers the CLI handler: always-written canvas, default paths, and the
validation-failure exit path.
"""

from __future__ import annotations

import argparse
import copy
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

# Absolute path to the script for real-subprocess (encoding) tests.
SCRIPT_PATH = str(Path(__file__).resolve().parent.parent / "focused-review.py")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _render_envelope() -> dict:
    """A representative, fully valid envelope used by the golden + format tests.

    Exercises: two Confirmed findings (one multi-source/grouped provenance, one
    single rule source), one Questionable finding with a null line and empty
    assessment/suggestion (tests field omission), one Invalid finding (separate
    table keyed by assessment_id), and a rule-quality note.
    """
    return {
        "schema_version": 1,
        "run": {
            "run_id": "20260203-100000",
            "scope": "branch",
            "date": "2026-02-03T10:00:00Z",
            "rule_count": 5,
            "concern_count": 3,
            "consolidated_count": 4,
            "confirmed": 2,
            "questionable": 1,
            "invalid": 1,
        },
        "rebuttal_overrides": [],
        "rule_quality_notes": [
            {
                "rule": "no-comments",
                "observation": "4 findings, all Invalid in embedded template code.",
                "suggestion": "Add an exception for embedded templates.",
            }
        ],
        "findings": [
            {
                "record_id": "r1",
                "assessment_id": "A-01",
                "display_number": 1,
                "title": "Null deref in request handler",
                "file": "src/a.py",
                "line": 10,
                "original_severity": "High",
                "severity": "High",
                "fix_complexity": "moderate",
                "verdict": "Confirmed",
                "type": "concern",
                "description": "The handler dereferences req.user without a null check.",
                "assessment": "Confirmed by reading the call site; user can be null on the error path.",
                "suggestion": "Guard req.user before access.",
                "provenance": [
                    {"source": "concern--bugs--opus"},
                    "concern--bugs--codex",
                    "rule--null-safety",
                ],
                "has_detail": False,
            },
            {
                "record_id": "r2",
                "assessment_id": "A-02",
                "display_number": 2,
                "title": "Duplicated parsing logic",
                "file": "src/b.py",
                "line": 20,
                "original_severity": "Medium",
                "severity": "Medium",
                "fix_complexity": "complex",
                "verdict": "Confirmed",
                "type": "rule",
                "description": "The same parse block appears in three methods.",
                "assessment": "Real duplication, though refactoring touches a hot path.",
                "suggestion": "Extract a shared parse helper.",
                "provenance": ["rule--simplicity"],
                "has_detail": False,
            },
            {
                "record_id": "r3",
                "assessment_id": "A-05",
                "display_number": 3,
                "title": "Magic number in retry loop",
                "file": "src/c.py",
                "line": None,
                "original_severity": "Low",
                "severity": "Low",
                "fix_complexity": "quickfix",
                "verdict": "Questionable",
                "type": "concern",
                "description": "Retry count 5 is hardcoded.",
                "assessment": "",
                "suggestion": "",
                "provenance": ["concern--style--gemini"],
                "has_detail": False,
            },
            {
                "record_id": "r4",
                "assessment_id": "A-09",
                "display_number": None,
                "title": "Section divider comment",
                "file": "src/d.py",
                "line": 5,
                "original_severity": "Low",
                "severity": "Medium",
                "fix_complexity": "quickfix",
                "verdict": "Invalid",
                "type": "rule",
                "description": "A // ---- divider.",
                "assessment": "Navigational aid in long embedded template.",
                "suggestion": "",
                "provenance": ["rule--no-comments"],
                "has_detail": False,
            },
        ],
    }


# The golden review.md for ``_render_envelope()``. This locks the heading/field
# shape the reporter agent used to hand-author (and which the post-mortem mode
# parses: the "### {n}." headings and the rule:/concern: provenance labels).
GOLDEN_REVIEW_MD = """# Unified Review Report

**Scope:** branch
**Date:** 2026-02-03T10:00:00Z
**Pipeline:** Discovery (5 rules, 3 concerns) → Consolidation → Assessment

## Summary

| Verdict | Count |
|---------|-------|
| ✅ Confirmed | 2 |
| ❓ Questionable | 1 |
| ❌ Invalid (filtered) | 1 |

---

## Confirmed Findings

### 1. [High] Null deref in request handler

**File:** `src/a.py:10`
**Fix complexity:** moderate
**Found by:** 3 sources: concern:bugs (opus), concern:bugs (codex), rule:null-safety

The handler dereferences req.user without a null check.

> **Assessment:** Confirmed by reading the call site; user can be null on the error path.

**Suggestion:** Guard req.user before access.

---

### 2. [Medium] Duplicated parsing logic

**File:** `src/b.py:20`
**Fix complexity:** complex
**Found by:** 1 source: rule:simplicity

The same parse block appears in three methods.

> **Assessment:** Real duplication, though refactoring touches a hot path.

**Suggestion:** Extract a shared parse helper.

---

## Questionable Findings

### 3. [Low] Magic number in retry loop

**File:** `src/c.py`
**Fix complexity:** quickfix
**Found by:** 1 source: concern:style (gemini)

Retry count 5 is hardcoded.

---

<details>
<summary>1 finding filtered as invalid</summary>

| ID | Severity | File | Title | Reason |
|----|----------|------|-------|--------|
| A-09 | Medium | `src/d.py:5` | Section divider comment | Navigational aid in long embedded template. |

</details>

## Rule Quality Notes

- **no-comments**: 4 findings, all Invalid in embedded template code. — Add an exception for embedded templates.
"""


def _canvas(env: dict) -> str:
    template = fr.CANVAS_TEMPLATE_PATH.read_text(encoding="utf-8")
    return fr.render_canvas_html(env, template)


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
# review.md — golden + structural contract
# ---------------------------------------------------------------------------


class TestReviewMarkdownGolden:
    def test_golden_exact_match(self) -> None:
        assert fr.render_review_markdown(_render_envelope()) == GOLDEN_REVIEW_MD

    def test_ends_with_single_newline(self) -> None:
        out = fr.render_review_markdown(_render_envelope())
        assert out.endswith("templates.\n")
        assert not out.endswith("\n\n")

    def test_heading_shape_is_postmortem_parseable(self) -> None:
        # Post-mortem matches "### {n}. [{sev}] {title}" headings.
        out = fr.render_review_markdown(_render_envelope())
        assert "### 1. [High] Null deref in request handler" in out
        assert "### 3. [Low] Magic number in retry loop" in out

    def test_found_by_labels_are_postmortem_parseable(self) -> None:
        # Post-mortem parses "rule:{name}" and "concern:{name} ({model})".
        out = fr.render_review_markdown(_render_envelope())
        assert "concern:bugs (opus)" in out
        assert "concern:bugs (codex)" in out
        assert "rule:null-safety" in out
        assert "concern:style (gemini)" in out
        # Count prefix: 3 distinct provenance entries on the first finding.
        assert "**Found by:** 3 sources: " in out
        assert "**Found by:** 1 source: rule:simplicity" in out

    def test_null_line_omits_colon(self) -> None:
        out = fr.render_review_markdown(_render_envelope())
        assert "**File:** `src/c.py`" in out
        assert "src/c.py:" not in out

    def test_empty_assessment_and_suggestion_omitted(self) -> None:
        # Only the two Confirmed findings have assessment/suggestion text.
        out = fr.render_review_markdown(_render_envelope())
        assert out.count("> **Assessment:**") == 2
        assert out.count("**Suggestion:**") == 2

    def test_confirmed_section_omitted_when_none(self) -> None:
        env = _render_envelope()
        # Keep only the Invalid finding.
        env["findings"] = [f for f in env["findings"] if f["verdict"] == "Invalid"]
        env["run"].update(consolidated_count=1, confirmed=0, questionable=0, invalid=1)
        out = fr.render_review_markdown(env)
        assert "## Confirmed Findings" not in out
        assert "## Questionable Findings" not in out
        assert "filtered as invalid" in out

    def test_quality_notes_section_omitted_when_none(self) -> None:
        env = _render_envelope()
        env["rule_quality_notes"] = []
        out = fr.render_review_markdown(env)
        assert "## Rule Quality Notes" not in out

    def test_invalid_block_omitted_when_none(self) -> None:
        env = _render_envelope()
        env["findings"] = [f for f in env["findings"] if f["verdict"] != "Invalid"]
        env["run"].update(consolidated_count=3, invalid=0)
        out = fr.render_review_markdown(env)
        assert "filtered as invalid" not in out
        assert "<details>" not in out

    def test_pipe_in_title_escaped_in_invalid_table(self) -> None:
        env = _render_envelope()
        inv = next(f for f in env["findings"] if f["verdict"] == "Invalid")
        inv["title"] = "use A | B operator"
        out = fr.render_review_markdown(env)
        assert r"use A \| B operator" in out


# ---------------------------------------------------------------------------
# Terminal summary
# ---------------------------------------------------------------------------


class TestTerminalSummary:
    def test_header_and_pipeline_line(self) -> None:
        out = fr.render_terminal_summary(_render_envelope(), "run/review.md")
        assert out.startswith("📄 run/review.md\n")
        assert "5 rules + 3 concerns → 4 unique findings → 3 actionable" in out

    def test_table_rows_and_verdict_icons(self) -> None:
        out = fr.render_terminal_summary(_render_envelope(), "run/review.md")
        assert "| # | Verdict | Severity | Found by | File | Issue |" in out
        # Confirmed → ✅, Questionable → ❓; grouped short "Found by".
        assert "| 1 | ✅ | High | bugs(opus,codex), rule:null-safety | src/a.py:10 |" in out
        assert "| 3 | ❓ | Low | style(gemini) | src/c.py |" in out

    def test_quality_notes_block(self) -> None:
        out = fr.render_terminal_summary(_render_envelope(), "run/review.md")
        assert "📝 Rule Quality Notes" in out
        assert "- no-comments: 4 findings, all Invalid" in out

    def test_no_relay_trailer(self) -> None:
        # The "relay everything above this line" trailer is the orchestrator's job
        # (SKILL.md), not the tool's — render-review output is pure data.
        out = fr.render_terminal_summary(_render_envelope(), "run/review.md").lower()
        assert "relay everything above" not in out
        assert "verbatim" not in out
        assert "this is the final response" not in out

    def test_no_actionable_findings(self) -> None:
        env = _render_envelope()
        env["findings"] = [f for f in env["findings"] if f["verdict"] == "Invalid"]
        env["run"].update(consolidated_count=1, confirmed=0, questionable=0, invalid=1)
        out = fr.render_terminal_summary(env, "run/review.md")
        assert "✅ No actionable findings." in out
        assert "| # | Verdict |" not in out


# ---------------------------------------------------------------------------
# Canvas HTML
# ---------------------------------------------------------------------------


class TestCanvasRender:
    def test_no_unfilled_placeholders(self) -> None:
        out = _canvas(_render_envelope())
        assert "{{" not in out
        assert "<!-- FR:" not in out

    def test_head_doc_comment_stripped(self) -> None:
        out = _canvas(_render_envelope())
        # The template's documentation comment (and its per-finding shape example)
        # must be gone — only the body placeholders are filled.
        assert "VERSION-CONTROLLED TEMPLATE" not in out
        assert "STABLE_RECORD_ID" not in out
        # The page shell itself is intact.
        assert out.lstrip().startswith("<!DOCTYPE html>")
        assert "<head>" in out

    def test_run_id_embedded_in_body(self) -> None:
        out = _canvas(_render_envelope())
        assert 'data-run-id="20260203-100000"' in out

    def test_exclusive_accordion_and_details(self) -> None:
        out = _canvas(_render_envelope())
        assert 'name="findings"' in out
        assert "<details" in out and "<summary" in out

    def test_checkbox_precedes_details(self) -> None:
        out = _canvas(_render_envelope())
        block = out.split('data-record-id="r1"', 1)[1]
        assert block.index('class="row-cb"') < block.index("<details")

    def test_namespaced_action_buttons_present(self) -> None:
        out = _canvas(_render_envelope())
        for action in ("focused-review.fix", "focused-review.disregard", "focused-review.document"):
            assert f'data-action="{action}"' in out

    def test_section_and_badge_counts(self) -> None:
        out = _canvas(_render_envelope())
        assert "Confirmed Findings (2)" in out
        assert "Questionable Findings (1)" in out
        assert "1 finding filtered as invalid" in out
        assert "Rule Quality Notes (1)" in out
        assert '<span class="count">2</span> Confirmed' in out

    def test_found_tags_grouped_and_classed(self) -> None:
        out = _canvas(_render_envelope())
        assert '<span class="found-tag found-tag-concern">bugs(opus,codex)</span>' in out
        assert '<span class="found-tag found-tag-rule">null-safety</span>' in out
        assert '<span class="found-tag found-tag-concern">style(gemini)</span>' in out

    def test_severity_pill_class(self) -> None:
        out = _canvas(_render_envelope())
        assert '<span class="sev sev-high">High</span>' in out
        assert '<span class="sev sev-low">Low</span>' in out

    def test_invalid_row_uses_abbreviated_severity(self) -> None:
        out = _canvas(_render_envelope())
        # The Invalid finding's final severity is Medium → "Med".
        assert "<td>A-09</td>" in out
        assert '<span class="sev sev-medium">Med</span>' in out

    def test_null_line_location_no_colon(self) -> None:
        out = _canvas(_render_envelope())
        assert '<span class="detail-file">src/c.py</span>' in out

    def test_optional_detail_section_omitted_when_empty(self) -> None:
        # The Questionable finding has empty assessment + suggestion.
        out = _canvas(_render_envelope())
        q_block = out.split('data-record-id="r3"', 1)[1].split("</details>", 1)[0]
        assert "detail-assessment" not in q_block
        assert "detail-suggestion" not in q_block
        assert "Location" in q_block  # Location is always present


# ---------------------------------------------------------------------------
# Escaping / injection safety
# ---------------------------------------------------------------------------


class TestEscaping:
    def _xss_env(self) -> dict:
        env = _render_envelope()
        env["run"]["run_id"] = 'rid"><script>evil()</script>'
        f = env["findings"][0]
        f["title"] = 'Bug <img src=x onerror=alert(1)> & "q"'
        f["description"] = "<script>steal()</script>"
        f["suggestion"] = "a & b < c > d"
        return env

    def test_text_fields_html_escaped_in_canvas(self) -> None:
        out = _canvas(self._xss_env())
        assert "<script>steal()</script>" not in out
        assert "&lt;script&gt;steal()&lt;/script&gt;" in out
        assert "onerror=alert(1)>" not in out
        assert "&lt;img src=x onerror=alert(1)&gt;" in out
        assert "a &amp; b &lt; c &gt; d" in out

    def test_run_id_escaped_for_attribute_context(self) -> None:
        out = _canvas(self._xss_env())
        assert "<script>evil()</script>" not in out
        assert 'data-run-id="rid&quot;&gt;&lt;script&gt;evil()&lt;/script&gt;"' in out

    def test_aria_label_attribute_escaped(self) -> None:
        out = _canvas(self._xss_env())
        # The title flows into an aria-label attribute; quotes must be escaped.
        assert 'onerror=alert(1)&gt; &amp; &quot;q&quot;' in out

    def test_marker_text_in_content_is_forge_proof(self) -> None:
        # A finding whose text contains literal placeholder markers must not be
        # able to forge them: html.escape neutralizes the HTML-comment markers,
        # and single-pass substitution never re-expands injected content.
        env = _render_envelope()
        env["run"]["run_id"] = "real-run-123"
        env["findings"][0]["description"] = "evil <!-- FR:QUALITY_NOTES --> and {{RUN_ID}}"
        out = _canvas(env)
        # The real run id is filled correctly (content marker did not interfere).
        assert 'data-run-id="real-run-123"' in out
        # The comment marker in content is escaped (inert), not a live placeholder.
        assert "&lt;!-- FR:QUALITY_NOTES --&gt;" in out
        # The literal {{RUN_ID}} in content was not re-expanded into the run id.
        assert "and {{RUN_ID}}" in out


# ---------------------------------------------------------------------------
# Provenance label mapping (helpers)
# ---------------------------------------------------------------------------


class TestProvenanceMapping:
    def test_parse_source_label(self) -> None:
        assert fr._parse_source_label("rule--null-safety") == ("rule", "null-safety", None)
        assert fr._parse_source_label("concern--bugs--opus") == ("concern", "bugs", "opus")
        assert fr._parse_source_label("concern--bugs") == ("concern", "bugs", None)
        # Multi-segment concern name: only the last segment is the model.
        assert fr._parse_source_label("concern--data-flow--gpt") == ("concern", "data-flow", "gpt")
        # Unrecognised label is preserved verbatim.
        assert fr._parse_source_label("freeform") == ("other", "freeform", None)

    def test_md_groups_ungrouped_with_count(self) -> None:
        prov = [{"source": "concern--bugs--opus"}, "concern--bugs--codex", "rule--x"]
        assert fr._found_by_md(prov) == "3 sources: concern:bugs (opus), concern:bugs (codex), rule:x"

    def test_terminal_groups_models_under_concern(self) -> None:
        prov = [{"source": "concern--bugs--opus"}, "concern--bugs--codex", "rule--x"]
        assert fr._found_by_terminal(prov) == "bugs(opus,codex), rule:x"

    def test_canvas_tags_grouped(self) -> None:
        prov = ["concern--bugs--opus", "concern--bugs--codex"]
        assert fr._found_tags_html(prov) == (
            '<span class="found-tag found-tag-concern">bugs(opus,codex)</span>'
        )

    def test_object_form_uses_source_field(self) -> None:
        assert fr._found_by_terminal([{"source": "rule--y"}]) == "rule:y"


# ---------------------------------------------------------------------------
# CLI handler: render-review
# ---------------------------------------------------------------------------


class TestRenderReviewCLI:
    def _write_records(self, tmp_path: Path, env: dict | None = None) -> Path:
        p = tmp_path / "records.json"
        p.write_text(json.dumps(env if env is not None else _render_envelope()), encoding="utf-8")
        return p

    def test_writes_review_canvas_and_prints_terminal(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        records = self._write_records(tmp_path)
        review_out = tmp_path / "review.md"
        canvas_out = tmp_path / "canvas" / "focused-review.html"
        fr.render_review(
            _render_args(records, review_out=str(review_out), canvas_out=str(canvas_out))
        )

        assert review_out.read_text(encoding="utf-8") == GOLDEN_REVIEW_MD
        canvas = canvas_out.read_text(encoding="utf-8")
        assert 'data-run-id="20260203-100000"' in canvas
        assert "<!-- FR:" not in canvas

        # stdout is exactly the terminal summary (for verbatim relay).
        env = _render_envelope()
        assert capsys.readouterr().out == fr.render_terminal_summary(env, str(review_out))

    def test_canvas_written_to_default_repo_path(self, tmp_path: Path) -> None:
        records = self._write_records(tmp_path)
        fr.render_review(_render_args(records, run_dir=str(tmp_path), repo=str(tmp_path)))
        # Default canvas path is {repo}/.agents/canvas/focused-review.html — always written.
        canvas = tmp_path / ".agents" / "canvas" / "focused-review.html"
        assert canvas.is_file()
        assert "data-run-id" in canvas.read_text(encoding="utf-8")

    def test_default_review_out_in_run_dir(self, tmp_path: Path) -> None:
        records = self._write_records(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        moved = run_dir / "records.json"
        records.replace(moved)
        fr.render_review(_render_args(moved, repo=str(tmp_path)))
        assert (run_dir / "review.md").is_file()

    def test_validation_failure_exits_1_and_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env = _render_envelope()
        env["findings"][0]["severity"] = "Critical?!"  # invalid enum
        records = self._write_records(tmp_path, env)
        review_out = tmp_path / "review.md"
        canvas_out = tmp_path / "canvas.html"

        with pytest.raises(SystemExit) as exc:
            fr.render_review(
                _render_args(records, review_out=str(review_out), canvas_out=str(canvas_out))
            )
        assert exc.value.code == 1

        captured = capsys.readouterr()
        payload = json.loads(captured.err)
        assert payload["valid"] is False
        assert payload["error_count"] >= 1
        assert any(e["field"] == "severity" for e in payload["errors"])
        assert captured.out == ""
        # No artifacts written on a validation failure (orchestrator retries/falls back).
        assert not review_out.exists()
        assert not canvas_out.exists()

    def test_missing_records_file_exits_1(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc:
            fr.render_review(_render_args(tmp_path / "nope.json"))
        assert exc.value.code == 1


class TestRenderReviewSubprocess:
    """Real-subprocess tests: the in-process ``capsys`` tests above capture at the
    Python text layer, so they never exercise an OS-level non-UTF-8 stdout. The
    orchestrator captures (pipes) stdout, which on Windows defaults to cp1252 —
    the terminal summary's glyphs (doc/pipeline/verdict emoji, ``->`` arrow) must
    not crash the encode there.
    """

    def _run(self, tmp_path: Path, env_overrides: dict) -> subprocess.CompletedProcess:
        records = tmp_path / "records.json"
        records.write_text(json.dumps(_render_envelope()), encoding="utf-8")
        child_env = {**os.environ, **env_overrides}
        return subprocess.run(
            [
                sys.executable,
                SCRIPT_PATH,
                "render-review",
                "--records",
                str(records),
                "--run-dir",
                str(tmp_path),
                "--repo",
                str(tmp_path),
            ],
            capture_output=True,
            env=child_env,
        )

    def test_stdout_is_utf8_under_non_utf8_locale(self, tmp_path: Path) -> None:
        # Force a non-UTF-8 stdout encoding (cp1252 == default Windows pipe).
        result = self._run(tmp_path, {"PYTHONIOENCODING": "cp1252"})

        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        # Bytes on the wire are UTF-8 regardless of the ambient locale.
        out = result.stdout.decode("utf-8")
        assert "\U0001f4c4" in out  # 📄 file marker (would not encode in cp1252)
        assert "\u2192" in out  # -> pipeline arrow
        # Artifacts were written (and the run is reported as success, not failure).
        assert (tmp_path / "review.md").is_file()
        assert (tmp_path / ".agents" / "canvas" / "focused-review.html").is_file()
