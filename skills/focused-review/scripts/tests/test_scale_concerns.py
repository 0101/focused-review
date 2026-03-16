"""Tests for the scale-concerns subcommand (tier-based concern filtering)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importlib

fr = importlib.import_module("focused-review")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(concern: str, model: str = "opus") -> dict[str, str]:
    """Build a minimal concern dispatch entry."""
    return {
        "concern": concern,
        "model": model,
        "priority": "standard",
        "prompt_path": f".agents/focused-review/prompts/{concern}--{model}.md",
    }


def _full_dispatch() -> list[dict[str, str]]:
    """Standard dispatch with all 4 concerns, some multi-model."""
    return [
        _entry("bugs", "opus"),
        _entry("security", "opus"),
        _entry("architecture", "opus"),
        _entry("general", "opus"),
        _entry("bugs", "gemini"),       # multi-model duplicate
        _entry("security", "gemini"),   # multi-model duplicate
    ]


# ---------------------------------------------------------------------------
# _diff_lines_to_tier
# ---------------------------------------------------------------------------


class TestDiffLinesToTier:
    def test_zero_lines(self) -> None:
        assert fr._diff_lines_to_tier(0) == "1-10"

    def test_boundary_10(self) -> None:
        assert fr._diff_lines_to_tier(10) == "1-10"

    def test_boundary_11(self) -> None:
        assert fr._diff_lines_to_tier(11) == "11-100"

    def test_boundary_100(self) -> None:
        assert fr._diff_lines_to_tier(100) == "11-100"

    def test_boundary_101(self) -> None:
        assert fr._diff_lines_to_tier(101) == "101-500"

    def test_boundary_500(self) -> None:
        assert fr._diff_lines_to_tier(500) == "101-500"

    def test_boundary_501(self) -> None:
        assert fr._diff_lines_to_tier(501) == "501+"

    def test_large_value(self) -> None:
        assert fr._diff_lines_to_tier(10000) == "501+"


# ---------------------------------------------------------------------------
# _dedup_concerns
# ---------------------------------------------------------------------------


class TestDedupConcerns:
    def test_no_duplicates(self) -> None:
        entries = [_entry("bugs"), _entry("security")]
        result = fr._dedup_concerns(entries)
        assert len(result) == 2
        assert [e["concern"] for e in result] == ["bugs", "security"]

    def test_keeps_first_model(self) -> None:
        entries = [_entry("bugs", "opus"), _entry("bugs", "gemini")]
        result = fr._dedup_concerns(entries)
        assert len(result) == 1
        assert result[0]["model"] == "opus"

    def test_preserves_order(self) -> None:
        entries = [
            _entry("security", "opus"),
            _entry("bugs", "opus"),
            _entry("general", "opus"),
            _entry("bugs", "gemini"),
        ]
        result = fr._dedup_concerns(entries)
        assert [e["concern"] for e in result] == ["security", "bugs", "general"]

    def test_empty_list(self) -> None:
        assert fr._dedup_concerns([]) == []


# ---------------------------------------------------------------------------
# _filter_concerns_by_tier — 1-10 lines
# ---------------------------------------------------------------------------


class TestTier1To10:
    """1-10 lines: keep only 'general' concern (first match)."""

    def test_keeps_only_general(self) -> None:
        result = fr._filter_concerns_by_tier(5, _full_dispatch())
        assert len(result) == 1
        assert result[0]["concern"] == "general"

    def test_boundary_at_10(self) -> None:
        result = fr._filter_concerns_by_tier(10, _full_dispatch())
        assert len(result) == 1
        assert result[0]["concern"] == "general"

    def test_no_general_returns_empty(self) -> None:
        entries = [_entry("bugs"), _entry("security")]
        result = fr._filter_concerns_by_tier(3, entries)
        assert result == []

    def test_multiple_general_keeps_first(self) -> None:
        entries = [_entry("general", "opus"), _entry("general", "gemini")]
        result = fr._filter_concerns_by_tier(1, entries)
        assert len(result) == 1
        assert result[0]["model"] == "opus"

    def test_zero_lines(self) -> None:
        result = fr._filter_concerns_by_tier(0, _full_dispatch())
        assert len(result) == 1
        assert result[0]["concern"] == "general"


# ---------------------------------------------------------------------------
# _filter_concerns_by_tier — 11-100 lines
# ---------------------------------------------------------------------------


class TestTier11To100:
    """11-100 lines: keep only 'bugs' + 'security', deduplicated."""

    def test_keeps_bugs_and_security(self) -> None:
        result = fr._filter_concerns_by_tier(50, _full_dispatch())
        concerns = [e["concern"] for e in result]
        assert set(concerns) == {"bugs", "security"}

    def test_boundary_at_11(self) -> None:
        result = fr._filter_concerns_by_tier(11, _full_dispatch())
        concerns = [e["concern"] for e in result]
        assert set(concerns) == {"bugs", "security"}

    def test_boundary_at_100(self) -> None:
        result = fr._filter_concerns_by_tier(100, _full_dispatch())
        concerns = [e["concern"] for e in result]
        assert set(concerns) == {"bugs", "security"}

    def test_deduplicates_multi_model(self) -> None:
        result = fr._filter_concerns_by_tier(50, _full_dispatch())
        assert len(result) == 2  # 2 unique concerns, not 4 entries

    def test_keeps_first_model_entry(self) -> None:
        result = fr._filter_concerns_by_tier(50, _full_dispatch())
        bugs = [e for e in result if e["concern"] == "bugs"][0]
        assert bugs["model"] == "opus"  # first entry, not gemini

    def test_excludes_architecture_and_general(self) -> None:
        result = fr._filter_concerns_by_tier(50, _full_dispatch())
        concerns = {e["concern"] for e in result}
        assert "architecture" not in concerns
        assert "general" not in concerns

    def test_no_bugs_or_security_returns_empty(self) -> None:
        entries = [_entry("architecture"), _entry("general")]
        result = fr._filter_concerns_by_tier(50, entries)
        assert result == []


# ---------------------------------------------------------------------------
# _filter_concerns_by_tier — 101-500 lines
# ---------------------------------------------------------------------------


class TestTier101To500:
    """101-500 lines: all concerns, deduplicated by concern name."""

    def test_keeps_all_concern_types(self) -> None:
        result = fr._filter_concerns_by_tier(200, _full_dispatch())
        concerns = {e["concern"] for e in result}
        assert concerns == {"bugs", "security", "architecture", "general"}

    def test_boundary_at_101(self) -> None:
        result = fr._filter_concerns_by_tier(101, _full_dispatch())
        concerns = {e["concern"] for e in result}
        assert concerns == {"bugs", "security", "architecture", "general"}

    def test_boundary_at_500(self) -> None:
        result = fr._filter_concerns_by_tier(500, _full_dispatch())
        concerns = {e["concern"] for e in result}
        assert concerns == {"bugs", "security", "architecture", "general"}

    def test_deduplicates_multi_model(self) -> None:
        result = fr._filter_concerns_by_tier(300, _full_dispatch())
        assert len(result) == 4  # 4 unique concerns, not 6 entries

    def test_keeps_first_model_entry(self) -> None:
        result = fr._filter_concerns_by_tier(300, _full_dispatch())
        bugs = [e for e in result if e["concern"] == "bugs"][0]
        assert bugs["model"] == "opus"


# ---------------------------------------------------------------------------
# _filter_concerns_by_tier — 501+ lines
# ---------------------------------------------------------------------------


class TestTier501Plus:
    """501+ lines: all entries, no filtering or deduplication."""

    def test_returns_all_entries(self) -> None:
        dispatch = _full_dispatch()
        result = fr._filter_concerns_by_tier(600, dispatch)
        assert len(result) == 6  # all entries including duplicates

    def test_boundary_at_501(self) -> None:
        dispatch = _full_dispatch()
        result = fr._filter_concerns_by_tier(501, dispatch)
        assert len(result) == 6

    def test_preserves_multi_model_entries(self) -> None:
        dispatch = _full_dispatch()
        result = fr._filter_concerns_by_tier(1000, dispatch)
        bugs_entries = [e for e in result if e["concern"] == "bugs"]
        assert len(bugs_entries) == 2
        assert {e["model"] for e in bugs_entries} == {"opus", "gemini"}

    def test_identity_for_large_diff(self) -> None:
        dispatch = _full_dispatch()
        result = fr._filter_concerns_by_tier(5000, dispatch)
        assert result == dispatch


# ---------------------------------------------------------------------------
# _count_diff_lines
# ---------------------------------------------------------------------------


class TestCountDiffLines:
    def test_counts_added_and_removed(self, tmp_path: Path) -> None:
        patch = tmp_path / "diff.patch"
        patch.write_text(
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,3 @@\n"
            " context line\n"
            "-removed line\n"
            "+added line\n",
            encoding="utf-8",
        )
        assert fr._count_diff_lines(patch) == 2  # -removed, +added

    def test_excludes_header_markers(self, tmp_path: Path) -> None:
        patch = tmp_path / "diff.patch"
        patch.write_text(
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "+real change\n",
            encoding="utf-8",
        )
        assert fr._count_diff_lines(patch) == 1  # only +real, not --- or +++

    def test_missing_file_returns_zero(self, tmp_path: Path) -> None:
        assert fr._count_diff_lines(tmp_path / "nonexistent.patch") == 0

    def test_empty_file_returns_zero(self, tmp_path: Path) -> None:
        patch = tmp_path / "diff.patch"
        patch.write_text("", encoding="utf-8")
        assert fr._count_diff_lines(patch) == 0


# ---------------------------------------------------------------------------
# scale_concerns (integration — full subcommand via argparse)
# ---------------------------------------------------------------------------


class TestScaleConcernsSubcommand:
    """End-to-end tests for the scale-concerns subcommand."""

    def _run(
        self,
        tmp_path: Path,
        entries: list[dict[str, str]],
        *,
        diff_lines: int | None = None,
        diff_path: Path | None = None,
    ) -> dict:
        """Write dispatch, invoke scale_concerns, return parsed JSON output."""
        dispatch_path = tmp_path / "concern-dispatch.json"
        dispatch_path.write_text(json.dumps(entries), encoding="utf-8")

        import argparse
        import io
        from contextlib import redirect_stdout

        args = argparse.Namespace(
            diff_lines=diff_lines,
            diff_path=str(diff_path) if diff_path else None,
            dispatch_path=str(dispatch_path),
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            fr.scale_concerns(args)

        result = json.loads(buf.getvalue())
        # Also read back the written file to verify it was updated
        result["_written"] = json.loads(dispatch_path.read_text(encoding="utf-8"))
        return result

    def test_tier_1_10_via_diff_lines(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _full_dispatch(), diff_lines=5)
        assert result["tier"] == "1-10"
        assert result["concerns_after"] == 1
        assert result["_written"][0]["concern"] == "general"

    def test_tier_11_100_via_diff_lines(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _full_dispatch(), diff_lines=50)
        assert result["tier"] == "11-100"
        assert result["concerns_after"] == 2

    def test_tier_101_500_via_diff_lines(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _full_dispatch(), diff_lines=200)
        assert result["tier"] == "101-500"
        assert result["concerns_after"] == 4

    def test_tier_501_via_diff_lines(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _full_dispatch(), diff_lines=600)
        assert result["tier"] == "501+"
        assert result["concerns_after"] == 6

    def test_uses_diff_path_when_no_diff_lines(self, tmp_path: Path) -> None:
        # Create a patch with 15 changed lines (11-100 tier)
        patch = tmp_path / "diff.patch"
        lines = ["--- a/f.py\n", "+++ b/f.py\n", "@@ -1 +1 @@\n"]
        lines += [f"+line{i}\n" for i in range(15)]
        patch.write_text("".join(lines), encoding="utf-8")

        result = self._run(tmp_path, _full_dispatch(), diff_path=patch)
        assert result["diff_lines"] == 15
        assert result["tier"] == "11-100"

    def test_diff_lines_overrides_diff_path(self, tmp_path: Path) -> None:
        # diff_path would give 15 lines, but diff_lines=5 forces tier 1-10
        patch = tmp_path / "diff.patch"
        lines = ["--- a/f.py\n", "+++ b/f.py\n"]
        lines += [f"+line{i}\n" for i in range(15)]
        patch.write_text("".join(lines), encoding="utf-8")

        result = self._run(tmp_path, _full_dispatch(), diff_lines=5, diff_path=patch)
        assert result["diff_lines"] == 5
        assert result["tier"] == "1-10"

    def test_output_includes_before_and_after_counts(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _full_dispatch(), diff_lines=50)
        assert result["concerns_before"] == 6
        assert result["concerns_after"] == 2

    def test_empty_dispatch(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, [], diff_lines=50)
        assert result["concerns_before"] == 0
        assert result["concerns_after"] == 0

    def test_missing_dispatch_exits(self, tmp_path: Path) -> None:
        import argparse

        args = argparse.Namespace(
            diff_lines=50,
            diff_path=None,
            dispatch_path=str(tmp_path / "nonexistent.json"),
        )
        with pytest.raises(SystemExit):
            fr.scale_concerns(args)

    def test_no_diff_lines_or_path_exits(self, tmp_path: Path) -> None:
        dispatch_path = tmp_path / "concern-dispatch.json"
        dispatch_path.write_text(json.dumps(_full_dispatch()), encoding="utf-8")

        import argparse

        args = argparse.Namespace(
            diff_lines=None,
            diff_path=None,
            dispatch_path=str(dispatch_path),
        )
        with pytest.raises(SystemExit):
            fr.scale_concerns(args)
