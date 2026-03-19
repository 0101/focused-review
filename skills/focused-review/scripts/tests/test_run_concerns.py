"""Tests for the run-concerns subcommand."""

from __future__ import annotations

import argparse
import json
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


def _write_finding(
    repo: Path,
    concern: str,
    model: str,
    content: str = "### [High] Bug found\nDetails.",
) -> Path:
    """Pre-create a findings file as if the agent wrote it during subprocess.run."""
    finding_path = repo / ".agents" / "focused-review" / "findings" / f"concern--{concern}--{model}.md"
    finding_path.parent.mkdir(parents=True, exist_ok=True)
    finding_path.write_text(content, encoding="utf-8")
    return finding_path


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
        _write_finding(repo, "bugs", "opus")

        with patch("subprocess.run", return_value=_mock_subprocess_success()):
            result = fr._run_single_concern(entry, repo, work_dir)

        assert result["status"] == "success"
        assert result["concern"] == "bugs"
        assert result["model"] == "opus"
        assert result["attempt"] == 1
        # finding_path should be posix-style
        assert "\\" not in result["finding_path"]
        assert "concern--bugs--opus.md" in result["finding_path"]

        finding_path = work_dir / "findings" / "concern--bugs--opus.md"
        assert finding_path.exists()

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
        _write_finding(repo, "security", "codex")

        with patch("subprocess.run", return_value=_mock_subprocess_success()):
            result = fr._run_single_concern(entries[0], repo, work_dir)

        assert result["status"] == "success"
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

        trace_path = work_dir / "traces" / "concern--bugs--opus.md"
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
        finding_path = repo / Path(finding_rel.replace("/", "\\"))
        finding_path.parent.mkdir(parents=True, exist_ok=True)
        finding_path.write_text("### [High] Clean finding\nNo noise.", encoding="utf-8")

        noisy_stdout = "● Read file.cs\n● Search grep\n### [High] Bug found\nDetails."
        with patch("subprocess.run", return_value=_mock_subprocess_success(noisy_stdout)):
            result = fr._run_single_concern(entry, repo, work_dir)

        assert result["status"] == "success"
        # The finding file should contain the agent-written content, not stdout
        content = finding_path.read_text(encoding="utf-8")
        assert "Clean finding" in content
        assert "Read file.cs" not in content

        # Trace should contain the noisy stdout
        trace_path = work_dir / "traces" / "concern--bugs--opus.md"
        assert "Read file.cs" in trace_path.read_text(encoding="utf-8")

    def test_stdout_fallback_when_agent_does_not_write_file(self, tmp_path: Path) -> None:
        """When the agent fails to write the file, report as failed."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]
        entry["finding_path"] = ".agents/focused-review/findings/concern--bugs--opus.md"

        # Agent doesn't write the file — subprocess returns stdout only
        with patch("subprocess.run", return_value=_mock_subprocess_success()):
            result = fr._run_single_concern(entry, repo, work_dir)

        assert result["status"] == "failed"
        assert "did not write findings" in result["error"]
        finding_path = work_dir / "findings" / "concern--bugs--opus.md"
        assert not finding_path.exists()


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
        assert result["attempt"] == 0

    def test_copilot_not_found_returns_error(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = fr._run_single_concern(entry, repo, work_dir)

        assert result["status"] == "error"
        assert "not installed or not on PATH" in result["error"]

    def test_copilot_not_found_does_not_retry(self, tmp_path: Path) -> None:
        """FileNotFoundError should not be retried — copilot won't appear."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        mock_run = MagicMock(side_effect=FileNotFoundError)
        with patch("subprocess.run", mock_run):
            fr._run_single_concern(entry, repo, work_dir, retries=3)

        assert mock_run.call_count == 1

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

    def test_os_error_does_not_retry(self, tmp_path: Path) -> None:
        """OSError should not be retried — the prompt won't shrink."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        mock_run = MagicMock(side_effect=OSError("too long"))
        with patch("subprocess.run", mock_run):
            fr._run_single_concern(entry, repo, work_dir, retries=3)

        assert mock_run.call_count == 1

    def test_nonzero_exit_returns_failed(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        with patch("subprocess.run", return_value=_mock_subprocess_failure()):
            result = fr._run_single_concern(entry, repo, work_dir, retries=0)

        assert result["status"] == "failed"
        assert "Error occurred" in result["error"]

    def test_empty_output_returns_failed(self, tmp_path: Path) -> None:
        """Zero exit but empty stdout is treated as failure."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        empty_result = _mock_subprocess_success(stdout="   \n  ")
        with patch("subprocess.run", return_value=empty_result):
            result = fr._run_single_concern(entry, repo, work_dir, retries=0)

        assert result["status"] == "failed"
        assert "Empty output" in result["error"]


# ---------------------------------------------------------------------------
# _run_single_concern: retry and timeout
# ---------------------------------------------------------------------------


class TestRunSingleConcernRetry:

    def test_retries_on_failure_then_succeeds(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]
        _write_finding(repo, "bugs", "opus")

        mock_run = MagicMock(
            side_effect=[
                _mock_subprocess_failure(),
                _mock_subprocess_success(),
            ]
        )
        with patch("subprocess.run", mock_run):
            result = fr._run_single_concern(entry, repo, work_dir, retries=1)

        assert result["status"] == "success"
        assert result["attempt"] == 2
        assert mock_run.call_count == 2

    def test_exhausts_retries_returns_failed(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        mock_run = MagicMock(return_value=_mock_subprocess_failure())
        with patch("subprocess.run", mock_run):
            result = fr._run_single_concern(entry, repo, work_dir, retries=2)

        assert result["status"] == "failed"
        assert result["attempt"] == 3  # 1 + 2 retries
        assert mock_run.call_count == 3

    def test_timeout_triggers_retry(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]
        _write_finding(repo, "bugs", "opus")

        mock_run = MagicMock(
            side_effect=[
                sp.TimeoutExpired(cmd="copilot", timeout=10),
                _mock_subprocess_success(),
            ]
        )
        with patch("subprocess.run", mock_run):
            result = fr._run_single_concern(entry, repo, work_dir, retries=1, timeout=10)

        assert result["status"] == "success"
        assert result["attempt"] == 2

    def test_timeout_exhausted_returns_failed(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        mock_run = MagicMock(
            side_effect=sp.TimeoutExpired(cmd="copilot", timeout=10)
        )
        with patch("subprocess.run", mock_run):
            result = fr._run_single_concern(entry, repo, work_dir, retries=1, timeout=10)

        assert result["status"] == "failed"
        assert "Timed out" in result["error"]
        assert result["attempt"] == 2

    def test_zero_retries_means_one_attempt(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        mock_run = MagicMock(return_value=_mock_subprocess_failure())
        with patch("subprocess.run", mock_run):
            result = fr._run_single_concern(entry, repo, work_dir, retries=0)

        assert result["attempt"] == 1
        assert mock_run.call_count == 1

    def test_prompt_passed_as_cli_argument(self, tmp_path: Path) -> None:
        """Prompt content is passed as direct CLI argument to ``-p``."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = _setup_work_dir(repo)
        entry = _make_dispatch()[0]

        mock_run = MagicMock(return_value=_mock_subprocess_success())
        with patch("subprocess.run", mock_run):
            fr._run_single_concern(entry, repo, work_dir, retries=0)

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
            fr._run_single_concern(entry, repo, work_dir, retries=0)

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        # Shorthand "opus" should be resolved to full CLI name
        assert cmd[model_idx + 1] == "claude-opus-4.6"

    def test_inherit_model_without_flag_omits_model(self, tmp_path: Path) -> None:
        """When model is 'inherit' and no inherit_model provided, --model is omitted."""
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
            fr._run_single_concern(entries[0], repo, work_dir, retries=0)

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "--model" not in cmd

    def test_inherit_model_with_flag_uses_orchestrator_model(self, tmp_path: Path) -> None:
        """When model is 'inherit' and inherit_model is provided, that model is used."""
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
            fr._run_single_concern(
                entries[0], repo, work_dir, retries=0,
                inherit_model="claude-opus-4.6",
            )

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-opus-4.6"


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
            timeout=60,
            retries=0,
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
            timeout=60,
            retries=0,
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
        _write_finding(repo, "bugs", "opus")
        _write_finding(repo, "security", "opus")

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=2,
            timeout=60,
            retries=0,
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
        _write_finding(repo, "bugs", "opus")

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
            timeout=60,
            retries=0,
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
            timeout=60,
            retries=0,
        )
        with patch("subprocess.run", return_value=_mock_subprocess_success(finding_content)):
            fr.run_concerns(args)

        finding_path = repo / ".agents" / "focused-review" / "findings" / "concern--bugs--opus.md"
        # Agent didn't write the file (mock doesn't create it), so reported as failed
        assert not finding_path.exists()

    def test_progress_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _setup_work_dir(repo)

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=1,
            timeout=60,
            retries=0,
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
            timeout=60,
            retries=0,
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
            captured_args["timeout"] = args.timeout
            captured_args["retries"] = args.retries

        with patch("sys.argv", [
            "focused-review", "run-concerns", "--repo", str(repo),
        ]):
            with patch.object(fr, "run_concerns", spy_run_concerns):
                fr.main()

        assert captured_args["max_workers"] == fr.CONCERN_MAX_WORKERS
        assert captured_args["timeout"] == fr.CONCERN_TIMEOUT_SECS
        assert captured_args["retries"] == fr.CONCERN_RETRIES

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
                "--timeout",
                "120",
                "--retries",
                "5",
            ],
        ):
            # Capture the args by monkey-patching run_concerns
            captured_args = {}

            original_run = fr.run_concerns

            def capture_args(args: argparse.Namespace) -> None:
                captured_args["max_workers"] = args.max_workers
                captured_args["timeout"] = args.timeout
                captured_args["retries"] = args.retries
                original_run(args)

            with patch.object(fr, "run_concerns", capture_args):
                fr.main()

        assert captured_args["max_workers"] == 8
        assert captured_args["timeout"] == 120
        assert captured_args["retries"] == 5


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
        _write_finding(repo, "bugs", "opus")
        _write_finding(repo, "security", "opus")

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=3,
            timeout=60,
            retries=0,
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
        _write_finding(repo, "bugs", "opus")
        _write_finding(repo, "bugs", "codex")

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=2,
            timeout=60,
            retries=0,
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
