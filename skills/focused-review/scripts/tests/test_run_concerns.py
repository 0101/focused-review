"""Tests for the run-concerns subcommand."""

from __future__ import annotations

import argparse
import json
import os
import subprocess as sp
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests import create_file

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importlib

fr = importlib.import_module("focused-review")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dispatch(
    entries: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Build a default concern-dispatch.json payload."""
    if entries is not None:
        return entries
    return [
        {
            "concern": "bugs",
            "model": "opus",
            "priority": "high",
            "prompt_path": ".agents/focused-review/prompts/bugs--opus.md",
        },
    ]


def _setup_work_dir(
    repo: Path,
    dispatch: list[dict[str, str]] | None = None,
    *,
    write_prompts: bool = True,
) -> Path:
    """Set up the work directory with dispatch JSON and prompt files."""
    work_dir = repo / ".agents" / "focused-review"
    work_dir.mkdir(parents=True, exist_ok=True)

    entries = _make_dispatch(dispatch)

    dispatch_path = work_dir / "concern-dispatch.json"
    dispatch_path.write_text(json.dumps(entries), encoding="utf-8")

    if write_prompts:
        prompts_dir = work_dir / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            prompt_file = repo / entry["prompt_path"]
            prompt_file.parent.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text(
                f"# {entry['concern'].title()}\n## Role\nReview {entry['concern']}.",
                encoding="utf-8",
            )

    return work_dir


def _mock_subprocess_success(stdout: str = "### [High] Bug found\nDetails.") -> MagicMock:
    """Create a mock for subprocess.run that returns success."""
    mock = MagicMock()
    mock.returncode = 0
    mock.stdout = stdout
    mock.stderr = ""
    return mock


def _mock_subprocess_failure(
    returncode: int = 1,
    stderr: str = "Error occurred",
) -> MagicMock:
    """Create a mock for subprocess.run that returns failure."""
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = ""
    mock.stderr = stderr
    return mock


# ---------------------------------------------------------------------------
# _resolve_model and MODEL_MAP
# ---------------------------------------------------------------------------


class TestResolveModel:
    """Tests for the model name resolution mapping."""

    def test_opus_maps_to_full_name(self) -> None:
        assert fr._resolve_model("opus") == "claude-opus-4.6"

    def test_sonnet_maps_to_full_name(self) -> None:
        assert fr._resolve_model("sonnet") == "claude-sonnet-4.6"

    def test_haiku_maps_to_full_name(self) -> None:
        assert fr._resolve_model("haiku") == "claude-haiku-4.5"

    def test_codex_maps_to_full_name(self) -> None:
        assert fr._resolve_model("codex") == "gpt-5.3-codex"

    def test_gemini_maps_to_full_name(self) -> None:
        assert fr._resolve_model("gemini") == "gemini-3-pro-preview"

    def test_unknown_model_passes_through(self) -> None:
        """Unknown names pass through unchanged (user may specify full CLI name)."""
        assert fr._resolve_model("claude-opus-4.6") == "claude-opus-4.6"

    def test_arbitrary_model_passes_through(self) -> None:
        assert fr._resolve_model("some-future-model-v2") == "some-future-model-v2"

    def test_inherit_passes_through(self) -> None:
        """'inherit' is not in MODEL_MAP and should pass through."""
        assert fr._resolve_model("inherit") == "inherit"

    def test_case_insensitive_opus(self) -> None:
        """Model lookup is case-insensitive."""
        assert fr._resolve_model("Opus") == "claude-opus-4.6"
        assert fr._resolve_model("OPUS") == "claude-opus-4.6"

    def test_case_insensitive_codex(self) -> None:
        assert fr._resolve_model("Codex") == "gpt-5.3-codex"


# ---------------------------------------------------------------------------
# _run_single_concern: success cases
# ---------------------------------------------------------------------------


class TestRunSingleConcernSuccess:

    def test_success_writes_finding(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        with patch("subprocess.run", return_value=_mock_subprocess_success()):
            result = fr._run_single_concern(entry, repo, work_dir)

        assert result["status"] == "exited"
        assert result["concern"] == "bugs"
        assert result["model"] == "opus"
        # finding_path should be posix-style
        assert "\\" not in result["finding_path"]
        assert "concern--bugs--opus.md" in result["finding_path"]

        finding_path = work_dir / "findings" / "concern--bugs--opus.md"
        assert finding_path.exists()
        assert "Bug found" in finding_path.read_text(encoding="utf-8")

    def test_finding_filename_format(self, tmp_path: Path) -> None:
        """Finding filename is concern--{name}--{model}.md."""
        repo = tmp_path / "repo"
        repo.mkdir()
        entries = [
            {
                "concern": "security",
                "model": "codex",
                "priority": "high",
                "prompt_path": ".agents/focused-review/prompts/security--codex.md",
            }
        ]
        work_dir = _setup_work_dir(repo, entries)

        with patch("subprocess.run", return_value=_mock_subprocess_success()):
            result = fr._run_single_concern(entries[0], repo, work_dir)

        assert result["status"] == "exited"
        finding_path = work_dir / "findings" / "concern--security--codex.md"
        assert finding_path.exists()

    def test_creates_findings_dir_if_missing(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        findings_dir = work_dir / "findings"
        assert not findings_dir.exists()

        with patch("subprocess.run", return_value=_mock_subprocess_success()):
            fr._run_single_concern(entry, repo, work_dir)

        assert findings_dir.is_dir()

    def test_trace_file_written_on_success(self, tmp_path: Path) -> None:
        """Raw stdout is saved to traces/ for debugging."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        with patch("subprocess.run", return_value=_mock_subprocess_success()):
            fr._run_single_concern(entry, repo, work_dir)

        trace_path = work_dir / "traces" / "concern--bugs--opus.jsonl"
        assert trace_path.exists()
        assert "Bug found" in trace_path.read_text(encoding="utf-8")

    def test_agent_written_file_preferred_over_stdout(self, tmp_path: Path) -> None:
        """When the agent writes clean findings to the expected path,
        stdout (with tool-call noise) is only saved to the trace."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]
        # Add finding_path to dispatch entry (as the real code does)
        finding_rel = ".agents/focused-review/findings/concern--bugs--opus.md"
        entry["finding_path"] = finding_rel

        # Pre-create the file as if the agent wrote it via the create tool
        finding_path = repo / Path(finding_rel.replace("/", os.sep))
        finding_path.parent.mkdir(parents=True, exist_ok=True)
        finding_path.write_text("### [High] Clean finding\nNo noise.", encoding="utf-8")

        noisy_stdout = "● Read file.cs\n● Search grep\n### [High] Bug found\nDetails."
        with patch("subprocess.run", return_value=_mock_subprocess_success(noisy_stdout)):
            result = fr._run_single_concern(entry, repo, work_dir)

        assert result["status"] == "exited"
        # The finding file should contain the agent-written content, not stdout
        content = finding_path.read_text(encoding="utf-8")
        assert "Clean finding" in content
        assert "Read file.cs" not in content

        # Trace should contain the noisy stdout
        trace_path = work_dir / "traces" / "concern--bugs--opus.jsonl"
        assert "Read file.cs" in trace_path.read_text(encoding="utf-8")

    def test_stdout_fallback_when_agent_does_not_write_file(self, tmp_path: Path) -> None:
        """When the agent fails to write the file, stdout is used as fallback."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]
        entry["finding_path"] = ".agents/focused-review/findings/concern--bugs--opus.md"

        # Agent doesn't write the file — subprocess returns stdout only
        with patch("subprocess.run", return_value=_mock_subprocess_success()):
            result = fr._run_single_concern(entry, repo, work_dir)

        assert result["status"] == "exited"
        finding_path = work_dir / "findings" / "concern--bugs--opus.md"
        assert finding_path.exists()
        assert "Bug found" in finding_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# _run_single_concern: error cases
# ---------------------------------------------------------------------------


class TestRunSingleConcernErrors:

    def test_missing_prompt_file_returns_error(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo, write_prompts=False)
        entry = _make_dispatch()[0]

        result = fr._run_single_concern(entry, repo, work_dir)

        assert result["status"] == "error"
        assert "Prompt file not found" in result["error"]

    def test_copilot_not_found_returns_error(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = fr._run_single_concern(entry, repo, work_dir)

        assert result["status"] == "error"
        assert "not installed or not on PATH" in result["error"]

    def test_copilot_not_found_single_attempt(self, tmp_path: Path) -> None:
        """FileNotFoundError returns error after single attempt."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        mock_run = MagicMock(side_effect=FileNotFoundError)
        with patch("subprocess.run", mock_run):
            result = fr._run_single_concern(entry, repo, work_dir)

        assert mock_run.call_count == 1
        assert result["status"] == "error"

    def test_os_error_returns_error(self, tmp_path: Path) -> None:
        """OSError (e.g. prompt exceeds Windows CreateProcess limit) returns error."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        with patch("subprocess.run", side_effect=OSError("[Errno 7] Argument list too long")):
            result = fr._run_single_concern(entry, repo, work_dir)

        assert result["status"] == "error"
        assert "OS error" in result["error"]
        assert "CLI argument limit" in result["error"]

    def test_os_error_single_attempt(self, tmp_path: Path) -> None:
        """OSError returns error after single attempt."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        mock_run = MagicMock(side_effect=OSError("too long"))
        with patch("subprocess.run", mock_run):
            result = fr._run_single_concern(entry, repo, work_dir)

        assert mock_run.call_count == 1
        assert result["status"] == "error"

    def test_nonzero_exit_returns_exited_no_finding(self, tmp_path: Path) -> None:
        """Non-zero exit returns status=exited without finding_path."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        with patch("subprocess.run", return_value=_mock_subprocess_failure()):
            result = fr._run_single_concern(entry, repo, work_dir)

        assert result["status"] == "exited"
        assert "finding_path" not in result

    def test_empty_output_returns_exited_no_finding(self, tmp_path: Path) -> None:
        """Zero exit but empty stdout returns status=exited without finding_path."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        empty_result = _mock_subprocess_success(stdout="   \n  ")
        with patch("subprocess.run", return_value=empty_result):
            result = fr._run_single_concern(entry, repo, work_dir)

        assert result["status"] == "exited"
        assert "finding_path" not in result


# ---------------------------------------------------------------------------
# _run_single_concern: timeout behavior
# ---------------------------------------------------------------------------


class TestRunSingleConcernTimeout:

    def test_timeout_returns_timed_out(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        mock_run = MagicMock(
            side_effect=sp.TimeoutExpired(cmd="copilot", timeout=10)
        )
        with patch("subprocess.run", mock_run):
            result = fr._run_single_concern(entry, repo, work_dir, hard_timeout=10)

        assert result["status"] == "timed_out"
        assert mock_run.call_count == 1

    def test_timeout_with_partial_finding_includes_path(self, tmp_path: Path) -> None:
        """When the agent wrote a partial finding before timeout, finding_path is included."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        # Pre-create a partial finding as if the agent wrote it before being killed
        findings_dir = work_dir / "findings"
        findings_dir.mkdir(parents=True, exist_ok=True)
        partial = findings_dir / "concern--bugs--opus.md"
        partial.write_text("### Partial finding\nAgent was interrupted.", encoding="utf-8")

        mock_run = MagicMock(
            side_effect=sp.TimeoutExpired(cmd="copilot", timeout=900)
        )
        with patch("subprocess.run", mock_run):
            result = fr._run_single_concern(entry, repo, work_dir, hard_timeout=900)

        assert result["status"] == "timed_out"
        assert "finding_path" in result
        assert "concern--bugs--opus.md" in result["finding_path"]

    def test_timeout_without_finding_omits_path(self, tmp_path: Path) -> None:
        """When no finding file exists after timeout, finding_path is absent."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        mock_run = MagicMock(
            side_effect=sp.TimeoutExpired(cmd="copilot", timeout=900)
        )
        with patch("subprocess.run", mock_run):
            result = fr._run_single_concern(entry, repo, work_dir, hard_timeout=900)

        assert result["status"] == "timed_out"
        assert "finding_path" not in result

    def test_single_attempt_no_retry(self, tmp_path: Path) -> None:
        """Only one subprocess invocation — no retry loop."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        mock_run = MagicMock(return_value=_mock_subprocess_failure())
        with patch("subprocess.run", mock_run):
            fr._run_single_concern(entry, repo, work_dir)

        assert mock_run.call_count == 1

    def test_prompt_passed_as_cli_argument(self, tmp_path: Path) -> None:
        """Prompt content is passed as direct CLI argument to ``-p``."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        mock_run = MagicMock(return_value=_mock_subprocess_success())
        with patch("subprocess.run", mock_run):
            fr._run_single_concern(entry, repo, work_dir)

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        # cmd uses "-p" followed by the actual prompt content (not "-")
        assert "-p" in cmd
        prompt_arg = cmd[cmd.index("-p") + 1]
        assert prompt_arg != "-"
        assert "bugs" in prompt_arg.lower() or "Review" in prompt_arg
        # No input= kwarg (stdin not used)
        assert call_args.kwargs.get("input") is None

    def test_model_propagated_in_command(self, tmp_path: Path) -> None:
        """Model from dispatch entry is resolved and passed as --model to copilot CLI."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]  # model="opus"

        mock_run = MagicMock(return_value=_mock_subprocess_success())
        with patch("subprocess.run", mock_run):
            fr._run_single_concern(entry, repo, work_dir)

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        # Shorthand "opus" should be resolved to full CLI name
        assert cmd[model_idx + 1] == "claude-opus-4.6"

    def test_inherit_model_not_in_command(self, tmp_path: Path) -> None:
        """When model is 'inherit', --model flag is omitted."""
        repo = tmp_path / "repo"
        repo.mkdir()
        entries = [
            {
                "concern": "bugs",
                "model": "inherit",
                "priority": "high",
                "prompt_path": ".agents/focused-review/prompts/bugs--inherit.md",
            }
        ]
        work_dir = _setup_work_dir(repo, entries)

        mock_run = MagicMock(return_value=_mock_subprocess_success())
        with patch("subprocess.run", mock_run):
            fr._run_single_concern(entries[0], repo, work_dir)

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "--model" not in cmd


# ---------------------------------------------------------------------------
# run_concerns subcommand: end-to-end
# ---------------------------------------------------------------------------


class TestRunConcerns:

    def test_empty_dispatch_prints_summary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _setup_work_dir(repo, dispatch=[])

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=2,
            hard_timeout=60,
        )
        fr.run_concerns(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["total"] == 0
        assert summary["success"] == 0
        assert summary["failed"] == 0
        assert summary["results"] == []

    def test_missing_dispatch_file_exits(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        # No dispatch file created

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=2,
            hard_timeout=60,
        )
        with pytest.raises(SystemExit, match="1"):
            fr.run_concerns(args)

    def test_successful_run_produces_summary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        entries = [
            {
                "concern": "bugs",
                "model": "opus",
                "priority": "high",
                "prompt_path": ".agents/focused-review/prompts/bugs--opus.md",
            },
            {
                "concern": "security",
                "model": "opus",
                "priority": "high",
                "prompt_path": ".agents/focused-review/prompts/security--opus.md",
            },
        ]
        _setup_work_dir(repo, entries)

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=2,
            hard_timeout=60,
        )
        with patch("subprocess.run", return_value=_mock_subprocess_success()):
            fr.run_concerns(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["total"] == 2
        assert summary["success"] == 2
        assert summary["failed"] == 0

    def test_partial_failure_counted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        entries = [
            {
                "concern": "bugs",
                "model": "opus",
                "priority": "high",
                "prompt_path": ".agents/focused-review/prompts/bugs--opus.md",
            },
            {
                "concern": "security",
                "model": "opus",
                "priority": "high",
                "prompt_path": ".agents/focused-review/prompts/security--opus.md",
            },
        ]
        _setup_work_dir(repo, entries)

        # First call succeeds, second fails
        mock_run = MagicMock(
            side_effect=[
                _mock_subprocess_success(),
                _mock_subprocess_failure(),
            ]
        )

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=1,  # serial to get deterministic order
            hard_timeout=60,
        )
        with patch("subprocess.run", mock_run):
            fr.run_concerns(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["total"] == 2
        assert summary["success"] == 1
        assert summary["failed"] == 1

    def test_findings_written_to_correct_paths(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        entries = [
            {
                "concern": "bugs",
                "model": "opus",
                "priority": "high",
                "prompt_path": ".agents/focused-review/prompts/bugs--opus.md",
            },
        ]
        _setup_work_dir(repo, entries)

        finding_content = "### [High] Race condition\nDetails here."

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=1,
            hard_timeout=60,
        )
        with patch("subprocess.run", return_value=_mock_subprocess_success(finding_content)):
            fr.run_concerns(args)

        finding_path = repo / ".agents" / "focused-review" / "findings" / "concern--bugs--opus.md"
        assert finding_path.exists()
        assert "Race condition" in finding_path.read_text(encoding="utf-8")

    def test_progress_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _setup_work_dir(repo)

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=1,
            hard_timeout=60,
        )
        with patch("subprocess.run", return_value=_mock_subprocess_success()):
            fr.run_concerns(args)

        captured = capsys.readouterr()
        assert "bugs" in captured.err
        assert "opus" in captured.err

    def test_unexpected_exception_captured_as_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unexpected exception in _run_single_concern is captured, not raised."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _setup_work_dir(repo)

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=1,
            hard_timeout=60,
        )
        with patch.object(
            fr,
            "_run_single_concern",
            side_effect=PermissionError("disk full"),
        ):
            fr.run_concerns(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["total"] == 1
        assert summary["failed"] == 1
        result = summary["results"][0]
        assert result["status"] == "error"
        assert "Unexpected" in result["error"]
        assert "disk full" in result["error"]


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestRunConcernsCLI:

    def test_run_concerns_registered_as_subcommand(self) -> None:
        """The run-concerns subcommand is recognized by the parser."""
        with patch("sys.argv", ["focused-review", "run-concerns", "--help"]):
            with pytest.raises(SystemExit, match="0"):
                fr.main()

    def test_default_args_from_real_parser(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Default argument values from the real parser match module constants."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _setup_work_dir(repo, dispatch=[])

        captured_args: dict[str, object] = {}

        def spy_run_concerns(args: argparse.Namespace) -> None:
            captured_args["max_workers"] = args.max_workers
            captured_args["hard_timeout"] = args.hard_timeout

        with patch("sys.argv", [
            "focused-review", "run-concerns", "--repo", str(repo),
        ]):
            with patch.object(fr, "run_concerns", spy_run_concerns):
                fr.main()

        assert captured_args["max_workers"] == fr.CONCERN_MAX_WORKERS
        assert captured_args["hard_timeout"] == fr.CONCERN_HARD_TIMEOUT_SECS

    def test_custom_args_passed(self, tmp_path: Path) -> None:
        """Custom CLI args override defaults."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _setup_work_dir(repo, dispatch=[])

        with patch(
            "sys.argv",
            [
                "focused-review",
                "run-concerns",
                "--repo",
                str(repo),
                "--max-workers",
                "8",
                "--hard-timeout",
                "120",
            ],
        ):
            # Capture the args by monkey-patching run_concerns
            captured_args = {}

            original_run = fr.run_concerns

            def capture_args(args: argparse.Namespace) -> None:
                captured_args["max_workers"] = args.max_workers
                captured_args["hard_timeout"] = args.hard_timeout
                original_run(args)

            with patch.object(fr, "run_concerns", capture_args):
                fr.main()

        assert captured_args["max_workers"] == 8
        assert captured_args["hard_timeout"] == 120


# ---------------------------------------------------------------------------
# ThreadPoolExecutor integration
# ---------------------------------------------------------------------------


class TestConcernParallelism:

    def test_multiple_concerns_run_in_parallel(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Multiple concerns are dispatched to ThreadPoolExecutor."""
        repo = tmp_path / "repo"
        repo.mkdir()
        entries = [
            {
                "concern": "bugs",
                "model": "opus",
                "priority": "high",
                "prompt_path": ".agents/focused-review/prompts/bugs--opus.md",
            },
            {
                "concern": "security",
                "model": "opus",
                "priority": "high",
                "prompt_path": ".agents/focused-review/prompts/security--opus.md",
            },
        ]
        _setup_work_dir(repo, entries)

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=3,
            hard_timeout=60,
        )
        with patch("subprocess.run", return_value=_mock_subprocess_success()):
            fr.run_concerns(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["total"] == 2
        assert summary["success"] == 2

        # Both finding files created
        findings_dir = repo / ".agents" / "focused-review" / "findings"
        assert (findings_dir / "concern--bugs--opus.md").exists()
        assert (findings_dir / "concern--security--opus.md").exists()

    def test_multi_model_concerns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Different models for the same concern produce separate findings."""
        repo = tmp_path / "repo"
        repo.mkdir()
        entries = [
            {
                "concern": "bugs",
                "model": "opus",
                "priority": "high",
                "prompt_path": ".agents/focused-review/prompts/bugs--opus.md",
            },
            {
                "concern": "bugs",
                "model": "codex",
                "priority": "high",
                "prompt_path": ".agents/focused-review/prompts/bugs--codex.md",
            },
        ]
        _setup_work_dir(repo, entries)

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=2,
            hard_timeout=60,
        )
        with patch("subprocess.run", return_value=_mock_subprocess_success()):
            fr.run_concerns(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["total"] == 2
        assert summary["success"] == 2

        findings_dir = repo / ".agents" / "focused-review" / "findings"
        assert (findings_dir / "concern--bugs--opus.md").exists()
        assert (findings_dir / "concern--bugs--codex.md").exists()


# ---------------------------------------------------------------------------
# --dispatch alternate dispatch file
# ---------------------------------------------------------------------------


class TestDispatchArg:

    def test_custom_dispatch_file_used(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When --dispatch is provided, run_concerns reads that file."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = repo / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        # Write the alternate dispatch file in a non-default location
        alt_dispatch = tmp_path / "continue-dispatch.json"
        entries = [
            {
                "concern": "security",
                "model": "opus",
                "priority": "high",
                "prompt_path": ".agents/focused-review/prompts/security--opus.md",
            },
        ]
        alt_dispatch.write_text(json.dumps(entries), encoding="utf-8")

        # Create prompt file so _run_single_concern can find it
        prompts_dir = work_dir / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / "security--opus.md").write_text("Review security", encoding="utf-8")

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=2,
            hard_timeout=60,
            dispatch=str(alt_dispatch),
        )
        with patch("subprocess.run", return_value=_mock_subprocess_success()):
            fr.run_concerns(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["total"] == 1
        assert summary["success"] == 1
        assert summary["results"][0]["concern"] == "security"

    def test_default_dispatch_when_arg_is_none(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When dispatch is None, the default concern-dispatch.json is used."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _setup_work_dir(repo, dispatch=[])

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=2,
            hard_timeout=60,
            dispatch=None,
        )
        fr.run_concerns(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["total"] == 0

    def test_missing_custom_dispatch_file_exits(self, tmp_path: Path) -> None:
        """When --dispatch points to a nonexistent file, run_concerns exits."""
        repo = tmp_path / "repo"
        repo.mkdir()

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=2,
            hard_timeout=60,
            dispatch=str(tmp_path / "nonexistent.json"),
        )
        with pytest.raises(SystemExit, match="1"):
            fr.run_concerns(args)

    def test_dispatch_cli_arg_parsed(self, tmp_path: Path) -> None:
        """The --dispatch CLI argument is captured by argparse."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _setup_work_dir(repo, dispatch=[])

        captured_args: dict[str, object] = {}

        def spy_run_concerns(args: argparse.Namespace) -> None:
            captured_args["dispatch"] = args.dispatch

        alt_path = str(tmp_path / "my-dispatch.json")
        with patch("sys.argv", [
            "focused-review", "run-concerns",
            "--repo", str(repo),
            "--dispatch", alt_path,
        ]):
            with patch.object(fr, "run_concerns", spy_run_concerns):
                fr.main()

        assert captured_args["dispatch"] == alt_path

    def test_dispatch_cli_default_is_none(self, tmp_path: Path) -> None:
        """When --dispatch is not provided, the default is None."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _setup_work_dir(repo, dispatch=[])

        captured_args: dict[str, object] = {}

        def spy_run_concerns(args: argparse.Namespace) -> None:
            captured_args["dispatch"] = args.dispatch

        with patch("sys.argv", [
            "focused-review", "run-concerns", "--repo", str(repo),
        ]):
            with patch.object(fr, "run_concerns", spy_run_concerns):
                fr.main()

        assert captured_args["dispatch"] is None
