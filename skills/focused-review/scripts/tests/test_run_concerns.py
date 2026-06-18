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
# Model resolution: enumeration, best-match, and family shorthands
# ---------------------------------------------------------------------------

# A realistic snapshot of the live ``copilot help config`` model list, used so
# tests never depend on a live CLI call.
AVAILABLE_MODELS = (
    "claude-sonnet-4.6",
    "claude-sonnet-4.5",
    "claude-haiku-4.5",
    "claude-fable-5",
    "claude-opus-4.8",
    "claude-opus-4.7",
    "claude-opus-4.6",
    "claude-opus-4.6-fast",
    "claude-opus-4.5",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.2-codex",
    "gpt-5.2",
    "gpt-5.4-mini",
    "gpt-5-mini",
    "gemini-3.1-pro-preview",
    "gemini-3.5-flash",
)

# Representative ``copilot help config`` output: the ``model`` block plus
# neighbouring settings (whose bullets must NOT be picked up).
HELP_CONFIG_SAMPLE = """Configuration Settings:

  `logLevel`: log level for CLI; defaults to "default".

  `model`: AI model to use for Copilot CLI; can be changed with /model command or --model flag option.
    - "claude-sonnet-4.6"
    - "claude-opus-4.8"
    - "gpt-5.5"
    - "gpt-5.3-codex"
    - "gemini-3.1-pro-preview"

  `contextTier`: context window tier for tiered-pricing models (e.g., "default" or "long_context").
    - Can also be set with --context flag (overrides persisted setting)

  `keepAlive`: keep-alive mode applied at CLI startup; defaults to `"off"`.
    - `"off"` (default): system sleeps normally.
    - `"on"`: keep-alive is enabled for the duration of the session.
"""


def _with_models(models: tuple[str, ...]) -> object:
    """Patch the available-model enumeration to a fixed list for a test."""
    return patch.object(fr, "_available_models", return_value=tuple(models))


@pytest.fixture(autouse=True)
def _stub_available_models():
    """Resolve against a fixed model list so tests never invoke the live CLI.

    Individual tests override this with :func:`_with_models` when they need a
    different available set (e.g. to exercise skip/fallback paths).
    """
    with patch.object(fr, "_available_models", return_value=AVAILABLE_MODELS):
        yield


class TestParseModelList:
    """Parsing the model block out of ``copilot help config`` output."""

    def test_extracts_model_block_slugs_in_order(self) -> None:
        assert fr._parse_model_list(HELP_CONFIG_SAMPLE) == (
            "claude-sonnet-4.6",
            "claude-opus-4.8",
            "gpt-5.5",
            "gpt-5.3-codex",
            "gemini-3.1-pro-preview",
        )

    def test_ignores_bullets_from_other_settings(self) -> None:
        slugs = fr._parse_model_list(HELP_CONFIG_SAMPLE)
        assert "off" not in slugs
        assert "on" not in slugs

    def test_missing_model_block_returns_empty(self) -> None:
        text = "  `logLevel`: log level for CLI; defaults to \"default\".\n"
        assert fr._parse_model_list(text) == ()

    def test_deduplicates_preserving_order(self) -> None:
        text = (
            "  `model`: AI model to use.\n"
            '    - "gpt-5.5"\n'
            '    - "gpt-5.5"\n'
            '    - "claude-opus-4.8"\n'
        )
        assert fr._parse_model_list(text) == ("gpt-5.5", "claude-opus-4.8")


class TestQueryAvailableModels:
    """Querying the CLI for the available-model list (fail-soft)."""

    def test_parses_stdout_on_success(self) -> None:
        mock = MagicMock(returncode=0, stdout=HELP_CONFIG_SAMPLE, stderr="")
        with patch("subprocess.run", return_value=mock):
            assert fr._query_available_models() == (
                "claude-sonnet-4.6",
                "claude-opus-4.8",
                "gpt-5.5",
                "gpt-5.3-codex",
                "gemini-3.1-pro-preview",
            )

    def test_nonzero_exit_returns_empty(self) -> None:
        mock = MagicMock(returncode=1, stdout="anything", stderr="boom")
        with patch("subprocess.run", return_value=mock):
            assert fr._query_available_models() == ()

    def test_cli_missing_returns_empty(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert fr._query_available_models() == ()

    def test_timeout_returns_empty(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=sp.TimeoutExpired(cmd="copilot", timeout=30),
        ):
            assert fr._query_available_models() == ()


class TestBestMatch:
    """Selecting the best concrete slug for a family from an available set."""

    def test_picks_highest_opus_version(self) -> None:
        avail = ("claude-opus-4.5", "claude-opus-4.8", "claude-opus-4.6")
        assert fr._best_match("opus", avail) == "claude-opus-4.8"

    def test_gpt_excludes_codex_and_mini(self) -> None:
        avail = ("gpt-5.3-codex", "gpt-5.4-mini", "gpt-5.4", "gpt-5.2")
        assert fr._best_match("gpt", avail) == "gpt-5.4"

    def test_codex_requires_codex_token_and_highest_version(self) -> None:
        avail = ("gpt-5.5", "gpt-5.2-codex", "gpt-5.3-codex")
        assert fr._best_match("codex", avail) == "gpt-5.3-codex"

    def test_gemini_prefers_pro_over_higher_versioned_flash(self) -> None:
        avail = ("gemini-3.5-flash", "gemini-3.1-pro-preview")
        assert fr._best_match("gemini", avail) == "gemini-3.1-pro-preview"

    def test_gemini_falls_back_to_highest_when_no_pro(self) -> None:
        avail = ("gemini-2.0-flash", "gemini-3.5-flash")
        assert fr._best_match("gemini", avail) == "gemini-3.5-flash"

    def test_plain_variant_wins_version_tie(self) -> None:
        avail = ("claude-opus-4.6-fast", "claude-opus-4.6")
        assert fr._best_match("opus", avail) == "claude-opus-4.6"

    def test_no_candidate_returns_none(self) -> None:
        assert fr._best_match("gemini", ("gpt-5.5", "claude-opus-4.8")) is None

    def test_unrelated_claude_family_not_matched(self) -> None:
        # ``claude-fable-5`` must not be selected for opus/sonnet/haiku.
        assert fr._best_match("opus", ("claude-fable-5",)) is None


class TestResolveModel:
    """End-to-end shorthand → available-slug resolution."""

    def test_opus_resolves_to_best_available(self) -> None:
        with _with_models(AVAILABLE_MODELS):
            assert fr._resolve_model("opus") == "claude-opus-4.8"

    def test_sonnet_resolves_to_best_available(self) -> None:
        with _with_models(AVAILABLE_MODELS):
            assert fr._resolve_model("sonnet") == "claude-sonnet-4.6"

    def test_haiku_resolves_to_best_available(self) -> None:
        with _with_models(AVAILABLE_MODELS):
            assert fr._resolve_model("haiku") == "claude-haiku-4.5"

    def test_gpt_resolves_to_best_available(self) -> None:
        with _with_models(AVAILABLE_MODELS):
            assert fr._resolve_model("gpt") == "gpt-5.5"

    def test_codex_resolves_to_best_available(self) -> None:
        with _with_models(AVAILABLE_MODELS):
            assert fr._resolve_model("codex") == "gpt-5.3-codex"

    def test_gemini_resolves_to_best_available(self) -> None:
        with _with_models(AVAILABLE_MODELS):
            assert fr._resolve_model("gemini") == "gemini-3.1-pro-preview"

    def test_case_insensitive(self) -> None:
        with _with_models(AVAILABLE_MODELS):
            assert fr._resolve_model("Opus") == "claude-opus-4.8"
            assert fr._resolve_model("OPUS") == "claude-opus-4.8"
            assert fr._resolve_model("Codex") == "gpt-5.3-codex"
            assert fr._resolve_model("GPT") == "gpt-5.5"

    def test_full_slug_passes_through(self) -> None:
        """Exact/internal slugs (not a family shorthand) pass through unchanged."""
        with _with_models(AVAILABLE_MODELS):
            assert fr._resolve_model("claude-opus-4.6-1m") == "claude-opus-4.6-1m"

    def test_arbitrary_model_passes_through(self) -> None:
        with _with_models(AVAILABLE_MODELS):
            assert fr._resolve_model("some-future-model-v2") == "some-future-model-v2"

    def test_inherit_passes_through(self) -> None:
        """'inherit' is not a family shorthand and should pass through."""
        with _with_models(AVAILABLE_MODELS):
            assert fr._resolve_model("inherit") == "inherit"

    def test_family_without_available_match_returns_none(self) -> None:
        """A known family with no live match resolves to None (caller skips)."""
        with _with_models(("gpt-5.5", "claude-opus-4.8")):
            assert fr._resolve_model("gemini") is None

    def test_falls_back_to_static_when_enumeration_unavailable(self) -> None:
        """When the live list can't be enumerated, use the offline fallback."""
        with _with_models(()):
            assert fr._resolve_model("opus") == "claude-opus-4.6-1m"
            assert fr._resolve_model("gpt") == "gpt-5.5"
            assert fr._resolve_model("gemini") == "gemini-3-pro-preview"


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
        # Shorthand "opus" resolves to the best available concrete slug.
        assert cmd[model_idx + 1] == "claude-opus-4.8"

    def test_unresolvable_family_is_skipped_without_invoking_cli(
        self, tmp_path: Path
    ) -> None:
        """A family with no available match is skipped, not run or failed."""
        repo = tmp_path / "repo"
        repo.mkdir()
        entries = [
            {
                "concern": "architecture",
                "model": "gemini",
                "priority": "standard",
                "prompt_path": ".agents/focused-review/prompts/architecture--gemini.md",
            }
        ]
        work_dir = _setup_work_dir(repo, entries)

        mock_run = MagicMock(return_value=_mock_subprocess_success())
        with _with_models(("gpt-5.5", "claude-opus-4.8")), patch(
            "subprocess.run", mock_run
        ):
            result = fr._run_single_concern(entries[0], repo, work_dir, retries=0)

        assert result["status"] == "skipped"
        assert "gemini" in result["error"]
        assert mock_run.call_count == 0

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
                inherit_model="claude-opus-4.6-1m",
            )

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-opus-4.6-1m"


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
            run_dir="",
        )

    def test_missing_dispatch_file_exits(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        # No dispatch file created

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=2,
            timeout=60,
            retries=0,
            run_dir="",
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
            run_dir="",
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
            run_dir="",
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
            run_dir="",
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
            run_dir="",
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
            run_dir="",
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

    def test_prewarm_skipped_when_no_family_shorthand(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With no family shorthand, model enumeration is never invoked."""
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
        _setup_work_dir(repo, entries)
        _write_finding(repo, "bugs", "inherit")

        args = argparse.Namespace(
            repo=str(repo), max_workers=1, timeout=60, retries=0, run_dir="",
        )
        with patch.object(fr, "_available_models", return_value=()) as mock_avail, patch(
            "subprocess.run", return_value=_mock_subprocess_success()
        ):
            fr.run_concerns(args)

        assert mock_avail.call_count == 0

    def test_prewarm_runs_when_family_shorthand_present(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A family shorthand triggers cache warm-up (enumeration is invoked)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        entries = [
            {
                "concern": "bugs",
                "model": "opus",
                "priority": "high",
                "prompt_path": ".agents/focused-review/prompts/bugs--opus.md",
            }
        ]
        _setup_work_dir(repo, entries)
        _write_finding(repo, "bugs", "opus")

        args = argparse.Namespace(
            repo=str(repo), max_workers=1, timeout=60, retries=0, run_dir="",
        )
        with patch.object(
            fr, "_available_models", return_value=AVAILABLE_MODELS
        ) as mock_avail, patch(
            "subprocess.run", return_value=_mock_subprocess_success()
        ):
            fr.run_concerns(args)

        assert mock_avail.call_count >= 1


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
            run_dir="",
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
            run_dir="",
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
# --run-dir argument
# ---------------------------------------------------------------------------


class TestRunDirArgument:

    def test_run_dir_argument_used(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When --run-dir is set, run_concerns reads dispatch from that directory."""
        repo = tmp_path / "repo"
        repo.mkdir()

        # Set up dispatch in a timestamped run directory
        run_dir = repo / ".agents" / "focused-review" / "20260402-103000"
        run_dir.mkdir(parents=True)

        entries = [
            {
                "concern": "bugs",
                "model": "opus",
                "priority": "high",
                "prompt_path": ".agents/focused-review/20260402-103000/prompts/bugs--opus.md",
            },
        ]
        dispatch_path = run_dir / "concern-dispatch.json"
        dispatch_path.write_text(json.dumps(entries), encoding="utf-8")

        # Write prompt file
        prompts_dir = run_dir / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = repo / entries[0]["prompt_path"]
        prompt_file.write_text("# Bugs\n## Role\nReview bugs.", encoding="utf-8")

        # Pre-create the finding file as if the agent wrote it
        finding_path = run_dir / "findings" / "concern--bugs--opus.md"
        finding_path.parent.mkdir(parents=True, exist_ok=True)
        finding_path.write_text("### [High] Bug found\nDetails.", encoding="utf-8")

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=1,
            timeout=60,
            retries=0,
            run_dir=".agents/focused-review/20260402-103000",
        )
        with patch("subprocess.run", return_value=_mock_subprocess_success()):
            fr.run_concerns(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["total"] == 1
        assert summary["success"] == 1

    def test_missing_run_dir_dispatch_exits(self, tmp_path: Path) -> None:
        """When --run-dir points to a dir without concern-dispatch.json, exit 1."""
        repo = tmp_path / "repo"
        repo.mkdir()
        run_dir = repo / ".agents" / "focused-review" / "20260402-103000"
        run_dir.mkdir(parents=True)
        # No dispatch file

        args = argparse.Namespace(
            repo=str(repo),
            max_workers=1,
            timeout=60,
            retries=0,
            run_dir=".agents/focused-review/20260402-103000",
        )
        with pytest.raises(SystemExit, match="1"):
            fr.run_concerns(args)
