"""Tests for the post-comments subcommand (GitHub platform)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importlib

fr = importlib.import_module("focused-review")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_comments(
    tmp_path: Path,
    *,
    platform: str = "github",
    owner: str = "test-owner",
    repo: str = "test-repo",
    pr_number: int = 42,
    review_body: str = "## Focused Review\nSummary",
    inline_comments: list[dict] | None = None,
    extra: dict | None = None,
) -> Path:
    """Write a comments.json file and return its path."""
    data: dict = {
        "platform": platform,
        "owner": owner,
        "repo": repo,
        "pr_number": pr_number,
        "review_body": review_body,
        "inline_comments": inline_comments or [],
    }
    if extra:
        data.update(extra)
    p = tmp_path / "comments.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _make_args(
    comments_path: str | Path,
    exclude: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        comments=str(comments_path),
        exclude=exclude,
    )


def _gh_api_success(review_url: str = "https://github.com/test-owner/test-repo/pull/42#pullrequestreview-123") -> MagicMock:
    """Return a mock subprocess result for a successful gh api call."""
    return MagicMock(
        stdout=json.dumps({"html_url": review_url}),
        returncode=0,
    )


SAMPLE_INLINE_COMMENTS = [
    {
        "id": 1,
        "path": "src/foo.cs",
        "line": 42,
        "body": "### 🔴 [High] Null reference risk\nDetails...",
    },
    {
        "id": 2,
        "path": "src/bar.cs",
        "line": 10,
        "body": "### 🟡 [Medium] Missing validation\nDetails...",
    },
    {
        "id": 3,
        "path": "src/baz.cs",
        "line": 99,
        "body": "### 🔵 [Low] Code style\nDetails...",
    },
]


# ---------------------------------------------------------------------------
# _check_gh_cli tests
# ---------------------------------------------------------------------------


class TestCheckGhCli:
    """Test gh CLI preflight checks."""

    @patch("subprocess.run")
    def test_passes_when_installed_and_authed(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        # Should not raise
        fr._check_gh_cli()
        assert mock_run.call_count == 2

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_exits_when_gh_not_installed(self, mock_run: MagicMock) -> None:
        with pytest.raises(SystemExit) as exc_info:
            fr._check_gh_cli()
        assert exc_info.value.code == 1

    @patch("subprocess.run")
    def test_exits_when_version_check_fails(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        with pytest.raises(SystemExit) as exc_info:
            fr._check_gh_cli()
        assert exc_info.value.code == 1

    @patch("subprocess.run")
    def test_exits_when_auth_fails(self, mock_run: MagicMock) -> None:
        # First call (gh --version) succeeds, second (gh auth status) fails
        mock_run.side_effect = [
            MagicMock(returncode=0),
            subprocess.CalledProcessError(1, "gh", stderr="not logged in"),
        ]
        with pytest.raises(SystemExit) as exc_info:
            fr._check_gh_cli()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# post_comments — happy path
# ---------------------------------------------------------------------------


class TestPostCommentsSuccess:
    """Test successful posting of review comments."""

    @patch("subprocess.run")
    def test_posts_review_with_inline_comments(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        comments_path = _make_comments(
            tmp_path, inline_comments=SAMPLE_INLINE_COMMENTS
        )
        # gh --version, gh auth status, gh api POST
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            _gh_api_success(),
        ]

        args = _make_args(comments_path)
        with patch("builtins.print") as mock_print:
            fr.post_comments(args)

        result = json.loads(mock_print.call_args[0][0])
        assert result["status"] == "posted"
        assert result["comments_posted"] == 3
        assert result["comments_excluded"] == 0
        assert "review_url" in result

    @patch("subprocess.run")
    def test_posts_review_body_only_no_inline(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        comments_path = _make_comments(tmp_path, inline_comments=[])
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            _gh_api_success(),
        ]

        args = _make_args(comments_path)
        with patch("builtins.print") as mock_print:
            fr.post_comments(args)

        result = json.loads(mock_print.call_args[0][0])
        assert result["status"] == "posted"
        assert result["comments_posted"] == 0

    @patch("subprocess.run")
    def test_payload_structure(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """Verify the payload sent to gh api has the correct structure."""
        comments_path = _make_comments(
            tmp_path,
            inline_comments=[SAMPLE_INLINE_COMMENTS[0]],
            review_body="Test body",
        )
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            _gh_api_success(),
        ]

        args = _make_args(comments_path)
        with patch("builtins.print"):
            fr.post_comments(args)

        # The third call is the gh api POST
        api_call = mock_run.call_args_list[2]
        payload = json.loads(api_call.kwargs.get("input", api_call[1].get("input", "")))

        assert payload["body"] == "Test body"
        assert payload["event"] == "COMMENT"
        assert len(payload["comments"]) == 1
        assert payload["comments"][0]["path"] == "src/foo.cs"
        assert payload["comments"][0]["line"] == 42
        assert payload["comments"][0]["side"] == "RIGHT"

    @patch("subprocess.run")
    def test_no_comments_key_when_empty(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """When there are no inline comments, the payload should not include 'comments' key."""
        comments_path = _make_comments(tmp_path, inline_comments=[])
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            _gh_api_success(),
        ]

        args = _make_args(comments_path)
        with patch("builtins.print"):
            fr.post_comments(args)

        api_call = mock_run.call_args_list[2]
        payload = json.loads(api_call.kwargs.get("input", api_call[1].get("input", "")))
        assert "comments" not in payload

    @patch("subprocess.run")
    def test_gh_api_endpoint(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """Verify the correct API endpoint is used."""
        comments_path = _make_comments(
            tmp_path, owner="my-org", repo="my-repo", pr_number=99
        )
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            _gh_api_success(),
        ]

        args = _make_args(comments_path)
        with patch("builtins.print"):
            fr.post_comments(args)

        api_call = mock_run.call_args_list[2]
        cmd = api_call[0][0]
        assert "/repos/my-org/my-repo/pulls/99/reviews" in cmd


# ---------------------------------------------------------------------------
# post_comments — exclude filtering
# ---------------------------------------------------------------------------


class TestPostCommentsExclude:
    """Test --exclude filtering of findings by ID."""

    @patch("subprocess.run")
    def test_exclude_single_finding(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        comments_path = _make_comments(
            tmp_path, inline_comments=SAMPLE_INLINE_COMMENTS
        )
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            _gh_api_success(),
        ]

        args = _make_args(comments_path, exclude="2")
        with patch("builtins.print") as mock_print:
            fr.post_comments(args)

        result = json.loads(mock_print.call_args[0][0])
        assert result["comments_posted"] == 2
        assert result["comments_excluded"] == 1

        # Verify excluded comment not in payload
        api_call = mock_run.call_args_list[2]
        payload = json.loads(api_call.kwargs.get("input", api_call[1].get("input", "")))
        paths = [c["path"] for c in payload["comments"]]
        assert "src/bar.cs" not in paths
        assert "src/foo.cs" in paths
        assert "src/baz.cs" in paths

    @patch("subprocess.run")
    def test_exclude_multiple_findings(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        comments_path = _make_comments(
            tmp_path, inline_comments=SAMPLE_INLINE_COMMENTS
        )
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            _gh_api_success(),
        ]

        args = _make_args(comments_path, exclude="1,3")
        with patch("builtins.print") as mock_print:
            fr.post_comments(args)

        result = json.loads(mock_print.call_args[0][0])
        assert result["comments_posted"] == 1
        assert result["comments_excluded"] == 2

    @patch("subprocess.run")
    def test_exclude_all_findings(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        comments_path = _make_comments(
            tmp_path, inline_comments=SAMPLE_INLINE_COMMENTS
        )
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            _gh_api_success(),
        ]

        args = _make_args(comments_path, exclude="1,2,3")
        with patch("builtins.print") as mock_print:
            fr.post_comments(args)

        result = json.loads(mock_print.call_args[0][0])
        assert result["comments_posted"] == 0
        assert result["comments_excluded"] == 3

    @patch("subprocess.run")
    def test_exclude_nonexistent_id_ignored(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        comments_path = _make_comments(
            tmp_path, inline_comments=SAMPLE_INLINE_COMMENTS
        )
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            _gh_api_success(),
        ]

        args = _make_args(comments_path, exclude="99")
        with patch("builtins.print") as mock_print:
            fr.post_comments(args)

        result = json.loads(mock_print.call_args[0][0])
        assert result["comments_posted"] == 3
        # The exclude set contains 1 item even though it didn't match anything
        assert result["comments_excluded"] == 1

    @patch("subprocess.run")
    def test_exclude_with_whitespace(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        comments_path = _make_comments(
            tmp_path, inline_comments=SAMPLE_INLINE_COMMENTS
        )
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            _gh_api_success(),
        ]

        args = _make_args(comments_path, exclude=" 1 , 2 ")
        with patch("builtins.print") as mock_print:
            fr.post_comments(args)

        result = json.loads(mock_print.call_args[0][0])
        assert result["comments_posted"] == 1


# ---------------------------------------------------------------------------
# post_comments — error handling
# ---------------------------------------------------------------------------


class TestPostCommentsErrors:
    """Test error scenarios."""

    def test_file_not_found(self, tmp_path: Path) -> None:
        args = _make_args(tmp_path / "nonexistent.json")
        with pytest.raises(SystemExit) as exc_info:
            fr.post_comments(args)
        assert exc_info.value.code == 1

    def test_invalid_json(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not json {{{", encoding="utf-8")
        args = _make_args(bad)
        with pytest.raises(SystemExit) as exc_info:
            fr.post_comments(args)
        assert exc_info.value.code == 1

    def test_wrong_platform(self, tmp_path: Path) -> None:
        comments_path = _make_comments(tmp_path, platform="ado")
        args = _make_args(comments_path)
        with pytest.raises(SystemExit) as exc_info:
            fr.post_comments(args)
        assert exc_info.value.code == 1

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_gh_cli_not_installed(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        comments_path = _make_comments(tmp_path)
        args = _make_args(comments_path)
        with pytest.raises(SystemExit) as exc_info:
            fr.post_comments(args)
        assert exc_info.value.code == 1

    @patch("subprocess.run")
    def test_gh_auth_not_authenticated(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=0),  # gh --version OK
            subprocess.CalledProcessError(1, "gh", stderr="not logged in"),
        ]
        comments_path = _make_comments(tmp_path)
        args = _make_args(comments_path)
        with pytest.raises(SystemExit) as exc_info:
            fr.post_comments(args)
        assert exc_info.value.code == 1

    @patch("subprocess.run")
    def test_api_error_404(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """API returns 404 (PR not found)."""
        comments_path = _make_comments(tmp_path)
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            subprocess.CalledProcessError(
                1, "gh",
                output='{"message": "Not Found"}',
                stderr="",
            ),
        ]
        args = _make_args(comments_path)
        with patch("builtins.print") as mock_print:
            with pytest.raises(SystemExit) as exc_info:
                fr.post_comments(args)

        assert exc_info.value.code == 1
        result = json.loads(mock_print.call_args[0][0])
        assert result["status"] == "error"
        assert "Not Found" in result["error"]

    @patch("subprocess.run")
    def test_api_error_422(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """API returns 422 (line not in diff)."""
        comments_path = _make_comments(
            tmp_path, inline_comments=[SAMPLE_INLINE_COMMENTS[0]]
        )
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            subprocess.CalledProcessError(
                1, "gh",
                output='{"message": "Validation Failed", "errors": [{"message": "pull_request_review_thread.line must be part of the diff"}]}',
                stderr="",
            ),
        ]
        args = _make_args(comments_path)
        with patch("builtins.print") as mock_print:
            with pytest.raises(SystemExit) as exc_info:
                fr.post_comments(args)

        assert exc_info.value.code == 1
        result = json.loads(mock_print.call_args[0][0])
        assert result["status"] == "error"
        assert result["comments_attempted"] == 1

    @patch("subprocess.run")
    def test_api_error_non_json_stderr(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """API error with non-JSON output."""
        comments_path = _make_comments(tmp_path)
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            subprocess.CalledProcessError(
                1, "gh",
                output="",
                stderr="connection refused",
            ),
        ]
        args = _make_args(comments_path)
        with patch("builtins.print") as mock_print:
            with pytest.raises(SystemExit) as exc_info:
                fr.post_comments(args)

        assert exc_info.value.code == 1
        result = json.loads(mock_print.call_args[0][0])
        assert result["status"] == "error"
        assert result["error"] == "connection refused"


# ---------------------------------------------------------------------------
# CLI integration (argparse wiring)
# ---------------------------------------------------------------------------


class TestPostCommentsArgparseWiring:
    """Verify the post-comments subcommand is registered and parses correctly."""

    def test_post_comments_registered(self) -> None:
        """post-comments subcommand is wired to post_comments."""
        with patch(
            "sys.argv",
            ["focused-review", "post-comments", "--comments", "c.json"],
        ):
            with patch.object(fr, "post_comments") as mock_fn:
                mock_fn.return_value = None
                fr.main()
                mock_fn.assert_called_once()

    def test_post_comments_with_exclude(self) -> None:
        """post-comments parses --exclude correctly."""
        with patch(
            "sys.argv",
            [
                "focused-review",
                "post-comments",
                "--comments",
                "c.json",
                "--exclude",
                "1,2,3",
            ],
        ):
            with patch.object(fr, "post_comments") as mock_fn:
                mock_fn.return_value = None
                fr.main()
                called_args = mock_fn.call_args[0][0]
                assert called_args.exclude == "1,2,3"
                assert called_args.comments == "c.json"

    def test_post_comments_exclude_default_none(self) -> None:
        """--exclude defaults to None when not provided."""
        with patch(
            "sys.argv",
            ["focused-review", "post-comments", "--comments", "c.json"],
        ):
            with patch.object(fr, "post_comments") as mock_fn:
                mock_fn.return_value = None
                fr.main()
                called_args = mock_fn.call_args[0][0]
                assert called_args.exclude is None
