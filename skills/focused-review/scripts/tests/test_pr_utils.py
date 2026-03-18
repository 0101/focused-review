"""Tests for parse-pr-url and get-pr-user subcommands."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importlib

fr = importlib.import_module("focused-review")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_url(url: str) -> dict:
    """Run parse_pr_url and return the parsed JSON dict."""
    args = argparse.Namespace(url=url)
    with patch("builtins.print") as mock_print:
        fr.parse_pr_url(args)
    return json.loads(mock_print.call_args[0][0])


# ---------------------------------------------------------------------------
# parse-pr-url tests
# ---------------------------------------------------------------------------


class TestParsePrUrlGitHub:
    """Test GitHub PR URL parsing."""

    def test_basic_pr_url(self) -> None:
        result = _parse_url("https://github.com/owner/repo/pull/42")
        assert result == {
            "platform": "github",
            "owner": "owner",
            "repo": "repo",
            "pr_number": 42,
        }

    @pytest.mark.parametrize("suffix", ["/files", "/commits", "/checks"])
    def test_pr_url_with_trailing_path(self, suffix: str) -> None:
        result = _parse_url(f"https://github.com/my-org/my-repo/pull/123{suffix}")
        assert result["platform"] == "github"
        assert result["owner"] == "my-org"
        assert result["repo"] == "my-repo"
        assert result["pr_number"] == 123

    def test_owner_with_hyphens(self) -> None:
        result = _parse_url("https://github.com/my-cool-org/repo/pull/1")
        assert result["owner"] == "my-cool-org"

    def test_repo_with_dots_and_hyphens(self) -> None:
        result = _parse_url("https://github.com/owner/my-repo.js/pull/5")
        assert result["repo"] == "my-repo.js"

    def test_large_pr_number(self) -> None:
        result = _parse_url("https://github.com/owner/repo/pull/99999")
        assert result["pr_number"] == 99999

    def test_whitespace_stripped(self) -> None:
        result = _parse_url("  https://github.com/owner/repo/pull/42  ")
        assert result["pr_number"] == 42


class TestParsePrUrlAdo:
    """Test Azure DevOps PR URL parsing."""

    def test_dev_azure_com_url(self) -> None:
        result = _parse_url(
            "https://dev.azure.com/myorg/myproject/_git/myrepo/pullrequest/42"
        )
        assert result == {
            "platform": "ado",
            "org": "myorg",
            "project": "myproject",
            "repo": "myrepo",
            "pr_number": 42,
        }

    def test_visualstudio_com_url(self) -> None:
        result = _parse_url(
            "https://myorg.visualstudio.com/myproject/_git/myrepo/pullrequest/123"
        )
        assert result == {
            "platform": "ado",
            "org": "myorg",
            "project": "myproject",
            "repo": "myrepo",
            "pr_number": 123,
        }

    def test_ado_org_with_hyphens(self) -> None:
        result = _parse_url(
            "https://dev.azure.com/my-org/my-project/_git/my-repo/pullrequest/7"
        )
        assert result["org"] == "my-org"
        assert result["project"] == "my-project"
        assert result["repo"] == "my-repo"

    def test_ado_large_pr_number(self) -> None:
        result = _parse_url(
            "https://dev.azure.com/org/proj/_git/repo/pullrequest/88888"
        )
        assert result["pr_number"] == 88888


class TestParsePrUrlInvalid:
    """Test that invalid URLs produce errors."""

    def test_random_url(self) -> None:
        args = argparse.Namespace(url="https://example.com/not-a-pr")
        with pytest.raises(SystemExit) as exc_info:
            fr.parse_pr_url(args)
        assert exc_info.value.code == 1

    def test_github_non_pr_url(self) -> None:
        args = argparse.Namespace(url="https://github.com/owner/repo/issues/42")
        with pytest.raises(SystemExit) as exc_info:
            fr.parse_pr_url(args)
        assert exc_info.value.code == 1

    def test_empty_url(self) -> None:
        args = argparse.Namespace(url="")
        with pytest.raises(SystemExit) as exc_info:
            fr.parse_pr_url(args)
        assert exc_info.value.code == 1

    def test_ado_wrong_path(self) -> None:
        args = argparse.Namespace(
            url="https://dev.azure.com/org/proj/_git/repo/commits"
        )
        with pytest.raises(SystemExit) as exc_info:
            fr.parse_pr_url(args)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# get-pr-user tests
# ---------------------------------------------------------------------------


class TestGetPrUserGitHub:
    """Test GitHub user identity retrieval."""

    @patch("subprocess.run")
    def test_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            stdout='{"login": "octocat", "name": "The Octocat"}\n',
            returncode=0,
        )
        args = argparse.Namespace(platform="github")
        with patch("builtins.print") as mock_print:
            fr.get_pr_user(args)
        result = json.loads(mock_print.call_args[0][0])
        assert result == {
            "username": "octocat",
            "display_name": "The Octocat",
        }
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_no_display_name_falls_back_to_username(
        self, mock_run: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(
            stdout='{"login": "octocat", "name": null}\n',
            returncode=0,
        )
        args = argparse.Namespace(platform="github")
        with patch("builtins.print") as mock_print:
            fr.get_pr_user(args)
        result = json.loads(mock_print.call_args[0][0])
        assert result["display_name"] == "octocat"

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_gh_not_installed(self, mock_run: MagicMock) -> None:
        args = argparse.Namespace(platform="github")
        with pytest.raises(SystemExit) as exc_info:
            fr.get_pr_user(args)
        assert exc_info.value.code == 1

    @patch(
        "subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "gh", stderr="auth required"),
    )
    def test_gh_auth_failure(self, mock_run: MagicMock) -> None:
        args = argparse.Namespace(platform="github")
        with pytest.raises(SystemExit) as exc_info:
            fr.get_pr_user(args)
        assert exc_info.value.code == 1


class TestGetPrUserAdo:
    """Test ADO user identity retrieval."""

    @patch("subprocess.run")
    def test_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(stdout="John Doe\n", returncode=0)
        args = argparse.Namespace(platform="ado")
        with patch("builtins.print") as mock_print:
            fr.get_pr_user(args)
        result = json.loads(mock_print.call_args[0][0])
        assert result == {
            "username": "John Doe",
            "display_name": "John Doe",
        }

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_az_not_installed(self, mock_run: MagicMock) -> None:
        args = argparse.Namespace(platform="ado")
        with pytest.raises(SystemExit) as exc_info:
            fr.get_pr_user(args)
        assert exc_info.value.code == 1

    @patch(
        "subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "az", stderr="not logged in"),
    )
    def test_az_auth_failure(self, mock_run: MagicMock) -> None:
        args = argparse.Namespace(platform="ado")
        with pytest.raises(SystemExit) as exc_info:
            fr.get_pr_user(args)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# CLI integration (argparse wiring)
# ---------------------------------------------------------------------------


class TestArgparseWiring:
    """Verify subcommands are registered and parse correctly."""

    def test_parse_pr_url_registered(self) -> None:
        """parse-pr-url subcommand is wired to parse_pr_url."""
        with patch("sys.argv", ["focused-review", "parse-pr-url", "--url", "https://github.com/o/r/pull/1"]):
            with patch.object(fr, "parse_pr_url") as mock_fn:
                mock_fn.return_value = None
                fr.main()
                mock_fn.assert_called_once()

    def test_get_pr_user_registered(self) -> None:
        """get-pr-user subcommand is wired to get_pr_user."""
        with patch("sys.argv", ["focused-review", "get-pr-user", "--platform", "github"]):
            with patch.object(fr, "get_pr_user") as mock_fn:
                mock_fn.return_value = None
                fr.main()
                mock_fn.assert_called_once()
