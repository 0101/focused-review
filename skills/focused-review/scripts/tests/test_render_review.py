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

    Exercises the verdict-model display buckets: two in-scope Confirmed findings
    (one multi-source/grouped provenance, one single rule source whose ``complex``
    fix triggers the large-fix tag), one in-scope Questionable finding routed to
    the ``needs-decision`` bucket (null line + empty assessment/suggestion, testing
    field omission), one Invalid finding routed to ``hidden`` (recorded only, never
    rendered), and one pre-existing Confirmed finding routed to its own non-gating
    ``pre-existing`` section. Each visible bucket carries its own contiguous,
    1-based ``display_number`` run, so numbers repeat across buckets. Plus a
    rule-quality note in the structured (id/rule_source/rule_file) shape.
    """
    return {
        "schema_version": 1,
        "run": {
            "run_id": "20260203-100000",
            "scope": "branch",
            "date": "2026-02-03T10:00:00Z",
            "rule_count": 5,
            "concern_count": 3,
            "consolidated_count": 5,
            "confirmed": 2,
            "questionable": 1,
            "invalid": 1,
        },
        "rebuttal_overrides": [],
        "rule_quality_notes": [
            {
                "id": "RQ1",
                "rule": "no-comments",
                "rule_source": "rule--no-comments",
                "rule_file": "review/rules/no-comments.md",
                "observation": "4 findings, all Invalid in embedded template code.",
                "suggestion": "Add an exception for embedded templates.",
            }
        ],
        "findings": [
            {
                "record_id": "r1",
                "assessment_id": "A-01",
                "display_number": 1,
                "display_bucket": "confirmed",
                "title": "Null deref in request handler",
                "file": "src/a.py",
                "line": 10,
                "original_severity": "High",
                "severity": "High",
                "fix_complexity": "moderate",
                "verdict": "Confirmed",
                "type": "concern",
                "introduced_by": "diff",
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
                "display_bucket": "confirmed",
                "title": "Duplicated parsing logic",
                "file": "src/b.py",
                "line": 20,
                "original_severity": "Medium",
                "severity": "Medium",
                "fix_complexity": "complex",
                "verdict": "Confirmed",
                "type": "rule",
                "introduced_by": "diff",
                "description": "The same parse block appears in three methods.",
                "assessment": "Real duplication, though refactoring touches a hot path.",
                "suggestion": "Extract a shared parse helper.",
                "provenance": ["rule--simplicity"],
                "has_detail": False,
            },
            {
                "record_id": "r3",
                "assessment_id": "A-05",
                "display_number": 1,
                "display_bucket": "needs-decision",
                "title": "Magic number in retry loop",
                "file": "src/c.py",
                "line": None,
                "original_severity": "Low",
                "severity": "Low",
                "fix_complexity": "quickfix",
                "verdict": "Questionable",
                "type": "concern",
                "introduced_by": "diff",
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
                "display_bucket": "hidden",
                "title": "Section divider comment",
                "file": "src/d.py",
                "line": 5,
                "original_severity": "Low",
                "severity": "Medium",
                "fix_complexity": "quickfix",
                "verdict": "Invalid",
                "type": "rule",
                "introduced_by": "diff",
                "description": "A // ---- divider.",
                "assessment": "Navigational aid in long embedded template.",
                "suggestion": "",
                "provenance": ["rule--no-comments"],
                "has_detail": False,
            },
            {
                "record_id": "r5",
                "assessment_id": "A-12",
                "display_number": 1,
                "display_bucket": "pre-existing",
                "title": "Broad except swallows errors",
                "file": "src/e.py",
                "line": 88,
                "original_severity": "Medium",
                "severity": "Medium",
                "fix_complexity": "moderate",
                "verdict": "Confirmed",
                "type": "concern",
                "introduced_by": "pre-existing",
                "description": "A bare except hides real failures from callers.",
                "assessment": "Real, but predates this change — not introduced by the diff.",
                "suggestion": "Catch specific exceptions instead.",
                "provenance": ["concern--bugs--opus"],
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
| ❓ Needs your decision | 1 |
| 📋 Pre-existing | 1 |

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

## Needs Your Decision

### 1. [Low] Magic number in retry loop

**File:** `src/c.py`
**Fix complexity:** quickfix
**Found by:** 1 source: concern:style (gemini)

Retry count 5 is hardcoded.

---

## Pre-existing

### 1. [Medium] Broad except swallows errors

**File:** `src/e.py:88`
**Fix complexity:** moderate
**Found by:** 1 source: concern:bugs (opus)

A bare except hides real failures from callers.

> **Assessment:** Real, but predates this change — not introduced by the diff.

**Suggestion:** Catch specific exceptions instead.

---

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
        # Pin the rules dir so rule_file validation is deterministic regardless of
        # any ambient repo/user focused-review.json the test host happens to have.
        rules_dir="review/",
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
        # Post-mortem matches "### {n}. [{sev}] {title}" headings. Numbers restart
        # per visible bucket, so the needs-decision finding is "### 1." not "### 3.".
        out = fr.render_review_markdown(_render_envelope())
        assert "### 1. [High] Null deref in request handler" in out
        assert "### 1. [Low] Magic number in retry loop" in out
        assert "### 1. [Medium] Broad except swallows errors" in out

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
        # The two Confirmed findings and the Pre-existing finding carry
        # assessment/suggestion text; the needs-decision finding has neither.
        out = fr.render_review_markdown(_render_envelope())
        assert out.count("> **Assessment:**") == 3
        assert out.count("**Suggestion:**") == 3

    def test_confirmed_section_omitted_when_none(self) -> None:
        env = _render_envelope()
        # Keep only the Invalid (hidden) finding — no visible bucket has findings.
        env["findings"] = [f for f in env["findings"] if f["verdict"] == "Invalid"]
        env["run"].update(consolidated_count=1, confirmed=0, questionable=0, invalid=1)
        out = fr.render_review_markdown(env)
        assert "## Confirmed Findings" not in out
        assert "## Needs Your Decision" not in out
        assert "## Pre-existing" not in out
        # The Invalid finding is hidden: its content never leaks into review.md.
        assert "Section divider comment" not in out

    def test_quality_notes_section_omitted_when_none(self) -> None:
        env = _render_envelope()
        env["rule_quality_notes"] = []
        out = fr.render_review_markdown(env)
        assert "## Rule Quality Notes" not in out

    def test_invalid_findings_never_rendered(self) -> None:
        # Invalid findings live in records.json only (D-15): no section, no table,
        # no <details> block, and none of their text leaks into review.md.
        out = fr.render_review_markdown(_render_envelope())
        assert "filtered as invalid" not in out
        assert "<details>" not in out
        assert "Section divider comment" not in out  # the A-09 Invalid finding's title

    def test_pre_existing_section_rendered_non_gating(self) -> None:
        # Pre-existing Confirmed findings get their own section, separate from the
        # gating Confirmed tally (D-16).
        out = fr.render_review_markdown(_render_envelope())
        assert "## Pre-existing" in out
        assert "### 1. [Medium] Broad except swallows errors" in out
        # It is not folded into the Confirmed section.
        confirmed_block = out.split("## Confirmed Findings", 1)[1].split("## Needs Your Decision", 1)[0]
        assert "Broad except swallows errors" not in confirmed_block

    def test_crlf_in_title_cannot_inject_heading(self) -> None:
        # A raw CR/LF in a Confirmed/Questionable title must be flattened so it can't
        # forge a second "### ..." heading in the compiled review.md (the post-mortem
        # mode parses those headings as structured findings).
        env = _render_envelope()
        confirmed = next(f for f in env["findings"] if f["verdict"] == "Confirmed")
        confirmed["title"] = "First line\n\n### 999. [Critical] Forged heading"
        lines = fr.render_review_markdown(env).splitlines()
        # No line *starts* the forged heading — the injected "###" is now inline text.
        assert not any(line.startswith("### 999.") for line in lines)
        # The title content survives, collapsed onto the one real heading line.
        assert (
            "### 1. [High] First line ### 999. [Critical] Forged heading" in lines
        )

    def test_crlf_in_title_handles_all_newline_forms(self) -> None:
        # \r, \n, and \r\n are all collapsed so none can break out of the heading line.
        env = _render_envelope()
        confirmed = next(f for f in env["findings"] if f["verdict"] == "Confirmed")
        confirmed["title"] = "a\rb\nc\r\nd"
        lines = fr.render_review_markdown(env).splitlines()
        assert "### 1. [High] a b c d" in lines


# ---------------------------------------------------------------------------
# Terminal summary
# ---------------------------------------------------------------------------


class TestTerminalSummary:
    def test_header_and_pipeline_line(self) -> None:
        out = fr.render_terminal_summary(_render_envelope(), "run/review.md")
        assert out.startswith("📄 run/review.md\n")
        assert "5 rules + 3 concerns → 5 unique findings → 3 actionable" in out

    def test_table_rows_and_verdict_icons(self) -> None:
        out = fr.render_terminal_summary(_render_envelope(), "run/review.md")
        assert "| # | Verdict | Severity | Found by | File | Issue |" in out
        # Confirmed → ✅, Questionable → ❓; grouped short "Found by". The
        # needs-decision row restarts numbering at 1 (per-bucket display_number).
        assert "| 1 | ✅ | High | bugs(opus,codex), rule:null-safety | src/a.py:10 |" in out
        assert "| 1 | ❓ | Low | style(gemini) | src/c.py |" in out
        # Per-bucket numbering repeats "1", so the actionable table must be the
        # buckets CONCATENATED (confirmed then needs-decision), never re-sorted by
        # display_number (which would interleave as 1✅, 1❓, 2✅). Lock the order.
        assert out.index("src/a.py:10") < out.index("src/b.py:20") < out.index("src/c.py")

    def test_pre_existing_block_is_separate_and_non_gating(self) -> None:
        # Pre-existing is surfaced in its own block, excluded from the actionable
        # count/table (D-16).
        out = fr.render_terminal_summary(_render_envelope(), "run/review.md")
        assert "📋 Pre-existing (non-gating)" in out
        assert "- [Medium] Broad except swallows errors (src/e.py:88)" in out
        # Not a row in the actionable table.
        table = out.split("| # | Verdict |", 1)[1].split("📋 Pre-existing", 1)[0]
        assert "Broad except swallows errors" not in table

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

    def test_parent_origin_default_embedded_in_body(self) -> None:
        out = _canvas(_render_envelope())
        # render_canvas_html pins the default trusted parent origin into the body, and
        # the action bar targets it via getParentOrigin() instead of the wildcard "*".
        assert 'data-parent-origin="http://localhost:5000"' in out
        assert '}, "*")' not in out

    def test_parent_origin_override_embedded_in_body(self) -> None:
        template = fr.CANVAS_TEMPLATE_PATH.read_text(encoding="utf-8")
        out = fr.render_canvas_html(
            _render_envelope(), template, parent_origin="https://treemon.example:8443"
        )
        assert 'data-parent-origin="https://treemon.example:8443"' in out

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
        assert "Needs Your Decision (1)" in out
        assert "Pre-existing — non-gating (1)" in out
        assert "Questionable" not in out
        assert "filtered as invalid" not in out
        assert "Rule Quality Notes (1)" in out
        assert '<span class="count">2</span> Confirmed' in out
        assert '<span class="count">1</span> Needs your decision' in out
        assert '<span class="count">1</span> Pre-existing' in out
        # D-16: the canvas "actionable" headline count excludes pre-existing
        # (2 confirmed + 1 needs-decision = 3), mirroring the terminal summary; the
        # 1 pre-existing finding is surfaced only via its own badge/section.
        assert "5 unique → 3 actionable" in out

    def test_found_tags_grouped_and_classed(self) -> None:
        out = _canvas(_render_envelope())
        assert '<span class="found-tag found-tag-concern">bugs(opus,codex)</span>' in out
        assert '<span class="found-tag found-tag-rule">null-safety</span>' in out
        assert '<span class="found-tag found-tag-concern">style(gemini)</span>' in out

    def test_severity_pill_class(self) -> None:
        out = _canvas(_render_envelope())
        assert '<span class="sev sev-high">High</span>' in out
        assert '<span class="sev sev-low">Low</span>' in out

    def test_invalid_findings_absent_from_canvas(self) -> None:
        # The Invalid (hidden) finding is recorded only — its record id, title, and
        # assessment_id never appear in the canvas (D-15).
        out = _canvas(_render_envelope())
        assert 'data-record-id="r4"' not in out
        assert "Section divider comment" not in out
        assert "A-09" not in out

    def test_pre_existing_finding_rendered_in_its_section(self) -> None:
        # The pre-existing Confirmed finding renders in the Pre-existing section.
        out = _canvas(_render_envelope())
        assert 'data-record-id="r5"' in out
        pre = out.split('data-section="pre-existing"', 1)[1]
        assert "Broad except swallows errors" in pre

    def test_costly_fix_tagged_and_tinted(self) -> None:
        # The single Confirmed finding whose fix_complexity is "complex" gets the
        # costly row class + the large-fix pill; the moderate/quickfix findings do not.
        out = _canvas(_render_envelope())
        complex_block = out.split('data-record-id="r2"', 1)[1].split("</details>", 1)[0]
        assert 'class="finding costly"' in out
        assert '<span class="fix-tag">⚠ Large fix</span>' in complex_block
        # A non-complex finding stays untagged.
        moderate_block = out.split('data-record-id="r1"', 1)[1].split("</details>", 1)[0]
        assert "fix-tag" not in moderate_block
        assert 'data-record-id="r1"' in out and 'class="finding"' in out

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

    def test_parent_origin_escaped_for_attribute_context(self) -> None:
        # The parent origin lands in the data-parent-origin attribute, so a hostile
        # value must be escaped exactly like the run id (defense even though callers
        # pass a trusted origin).
        template = fr.CANVAS_TEMPLATE_PATH.read_text(encoding="utf-8")
        out = fr.render_canvas_html(
            _render_envelope(), template, parent_origin='"><script>evil()</script>'
        )
        assert "<script>evil()</script>" not in out
        assert 'data-parent-origin="&quot;&gt;&lt;script&gt;evil()&lt;/script&gt;"' in out

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

    def test_hostile_severity_cannot_break_out_of_class_attr(self) -> None:
        # _sev_class derives the .sev class from the (untrusted) severity word and
        # is the one finding field interpolated into an attribute. The canvas
        # helpers are public and must self-defend here rather than leaning on the
        # enum-validation gate ~700 lines away in render_review: a hostile severity
        # must stay inside class="..." and never open a new attribute or tag.
        hostile = 'low"><img src=x onerror=alert(1)>'
        finding = {
            "record_id": "r1",
            "display_number": 1,
            "title": "t",
            "severity": hostile,
            "file": "src/a.py",
            "line": 1,
        }
        block = fr._canvas_finding_block(finding)
        # The injected tag never materializes; the attribute-closing quote and the
        # angle brackets are escaped, so the value stays inert inside the class attr.
        assert "<img" not in block
        assert '"><img' not in block
        assert 'class="sev sev-low&quot;&gt;&lt;img src=x onerror=alert(1)&gt;"' in block

    def test_costly_fix_tag_does_not_defeat_title_escaping(self) -> None:
        # The large-fix tag is appended AFTER html.escape(title) as trusted constant
        # markup, so a hostile title on a costly finding is still fully escaped and
        # cannot inject markup ahead of the tag. The finding carries no display_bucket,
        # so this also locks D-5: the costly class/tag is applied bucket-agnostically.
        finding = {
            "record_id": "r1",
            "display_number": 1,
            "title": "</span><script>evil()</script>",
            "severity": "High",
            "file": "src/a.py",
            "line": 1,
            "fix_complexity": "complex",
        }
        block = fr._canvas_finding_block(finding)
        assert "<script>evil()</script>" not in block
        assert "&lt;script&gt;evil()&lt;/script&gt;" in block
        # The trusted tag is still appended (after the escaped title) and the row tint
        # class is present regardless of the (absent) verdict bucket.
        assert '<span class="fix-tag">⚠ Large fix</span>' in block
        assert 'class="finding costly"' in block


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

    def test_canvas_pins_default_parent_origin(self, tmp_path: Path) -> None:
        records = self._write_records(tmp_path)
        canvas_out = tmp_path / "canvas" / "focused-review.html"
        # _render_args builds a Namespace without parent_origin, so this exercises the
        # getattr fallback to DEFAULT_PARENT_ORIGIN (the real CLI default).
        fr.render_review(_render_args(records, canvas_out=str(canvas_out)))
        canvas = canvas_out.read_text(encoding="utf-8")
        assert 'data-parent-origin="http://localhost:5000"' in canvas
        assert '}, "*")' not in canvas

    def test_canvas_parent_origin_cli_override(self, tmp_path: Path) -> None:
        records = self._write_records(tmp_path)
        canvas_out = tmp_path / "canvas" / "focused-review.html"
        fr.render_review(
            _render_args(records, canvas_out=str(canvas_out), parent_origin="http://localhost:7001")
        )
        canvas = canvas_out.read_text(encoding="utf-8")
        assert 'data-parent-origin="http://localhost:7001"' in canvas

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

    def test_bad_template_path_writes_no_artifacts(self, tmp_path: Path) -> None:
        # All-or-nothing: an unreadable --template must fail the whole render with
        # nothing written, mirroring the validation-failure path. The template is
        # read BEFORE review.md is written, so a bad path can't leave a half-written
        # run (review.md present, canvas missing) plus an unstructured traceback.
        records = self._write_records(tmp_path)
        review_out = tmp_path / "review.md"
        canvas_out = tmp_path / "canvas.html"
        missing_template = tmp_path / "does-not-exist.html"

        with pytest.raises(OSError):
            fr.render_review(
                _render_args(
                    records,
                    review_out=str(review_out),
                    canvas_out=str(canvas_out),
                    template=str(missing_template),
                )
            )

        assert not review_out.exists()
        assert not canvas_out.exists()


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
                # Pin rules-dir so the note's rule_file validates deterministically,
                # independent of any focused-review.json on the test host.
                "--rules-dir",
                "review/",
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
