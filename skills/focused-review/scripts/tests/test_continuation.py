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
# TestListConcernFindings
# ---------------------------------------------------------------------------


class TestListConcernFindings:
    def test_existing_finding(self, tmp_path: Path) -> None:
        """Finding file exists → exists=True and size > 0."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("bugs", "opus")
        create_file(
            tmp_path,
            entry["finding_path"],
            "Review Status: This review is complete.\n\n## Findings\nNone.",
        )
        dispatch = _write_dispatch(tmp_path, [entry])

        result = fr.list_concern_findings(dispatch, work_dir, tmp_path)

        assert len(result) == 1
        assert result[0]["concern"] == "bugs"
        assert result[0]["model"] == "opus"
        assert result[0]["exists"] is True
        assert result[0]["size"] > 0

    def test_existing_finding_size(self, tmp_path: Path) -> None:
        """Finding file with incomplete sentinel → still just reports exists=True and size > 0."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("security", "gemini")
        content = "Review Status: This review is incomplete — 3 of 7 areas examined.\n\nPartial."
        create_file(
            tmp_path,
            entry["finding_path"],
            content,
        )
        dispatch = _write_dispatch(tmp_path, [entry])

        result = fr.list_concern_findings(dispatch, work_dir, tmp_path)

        assert len(result) == 1
        assert result[0]["concern"] == "security"
        assert result[0]["exists"] is True
        assert result[0]["size"] > 0

    def test_missing_finding(self, tmp_path: Path) -> None:
        """Finding file doesn't exist → exists=False and size=0."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("arch", "haiku")
        dispatch = _write_dispatch(tmp_path, [entry])

        result = fr.list_concern_findings(dispatch, work_dir, tmp_path)

        assert len(result) == 1
        assert result[0]["concern"] == "arch"
        assert result[0]["model"] == "haiku"
        assert result[0]["exists"] is False
        assert result[0]["size"] == 0

    def test_missing_finding_with_trace(self, tmp_path: Path) -> None:
        """Missing finding with trace file → trace_path present."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("arch", "haiku")
        create_file(
            tmp_path,
            ".agents/focused-review/traces/concern--arch--haiku.jsonl",
            '{"type":"trace"}',
        )
        dispatch = _write_dispatch(tmp_path, [entry])

        result = fr.list_concern_findings(dispatch, work_dir, tmp_path)

        assert len(result) == 1
        assert result[0]["exists"] is False
        assert "trace_path" in result[0]

    def test_empty_finding_file(self, tmp_path: Path) -> None:
        """Empty finding file → exists=True and size=0."""
        work_dir = _setup_work_dir(tmp_path)
        entry = _make_entry("bugs", "opus")
        create_file(tmp_path, entry["finding_path"], "")
        dispatch = _write_dispatch(tmp_path, [entry])

        result = fr.list_concern_findings(dispatch, work_dir, tmp_path)

        assert len(result) == 1
        assert result[0]["exists"] is True
        assert result[0]["size"] == 0

    def test_mixed_results(self, tmp_path: Path) -> None:
        """Multiple entries → correct exists/size for each."""
        work_dir = _setup_work_dir(tmp_path)
        existing_entry = _make_entry("bugs", "opus")
        another_entry = _make_entry("security", "gemini")
        missing_entry = _make_entry("arch", "haiku")

        create_file(
            tmp_path,
            existing_entry["finding_path"],
            "Review Status: This review is complete.\n\nDone.",
        )
        create_file(
            tmp_path,
            another_entry["finding_path"],
            "Review Status: This review is incomplete — 2 of 5 areas.\n\nPartial.",
        )
        # missing_entry: no finding file created

        dispatch = _write_dispatch(
            tmp_path, [existing_entry, another_entry, missing_entry]
        )

        result = fr.list_concern_findings(dispatch, work_dir, tmp_path)

        assert len(result) == 3
        bugs = next(r for r in result if r["concern"] == "bugs")
        security = next(r for r in result if r["concern"] == "security")
        arch = next(r for r in result if r["concern"] == "arch")
        assert bugs["exists"] is True
        assert bugs["size"] > 0
        assert security["exists"] is True
        assert security["size"] > 0
        assert arch["exists"] is False
        assert arch["size"] == 0


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
# Subcommand integration tests
# ---------------------------------------------------------------------------


class TestListFindingsSubcommand:
    def test_json_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Subcommand produces valid JSON list with finding metadata."""
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
            repo=str(tmp_path),
        )
        fr._list_findings_cmd(args)

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert isinstance(output, list)
        assert len(output) == 1
        assert output[0]["concern"] == "bugs"
        assert output[0]["exists"] is True
        assert output[0]["size"] > 0


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


# ---------------------------------------------------------------------------
# Integration: Full Continuation Loop
# ---------------------------------------------------------------------------


def _simulate_orchestrator_loop(
    dispatch_path: Path,
    work_dir: Path,
    max_rounds: int = 3,
    *,
    repo: Path | None = None,
    round_callback: Callable[[int, list[dict]], None] | None = None,
) -> dict:
    """Simulate the SKILL.md continuation loop using Python helpers.

    Classification logic lives here (simulating the orchestrator LLM) —
    list_concern_findings only reports metadata.

    round_callback is called before each continuation round with
    (round_number, incomplete_entries) — test code uses this to
    simulate agent behavior (modify files on disk).

    Returns: {
        'rounds': int,
        'final_complete': list,
        'final_failed': list,
    }
    """
    if repo is None:
        repo = work_dir.parent.parent
    current_dispatch = dispatch_path
    all_complete: list[dict] = []
    all_failed: list[dict] = []
    round_num = 0

    while True:
        findings_info = fr.list_concern_findings(current_dispatch, work_dir, repo)

        # Orchestrator classification (simulated here — in prod, the LLM does this)
        complete: list[dict] = []
        incomplete: list[dict] = []
        failed: list[dict] = []
        for info in findings_info:
            if not info["exists"]:
                failed.append(info)
            else:
                finding_path = repo / str(info["finding_path"])
                text = finding_path.read_text(encoding="utf-8")
                first_line = text.split("\n", 1)[0] if text else ""
                if first_line.startswith(fr._INCOMPLETE_SENTINEL):
                    incomplete.append(info)
                elif first_line.startswith(fr._COMPLETE_SENTINEL):
                    complete.append(info)
                else:
                    complete.append(info)  # legacy

        all_complete.extend(complete)
        all_failed.extend(failed)

        if not incomplete or round_num >= max_rounds:
            return {
                "rounds": round_num,
                "final_complete": all_complete,
                "final_failed": all_failed,
            }

        incomplete_pairs = [(str(e["concern"]), str(e["model"])) for e in incomplete]
        cont_path = work_dir.parent / "concern-dispatch-continue.json"
        fr.build_continuation_dispatch(dispatch_path, incomplete_pairs, cont_path)

        if round_callback:
            round_callback(round_num, incomplete)

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

        final = (tmp_path / entry["finding_path"]).read_text(encoding="utf-8")
        assert "[Hypothesis]" in final
        assert "Confirmed: Null dereference" in final
