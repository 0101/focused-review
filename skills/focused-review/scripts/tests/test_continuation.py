"""Tests for continuation loop helper functions."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests import create_file

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importlib

fr = importlib.import_module("focused-review")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_dispatch(tmp_path: Path, entries: list[dict[str, str]]) -> Path:
    """Write a dispatch JSON file and return its path."""
    dispatch_path = tmp_path / "dispatch.json"
    dispatch_path.write_text(json.dumps(entries), encoding="utf-8")
    return dispatch_path


def _make_entry(
    concern: str = "bugs",
    model: str = "opus",
    *,
    work_dir: Path | None = None,
) -> dict[str, str]:
    """Build a dispatch entry dict."""
    base = ".agents/focused-review"
    return {
        "concern": concern,
        "model": model,
        "priority": "high",
        "prompt_path": f"{base}/prompts/{concern}--{model}.md",
        "finding_path": f"{base}/findings/concern--{concern}--{model}.md",
    }


def _setup_work_dir(tmp_path: Path) -> Path:
    """Create the standard .agents/focused-review directory and return it."""
    work_dir = tmp_path / ".agents" / "focused-review"
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


# ---------------------------------------------------------------------------
# TestClassifyConcernResults
# ---------------------------------------------------------------------------


class TestClassifyConcernResults:
    def test_complete_finding(self, tmp_path: Path) -> None:
        """Finding file exists with complete sentinel → classified as complete."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("bugs", "opus")
        create_file(
            tmp_path,
            entry["finding_path"],
            "Review Status: This review is complete.\n\n## Findings\nNone.",
        )
        dispatch = _write_dispatch(tmp_path, [entry])

        result = fr.classify_concern_results(dispatch, work_dir)

        assert len(result["complete"]) == 1
        assert result["complete"][0]["concern"] == "bugs"
        assert result["incomplete"] == []
        assert result["failed"] == []

    def test_incomplete_finding(self, tmp_path: Path) -> None:
        """Finding file exists with incomplete sentinel → classified as incomplete."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("security", "gemini")
        create_file(
            tmp_path,
            entry["finding_path"],
            "Review Status: This review is incomplete — 3 of 7 areas examined.\n\nPartial.",
        )
        dispatch = _write_dispatch(tmp_path, [entry])

        result = fr.classify_concern_results(dispatch, work_dir)

        assert len(result["incomplete"]) == 1
        assert result["incomplete"][0]["concern"] == "security"
        assert result["complete"] == []
        assert result["failed"] == []

    def test_missing_finding(self, tmp_path: Path) -> None:
        """Finding file doesn't exist → classified as failed."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("arch", "haiku")
        dispatch = _write_dispatch(tmp_path, [entry])

        result = fr.classify_concern_results(dispatch, work_dir)

        assert len(result["failed"]) == 1
        assert result["failed"][0]["concern"] == "arch"
        assert result["failed"][0]["model"] == "haiku"
        assert result["complete"] == []
        assert result["incomplete"] == []

    def test_missing_finding_with_trace(self, tmp_path: Path) -> None:
        """Failed entry includes trace_path when trace file exists."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("arch", "haiku")
        trace_path = create_file(
            tmp_path,
            ".agents/focused-review/traces/concern--arch--haiku.jsonl",
            '{"type":"trace"}',
        )
        dispatch = _write_dispatch(tmp_path, [entry])

        result = fr.classify_concern_results(dispatch, work_dir)

        assert len(result["failed"]) == 1
        assert result["failed"][0]["trace_path"] == str(trace_path)

    def test_legacy_no_sentinel(self, tmp_path: Path) -> None:
        """Finding file exists without sentinel → classified as complete (legacy)."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("bugs", "opus")
        create_file(
            tmp_path,
            entry["finding_path"],
            "## Bug Findings\n\nFound a null pointer issue in module X.",
        )
        dispatch = _write_dispatch(tmp_path, [entry])

        result = fr.classify_concern_results(dispatch, work_dir)

        assert len(result["complete"]) == 1
        assert result["incomplete"] == []
        assert result["failed"] == []

    def test_empty_finding_file(self, tmp_path: Path) -> None:
        """Empty finding file → classified as complete (legacy)."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("bugs", "opus")
        create_file(tmp_path, entry["finding_path"], "")
        dispatch = _write_dispatch(tmp_path, [entry])

        result = fr.classify_concern_results(dispatch, work_dir)

        assert len(result["complete"]) == 1

    def test_mixed_results(self, tmp_path: Path) -> None:
        """Multiple entries with different statuses → correctly classified."""
        work_dir = _setup_work_dir(tmp_path)
        complete_entry = _make_entry("bugs", "opus")
        incomplete_entry = _make_entry("security", "gemini")
        failed_entry = _make_entry("arch", "haiku")

        create_file(
            tmp_path,
            complete_entry["finding_path"],
            "Review Status: This review is complete.\n\nDone.",
        )
        create_file(
            tmp_path,
            incomplete_entry["finding_path"],
            "Review Status: This review is incomplete — 2 of 5 areas.\n\nPartial.",
        )
        # failed_entry: no finding file created

        dispatch = _write_dispatch(
            tmp_path, [complete_entry, incomplete_entry, failed_entry]
        )

        result = fr.classify_concern_results(dispatch, work_dir)

        assert len(result["complete"]) == 1
        assert len(result["incomplete"]) == 1
        assert len(result["failed"]) == 1
        assert result["complete"][0]["concern"] == "bugs"
        assert result["incomplete"][0]["concern"] == "security"
        assert result["failed"][0]["concern"] == "arch"

    def test_hypothesis_in_incomplete(self, tmp_path: Path) -> None:
        """Finding with [Hypothesis] heading + incomplete sentinel → classified as incomplete."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("bugs", "opus")
        create_file(
            tmp_path,
            entry["finding_path"],
            "Review Status: This review is incomplete — 1 of 4 areas.\n\n"
            "## [Hypothesis] Possible null dereference\n\nNeeds verification.",
        )
        dispatch = _write_dispatch(tmp_path, [entry])

        result = fr.classify_concern_results(dispatch, work_dir)

        assert len(result["incomplete"]) == 1
        assert result["incomplete"][0]["concern"] == "bugs"
        assert result["complete"] == []


# ---------------------------------------------------------------------------
# TestBuildContinuationDispatch
# ---------------------------------------------------------------------------


class TestBuildContinuationDispatch:
    def test_filters_to_incomplete_only(self, tmp_path: Path) -> None:
        """Only incomplete pairs appear in continuation dispatch."""
        entries = [
            _make_entry("bugs", "opus"),
            _make_entry("security", "gemini"),
            _make_entry("arch", "haiku"),
        ]
        dispatch = _write_dispatch(tmp_path, entries)
        output = tmp_path / "continue.json"

        fr.build_continuation_dispatch(
            dispatch, [("security", "gemini")], output
        )

        written = json.loads(output.read_text(encoding="utf-8"))
        assert len(written) == 1
        assert written[0]["concern"] == "security"
        assert written[0]["model"] == "gemini"

    def test_preserves_entry_structure(self, tmp_path: Path) -> None:
        """Continuation entries have same keys as original dispatch entries."""
        entry = _make_entry("bugs", "opus")
        dispatch = _write_dispatch(tmp_path, [entry])
        output = tmp_path / "continue.json"

        fr.build_continuation_dispatch(dispatch, [("bugs", "opus")], output)

        written = json.loads(output.read_text(encoding="utf-8"))
        assert written[0].keys() == entry.keys()

    def test_empty_incomplete_list(self, tmp_path: Path) -> None:
        """No incomplete pairs → empty dispatch."""
        entries = [_make_entry("bugs", "opus")]
        dispatch = _write_dispatch(tmp_path, entries)
        output = tmp_path / "continue.json"

        fr.build_continuation_dispatch(dispatch, [], output)

        written = json.loads(output.read_text(encoding="utf-8"))
        assert written == []

    def test_overwrites_existing_output(self, tmp_path: Path) -> None:
        """Output file is overwritten each time."""
        entries = [_make_entry("bugs", "opus"), _make_entry("security", "gemini")]
        dispatch = _write_dispatch(tmp_path, entries)
        output = tmp_path / "continue.json"

        # First write with both
        fr.build_continuation_dispatch(
            dispatch, [("bugs", "opus"), ("security", "gemini")], output
        )
        first = json.loads(output.read_text(encoding="utf-8"))
        assert len(first) == 2

        # Overwrite with just one
        fr.build_continuation_dispatch(
            dispatch, [("bugs", "opus")], output
        )
        second = json.loads(output.read_text(encoding="utf-8"))
        assert len(second) == 1


# ---------------------------------------------------------------------------
# TestMeasureProgress
# ---------------------------------------------------------------------------


class TestMeasureProgress:
    def test_counts_checkmarks(self, tmp_path: Path) -> None:
        """Counts [x] and [~] marks in plan file."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("bugs", "opus")
        create_file(
            tmp_path,
            entry["finding_path"],
            "Some findings content here.",
        )
        create_file(
            tmp_path,
            ".agents/focused-review/scratchpad/bugs--opus--plan.md",
            "- [x] Check nulls\n- [~] Partial check\n- [ ] Not done\n- [x] Another done",
        )

        result = fr.measure_progress([entry], work_dir)

        assert result[("bugs", "opus")]["plan_checkmarks"] == 3

    def test_missing_plan_file(self, tmp_path: Path) -> None:
        """Missing plan file → 0 checkmarks."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("bugs", "opus")
        create_file(tmp_path, entry["finding_path"], "content")

        result = fr.measure_progress([entry], work_dir)

        assert result[("bugs", "opus")]["plan_checkmarks"] == 0

    def test_finding_size(self, tmp_path: Path) -> None:
        """Reports correct byte size of finding file."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("bugs", "opus")
        content = "Hello, world! This is a finding."
        create_file(tmp_path, entry["finding_path"], content)

        result = fr.measure_progress([entry], work_dir)

        expected_size = len(content.encode("utf-8"))
        assert result[("bugs", "opus")]["finding_size"] == expected_size

    def test_missing_finding_file(self, tmp_path: Path) -> None:
        """Missing finding file → 0 bytes."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("bugs", "opus")

        result = fr.measure_progress([entry], work_dir)

        assert result[("bugs", "opus")]["finding_size"] == 0

    def test_unchecked_boxes_not_counted(self, tmp_path: Path) -> None:
        """[ ] marks are NOT counted as progress."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("bugs", "opus")
        create_file(tmp_path, entry["finding_path"], "content")
        create_file(
            tmp_path,
            ".agents/focused-review/scratchpad/bugs--opus--plan.md",
            "- [ ] Not done\n- [ ] Also not done\n- [ ] Still not done",
        )

        result = fr.measure_progress([entry], work_dir)

        assert result[("bugs", "opus")]["plan_checkmarks"] == 0


# ---------------------------------------------------------------------------
# TestDetectStuck
# ---------------------------------------------------------------------------


class TestDetectStuck:
    def test_no_progress_is_stuck(self) -> None:
        """Same metrics before and after → stuck."""
        metrics = {("bugs", "opus"): {"finding_size": 100, "plan_checkmarks": 2}}

        stuck = fr.detect_stuck(metrics, metrics)

        assert ("bugs", "opus") in stuck

    def test_finding_grew_not_stuck(self) -> None:
        """Finding size increased → not stuck."""
        prev = {("bugs", "opus"): {"finding_size": 100, "plan_checkmarks": 2}}
        curr = {("bugs", "opus"): {"finding_size": 200, "plan_checkmarks": 2}}

        stuck = fr.detect_stuck(curr, prev)

        assert stuck == []

    def test_checkmarks_grew_not_stuck(self) -> None:
        """Plan checkmarks increased → not stuck."""
        prev = {("bugs", "opus"): {"finding_size": 100, "plan_checkmarks": 2}}
        curr = {("bugs", "opus"): {"finding_size": 100, "plan_checkmarks": 3}}

        stuck = fr.detect_stuck(curr, prev)

        assert stuck == []

    def test_both_grew_not_stuck(self) -> None:
        """Both metrics increased → not stuck."""
        prev = {("bugs", "opus"): {"finding_size": 100, "plan_checkmarks": 2}}
        curr = {("bugs", "opus"): {"finding_size": 200, "plan_checkmarks": 4}}

        stuck = fr.detect_stuck(curr, prev)

        assert stuck == []

    def test_only_finding_shrunk_is_stuck(self) -> None:
        """Finding size decreased (shouldn't happen but handle) → stuck."""
        prev = {("bugs", "opus"): {"finding_size": 200, "plan_checkmarks": 2}}
        curr = {("bugs", "opus"): {"finding_size": 150, "plan_checkmarks": 2}}

        stuck = fr.detect_stuck(curr, prev)

        assert ("bugs", "opus") in stuck

    def test_hypothesis_finding_grows_file(self, tmp_path: Path) -> None:
        """A [Hypothesis] finding increases file size → not stuck."""
        prev = {("bugs", "opus"): {"finding_size": 50, "plan_checkmarks": 0}}
        curr = {("bugs", "opus"): {"finding_size": 200, "plan_checkmarks": 0}}

        stuck = fr.detect_stuck(curr, prev)

        assert stuck == []


# ---------------------------------------------------------------------------
# Subcommand integration tests
# ---------------------------------------------------------------------------


class TestClassifyConcernsSubcommand:
    def test_json_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Subcommand produces valid JSON with complete/incomplete/failed keys."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("bugs", "opus")
        create_file(
            tmp_path,
            entry["finding_path"],
            "Review Status: This review is complete.\n\nDone.",
        )
        dispatch = _write_dispatch(tmp_path, [entry])

        import argparse

        args = argparse.Namespace(
            dispatch=str(dispatch),
            work_dir=str(work_dir),
        )
        fr._classify_concerns_cmd(args)

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "complete" in output
        assert "incomplete" in output
        assert "failed" in output
        assert len(output["complete"]) == 1


class TestBuildContinuationSubcommand:
    def test_filters_dispatch(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Subcommand writes filtered dispatch file."""
        entries = [_make_entry("bugs", "opus"), _make_entry("security", "gemini")]
        dispatch = _write_dispatch(tmp_path, entries)
        output = tmp_path / "continue.json"

        import argparse

        args = argparse.Namespace(
            dispatch=str(dispatch),
            incomplete="bugs:opus",
            output=str(output),
        )
        fr._build_continuation_cmd(args)

        written = json.loads(output.read_text(encoding="utf-8"))
        assert len(written) == 1
        assert written[0]["concern"] == "bugs"


class TestMeasureProgressSubcommand:
    def test_json_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Subcommand produces valid JSON with progress metrics."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("bugs", "opus")
        create_file(tmp_path, entry["finding_path"], "Some content here.")
        dispatch = _write_dispatch(tmp_path, [entry])

        import argparse

        args = argparse.Namespace(
            dispatch=str(dispatch),
            work_dir=str(work_dir),
        )
        fr._measure_progress_cmd(args)

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "bugs:opus" in output
        assert "finding_size" in output["bugs:opus"]
        assert "plan_checkmarks" in output["bugs:opus"]


# ---------------------------------------------------------------------------
# Integration: Full Continuation Loop
# ---------------------------------------------------------------------------


def _simulate_orchestrator_loop(
    dispatch_path: Path,
    work_dir: Path,
    max_rounds: int = 3,
    *,
    round_callback: Callable[[int, list[dict]], None] | None = None,
) -> dict:
    """Simulate the SKILL.md continuation loop using Python helpers.

    round_callback is called before each continuation round with
    (round_number, incomplete_entries) — test code uses this to
    simulate agent behavior (modify files on disk).

    Returns: {
        'rounds': int,
        'final_complete': list,
        'final_failed': list,
        'stuck': list,
    }
    """
    current_dispatch = dispatch_path
    all_stuck: list[tuple[str, str]] = []
    all_complete: list[dict] = []
    all_failed: list[dict] = []
    round_num = 0

    while True:
        result = fr.classify_concern_results(current_dispatch, work_dir)
        all_complete.extend(result["complete"])
        all_failed.extend(result["failed"])

        incomplete = [
            e for e in result["incomplete"]
            if (e["concern"], e["model"]) not in all_stuck
        ]

        if not incomplete or round_num >= max_rounds:
            return {
                "rounds": round_num,
                "final_complete": all_complete,
                "final_failed": all_failed,
                "stuck": all_stuck,
            }

        incomplete_pairs = [(e["concern"], e["model"]) for e in incomplete]
        cont_path = work_dir.parent / "concern-dispatch-continue.json"
        fr.build_continuation_dispatch(dispatch_path, incomplete_pairs, cont_path)

        previous = fr.measure_progress(incomplete, work_dir)

        if round_callback:
            round_callback(round_num, incomplete)

        current = fr.measure_progress(incomplete, work_dir)
        stuck_pairs = fr.detect_stuck(current, previous)
        all_stuck.extend(stuck_pairs)

        round_num += 1
        current_dispatch = cont_path


class TestContinuationLoop:
    def test_all_complete_first_round(self, tmp_path: Path) -> None:
        """All agents complete on first run → 0 continuation rounds."""
        work_dir = _setup_work_dir(tmp_path)
        entries = [_make_entry("bugs", "opus"), _make_entry("security", "gemini")]
        dispatch = _write_dispatch(tmp_path, entries)

        create_file(
            tmp_path,
            entries[0]["finding_path"],
            "Review Status: This review is complete.\n\nBug findings.",
        )
        create_file(
            tmp_path,
            entries[1]["finding_path"],
            "Review Status: This review is complete.\n\nSecurity findings.",
        )

        result = _simulate_orchestrator_loop(dispatch, work_dir)

        assert result["rounds"] == 0
        assert len(result["final_complete"]) == 2
        assert result["final_failed"] == []
        assert result["stuck"] == []

    def test_one_incomplete_then_completes(self, tmp_path: Path) -> None:
        """One agent incomplete → 1 continuation round → completes."""
        work_dir = _setup_work_dir(tmp_path)
        entries = [
            _make_entry("bugs", "opus"),
            _make_entry("security", "gemini"),
        ]
        dispatch = _write_dispatch(tmp_path, entries)

        create_file(
            tmp_path,
            entries[0]["finding_path"],
            "Review Status: This review is complete.\n\nBug findings here.",
        )
        create_file(
            tmp_path,
            entries[1]["finding_path"],
            "Review Status: This review is incomplete — 2 of 5 areas.\n\nPartial findings.",
        )
        create_file(
            tmp_path,
            ".agents/focused-review/scratchpad/security--gemini--plan.md",
            "- [x] Group 1\n- [x] Group 2\n- [ ] Group 3\n- [ ] Group 4\n- [ ] Group 5",
        )

        def on_continuation(round_num: int, incomplete: list[dict]) -> None:
            finding_path = tmp_path / entries[1]["finding_path"]
            finding_path.write_text(
                "Review Status: This review is complete.\n\nAll findings.",
                encoding="utf-8",
            )
            plan_path = work_dir / "scratchpad" / "security--gemini--plan.md"
            plan_path.write_text(
                "- [x] Group 1\n- [x] Group 2\n- [x] Group 3\n- [x] Group 4\n- [x] Group 5",
                encoding="utf-8",
            )

        result = _simulate_orchestrator_loop(dispatch, work_dir, round_callback=on_continuation)

        assert result["rounds"] == 1
        assert len(result["final_complete"]) == 2
        assert result["final_failed"] == []
        assert result["stuck"] == []

    def test_stuck_agent_halted(self, tmp_path: Path) -> None:
        """Agent makes no progress → detected as stuck after 1 round."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("bugs", "opus")
        dispatch = _write_dispatch(tmp_path, [entry])

        create_file(
            tmp_path,
            entry["finding_path"],
            "Review Status: This review is incomplete — 1 of 5 areas.\n\nPartial.",
        )
        create_file(
            tmp_path,
            ".agents/focused-review/scratchpad/bugs--opus--plan.md",
            "- [x] Area 1\n- [ ] Area 2\n- [ ] Area 3\n- [ ] Area 4\n- [ ] Area 5",
        )

        def on_continuation(round_num: int, incomplete: list[dict]) -> None:
            pass  # Agent makes no progress

        result = _simulate_orchestrator_loop(dispatch, work_dir, round_callback=on_continuation)

        assert result["rounds"] == 1
        assert ("bugs", "opus") in result["stuck"]
        assert result["final_complete"] == []

    def test_max_rounds_limit(self, tmp_path: Path) -> None:
        """Agent stays incomplete → stops after max_rounds."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("bugs", "opus")
        dispatch = _write_dispatch(tmp_path, [entry])

        create_file(
            tmp_path,
            entry["finding_path"],
            "Review Status: This review is incomplete — 1 of 5 areas.\n\nInitial.",
        )

        call_count = 0

        def on_continuation(round_num: int, incomplete: list[dict]) -> None:
            nonlocal call_count
            call_count += 1
            # Make progress (grow file) so not detected as stuck, but never complete
            finding = tmp_path / entry["finding_path"]
            finding.write_text(
                f"Review Status: This review is incomplete — {call_count + 1} of 5 areas.\n\n"
                + "x" * (call_count * 100),
                encoding="utf-8",
            )

        result = _simulate_orchestrator_loop(
            dispatch, work_dir, max_rounds=2, round_callback=on_continuation,
        )

        assert result["rounds"] == 2
        assert result["final_complete"] == []
        assert result["stuck"] == []
        assert call_count == 2

    def test_mixed_complete_incomplete_failed(self, tmp_path: Path) -> None:
        """3 agents: one complete, one needs continuation, one failed."""
        work_dir = _setup_work_dir(tmp_path)
        entries = [
            _make_entry("bugs", "opus"),
            _make_entry("security", "gemini"),
            _make_entry("arch", "haiku"),
        ]
        dispatch = _write_dispatch(tmp_path, entries)

        create_file(
            tmp_path,
            entries[0]["finding_path"],
            "Review Status: This review is complete.\n\nBug findings.",
        )
        create_file(
            tmp_path,
            entries[1]["finding_path"],
            "Review Status: This review is incomplete — 2 of 5 areas.\n\nPartial.",
        )
        # entries[2] (arch/haiku): no finding file → failed

        def on_continuation(round_num: int, incomplete: list[dict]) -> None:
            finding = tmp_path / entries[1]["finding_path"]
            finding.write_text(
                "Review Status: This review is complete.\n\n"
                "All security findings.\n\n"
                "## Details\n\nFull analysis of all 5 areas completed.",
                encoding="utf-8",
            )

        result = _simulate_orchestrator_loop(dispatch, work_dir, round_callback=on_continuation)

        assert result["rounds"] == 1
        assert len(result["final_complete"]) == 2
        assert len(result["final_failed"]) == 1
        assert result["final_failed"][0]["concern"] == "arch"
        assert result["stuck"] == []

    def test_hypothesis_survives_continuation(self, tmp_path: Path) -> None:
        """Agent writes [Hypothesis] finding → continuation agent confirms it → both in final file."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("bugs", "opus")
        dispatch = _write_dispatch(tmp_path, [entry])

        create_file(
            tmp_path,
            entry["finding_path"],
            "Review Status: This review is incomplete — 1 of 3 areas.\n\n"
            "## [Hypothesis] Possible null dereference\n\n"
            "Needs verification in module X.",
        )
        create_file(
            tmp_path,
            ".agents/focused-review/scratchpad/bugs--opus--plan.md",
            "- [x] Area 1\n- [ ] Area 2\n- [ ] Area 3",
        )

        def on_continuation(round_num: int, incomplete: list[dict]) -> None:
            finding = tmp_path / entry["finding_path"]
            finding.write_text(
                "Review Status: This review is complete.\n\n"
                "## [Hypothesis] Possible null dereference\n\n"
                "Needs verification in module X.\n\n"
                "## Confirmed: Null dereference in module X\n\n"
                "Verified the null pointer issue.",
                encoding="utf-8",
            )
            plan = work_dir / "scratchpad" / "bugs--opus--plan.md"
            plan.write_text(
                "- [x] Area 1\n- [x] Area 2\n- [x] Area 3",
                encoding="utf-8",
            )

        result = _simulate_orchestrator_loop(dispatch, work_dir, round_callback=on_continuation)

        assert result["rounds"] == 1
        assert len(result["final_complete"]) == 1
        assert result["stuck"] == []

        final = (tmp_path / entry["finding_path"]).read_text(encoding="utf-8")
        assert "[Hypothesis]" in final
        assert "Confirmed: Null dereference" in final
