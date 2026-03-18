"""Tests for the post-comments subcommand (ADO platform)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importlib

fr = importlib.import_module("focused-review")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_ado_comments(
    tmp_path: Path,
    *,
    org: str = "test-org",
    project: str = "test-project",
    repo: str = "test-repo",
    pr_number: int = 42,
    review_body: str = "## Focused Review\nSummary",
    inline_comments: list[dict] | None = None,
) -> Path:
    """Write an ADO comments.json file and return its path."""
    data: dict = {
        "platform": "ado",
        "org": org,
        "project": project,
        "repo": repo,
        "pr_number": pr_number,
        "review_body": review_body,
        "inline_comments": inline_comments or [],
    }
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


def _az_preflight_success() -> list[MagicMock]:
    """Return mock subprocess results for successful az preflight checks + token."""
    return [
        MagicMock(returncode=0),  # az --version
        MagicMock(returncode=0),  # az account show
        MagicMock(returncode=0, stdout="fake-token-abc123\n"),  # az account get-access-token
    ]


def _mock_urlopen_response(data: dict | None = None) -> MagicMock:
    """Create a mock context manager for urllib.request.urlopen."""
    response_data = json.dumps(data or {"id": 1}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# _check_az_cli tests
# ---------------------------------------------------------------------------


class TestCheckAzCli:
    """Test az CLI preflight checks."""

    @patch("subprocess.run")
    def test_passes_when_installed_and_authed(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        fr._check_az_cli()
        assert mock_run.call_count == 2

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_exits_when_az_not_installed(self, mock_run: MagicMock) -> None:
        with pytest.raises(SystemExit) as exc_info:
            fr._check_az_cli()
        assert exc_info.value.code == 1

    @patch("subprocess.run")
    def test_exits_when_version_check_fails(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(1, "az")
        with pytest.raises(SystemExit) as exc_info:
            fr._check_az_cli()
        assert exc_info.value.code == 1

    @patch("subprocess.run")
    def test_exits_when_not_authenticated(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=0),  # az --version OK
            subprocess.CalledProcessError(1, "az", stderr="not logged in"),
        ]
        with pytest.raises(SystemExit) as exc_info:
            fr._check_az_cli()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _get_ado_token tests
# ---------------------------------------------------------------------------


class TestGetAdoToken:
    """Test ADO token retrieval."""

    @patch("subprocess.run")
    def test_returns_token(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="my-access-token-123\n"
        )
        token = fr._get_ado_token()
        assert token == "my-access-token-123"

        cmd = mock_run.call_args[0][0]
        assert "get-access-token" in cmd
        assert "499b84ac-1321-427f-aa17-267ca6975798" in cmd

    @patch("subprocess.run")
    def test_exits_on_failure(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "az", stderr="token error"
        )
        with pytest.raises(SystemExit) as exc_info:
            fr._get_ado_token()
        assert exc_info.value.code == 1

    @patch("subprocess.run")
    def test_exits_on_empty_token(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="\n")
        with pytest.raises(SystemExit) as exc_info:
            fr._get_ado_token()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _post_ado_thread tests
# ---------------------------------------------------------------------------


class TestPostAdoThread:
    """Test posting a single thread to ADO."""

    @patch("urllib.request.urlopen")
    def test_posts_thread_and_returns_response(
        self, mock_urlopen: MagicMock
    ) -> None:
        expected = {"id": 42, "status": "active"}
        mock_urlopen.return_value = _mock_urlopen_response(expected)

        thread_body = {
            "comments": [{"content": "test", "commentType": 1}],
            "status": 1,
        }
        result = fr._post_ado_thread(
            "token", "org", "proj", "repo", 1, thread_body
        )

        assert result == expected
        req = mock_urlopen.call_args[0][0]
        assert "dev.azure.com/org/proj/_apis/git/repositories/repo" in req.full_url
        assert "pullRequests/1/threads" in req.full_url
        assert req.get_header("Authorization") == "Bearer token"
        assert req.get_header("Content-type") == "application/json"

    @patch("urllib.request.urlopen")
    def test_raises_on_http_error(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://example.com", 400, "Bad Request", {}, BytesIO(b"bad")
        )
        with pytest.raises(urllib.error.HTTPError):
            fr._post_ado_thread("token", "org", "proj", "repo", 1, {})


# ---------------------------------------------------------------------------
# ADO post_comments — happy path
# ---------------------------------------------------------------------------


class TestPostCommentsAdoSuccess:
    """Test successful posting of ADO review comments."""

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_posts_review_body_and_inline_comments(
        self, mock_run: MagicMock, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        comments_path = _make_ado_comments(
            tmp_path, inline_comments=SAMPLE_INLINE_COMMENTS
        )
        mock_run.side_effect = _az_preflight_success()
        mock_urlopen.return_value = _mock_urlopen_response()

        args = _make_args(comments_path)
        with patch("builtins.print") as mock_print:
            fr.post_comments(args)

        result = json.loads(mock_print.call_args[0][0])
        assert result["status"] == "posted"
        # 1 review body + 3 inline = 4 total
        assert result["comments_posted"] == 4
        assert result["comments_failed"] == 0
        assert result["comments_excluded"] == 0
        # urlopen called 4 times (1 review body + 3 inline)
        assert mock_urlopen.call_count == 4

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_posts_inline_only_when_no_review_body(
        self, mock_run: MagicMock, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        comments_path = _make_ado_comments(
            tmp_path,
            review_body="",
            inline_comments=SAMPLE_INLINE_COMMENTS,
        )
        mock_run.side_effect = _az_preflight_success()
        mock_urlopen.return_value = _mock_urlopen_response()

        args = _make_args(comments_path)
        with patch("builtins.print") as mock_print:
            fr.post_comments(args)

        result = json.loads(mock_print.call_args[0][0])
        assert result["status"] == "posted"
        assert result["comments_posted"] == 3
        assert mock_urlopen.call_count == 3

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_posts_review_body_only_no_inline(
        self, mock_run: MagicMock, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        comments_path = _make_ado_comments(tmp_path, inline_comments=[])
        mock_run.side_effect = _az_preflight_success()
        mock_urlopen.return_value = _mock_urlopen_response()

        args = _make_args(comments_path)
        with patch("builtins.print") as mock_print:
            fr.post_comments(args)

        result = json.loads(mock_print.call_args[0][0])
        assert result["status"] == "posted"
        assert result["comments_posted"] == 1
        assert mock_urlopen.call_count == 1

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_thread_context_structure(
        self, mock_run: MagicMock, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """Verify the threadContext in ADO inline comment threads."""
        comments_path = _make_ado_comments(
            tmp_path,
            review_body="",
            inline_comments=[SAMPLE_INLINE_COMMENTS[0]],
        )
        mock_run.side_effect = _az_preflight_success()
        mock_urlopen.return_value = _mock_urlopen_response()

        args = _make_args(comments_path)
        with patch("builtins.print"):
            fr.post_comments(args)

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))

        assert "threadContext" in body
        tc = body["threadContext"]
        assert tc["filePath"] == "src/foo.cs"
        assert tc["rightFileStart"] == {"line": 42, "offset": 1}
        assert tc["rightFileEnd"] == {"line": 42, "offset": 1}
        assert body["status"] == 1
        assert body["comments"][0]["commentType"] == 1

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_overall_thread_has_no_thread_context(
        self, mock_run: MagicMock, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """Verify the overall review body thread has no threadContext."""
        comments_path = _make_ado_comments(
            tmp_path,
            review_body="Summary body",
            inline_comments=[],
        )
        mock_run.side_effect = _az_preflight_success()
        mock_urlopen.return_value = _mock_urlopen_response()

        args = _make_args(comments_path)
        with patch("builtins.print"):
            fr.post_comments(args)

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))

        assert "threadContext" not in body
        assert body["comments"][0]["content"] == "Summary body"

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_ado_api_endpoint_format(
        self, mock_run: MagicMock, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """Verify the correct ADO API endpoint is used."""
        comments_path = _make_ado_comments(
            tmp_path,
            org="my-org",
            project="my-project",
            repo="my-repo",
            pr_number=99,
            inline_comments=[],
        )
        mock_run.side_effect = _az_preflight_success()
        mock_urlopen.return_value = _mock_urlopen_response()

        args = _make_args(comments_path)
        with patch("builtins.print"):
            fr.post_comments(args)

        req = mock_urlopen.call_args[0][0]
        assert "dev.azure.com/my-org/my-project/_apis/git/repositories/my-repo" in req.full_url
        assert "pullRequests/99/threads" in req.full_url
        assert "api-version=7.0" in req.full_url


# ---------------------------------------------------------------------------
# ADO post_comments — exclude filtering
# ---------------------------------------------------------------------------


class TestPostCommentsAdoExclude:
    """Test --exclude filtering for ADO."""

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_exclude_single_finding(
        self, mock_run: MagicMock, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        comments_path = _make_ado_comments(
            tmp_path, inline_comments=SAMPLE_INLINE_COMMENTS
        )
        mock_run.side_effect = _az_preflight_success()
        mock_urlopen.return_value = _mock_urlopen_response()

        args = _make_args(comments_path, exclude="2")
        with patch("builtins.print") as mock_print:
            fr.post_comments(args)

        result = json.loads(mock_print.call_args[0][0])
        # 1 review body + 2 remaining inline = 3
        assert result["comments_posted"] == 3
        assert result["comments_excluded"] == 1

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_exclude_all_findings(
        self, mock_run: MagicMock, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        comments_path = _make_ado_comments(
            tmp_path, inline_comments=SAMPLE_INLINE_COMMENTS
        )
        mock_run.side_effect = _az_preflight_success()
        mock_urlopen.return_value = _mock_urlopen_response()

        args = _make_args(comments_path, exclude="1,2,3")
        with patch("builtins.print") as mock_print:
            fr.post_comments(args)

        result = json.loads(mock_print.call_args[0][0])
        # Only review body posted
        assert result["comments_posted"] == 1
        assert result["comments_excluded"] == 3


# ---------------------------------------------------------------------------
# ADO post_comments — sequential posting and partial failure
# ---------------------------------------------------------------------------


class TestPostCommentsAdoPartialFailure:
    """Test sequential posting with partial failures."""

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_partial_failure_continues_posting(
        self, mock_run: MagicMock, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """When one inline thread fails, the rest should still be posted."""
        comments_path = _make_ado_comments(
            tmp_path, inline_comments=SAMPLE_INLINE_COMMENTS
        )
        mock_run.side_effect = _az_preflight_success()

        # Review body succeeds, 1st inline fails, 2nd+3rd succeed
        mock_urlopen.side_effect = [
            _mock_urlopen_response(),  # review body
            urllib.error.HTTPError(
                "http://example.com", 400, "Bad Request", {},
                BytesIO(b'{"message": "line not in diff"}'),
            ),
            _mock_urlopen_response(),  # 2nd inline OK
            _mock_urlopen_response(),  # 3rd inline OK
        ]

        args = _make_args(comments_path)
        with patch("builtins.print") as mock_print:
            fr.post_comments(args)

        result = json.loads(mock_print.call_args[0][0])
        assert result["status"] == "partial"
        assert result["comments_posted"] == 3
        assert result["comments_failed"] == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["path"] == "src/foo.cs"

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_all_inline_fail_review_body_succeeds(
        self, mock_run: MagicMock, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """Review body posts but all inline comments fail."""
        comments_path = _make_ado_comments(
            tmp_path,
            inline_comments=SAMPLE_INLINE_COMMENTS[:1],
        )
        mock_run.side_effect = _az_preflight_success()
        mock_urlopen.side_effect = [
            _mock_urlopen_response(),  # review body succeeds
            urllib.error.HTTPError(
                "http://example.com", 500, "Server Error", {},
                BytesIO(b"internal error"),
            ),
        ]

        args = _make_args(comments_path)
        with patch("builtins.print") as mock_print:
            fr.post_comments(args)

        result = json.loads(mock_print.call_args[0][0])
        assert result["status"] == "partial"
        assert result["comments_posted"] == 1
        assert result["comments_failed"] == 1

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_all_fail_exits_with_error(
        self, mock_run: MagicMock, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """When everything fails, exit with code 1."""
        comments_path = _make_ado_comments(
            tmp_path,
            inline_comments=SAMPLE_INLINE_COMMENTS[:1],
        )
        mock_run.side_effect = _az_preflight_success()
        mock_urlopen.side_effect = [
            urllib.error.HTTPError(
                "http://example.com", 500, "Error", {},
                BytesIO(b"error"),
            ),
            urllib.error.HTTPError(
                "http://example.com", 500, "Error", {},
                BytesIO(b"error"),
            ),
        ]

        args = _make_args(comments_path)
        with patch("builtins.print") as mock_print:
            with pytest.raises(SystemExit) as exc_info:
                fr.post_comments(args)

        assert exc_info.value.code == 1
        result = json.loads(mock_print.call_args[0][0])
        assert result["status"] == "error"
        assert result["comments_posted"] == 0
        assert result["comments_failed"] == 2

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_url_error_handled(
        self, mock_run: MagicMock, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """URLError (network) is handled gracefully."""
        comments_path = _make_ado_comments(
            tmp_path,
            review_body="",
            inline_comments=SAMPLE_INLINE_COMMENTS[:1],
        )
        mock_run.side_effect = _az_preflight_success()
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")

        args = _make_args(comments_path)
        with patch("builtins.print") as mock_print:
            with pytest.raises(SystemExit) as exc_info:
                fr.post_comments(args)

        assert exc_info.value.code == 1
        result = json.loads(mock_print.call_args[0][0])
        assert result["comments_failed"] == 1
        assert "connection refused" in result["errors"][0]["error"]


# ---------------------------------------------------------------------------
# ADO post_comments — error handling
# ---------------------------------------------------------------------------


class TestPostCommentsAdoErrors:
    """Test ADO-specific error scenarios."""

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_az_cli_not_installed(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        comments_path = _make_ado_comments(tmp_path)
        args = _make_args(comments_path)
        with pytest.raises(SystemExit) as exc_info:
            fr.post_comments(args)
        assert exc_info.value.code == 1

    @patch("subprocess.run")
    def test_az_not_authenticated(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=0),  # az --version OK
            subprocess.CalledProcessError(1, "az", stderr="not logged in"),
        ]
        comments_path = _make_ado_comments(tmp_path)
        args = _make_args(comments_path)
        with pytest.raises(SystemExit) as exc_info:
            fr.post_comments(args)
        assert exc_info.value.code == 1

    @patch("subprocess.run")
    def test_token_retrieval_fails(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=0),  # az --version
            MagicMock(returncode=0),  # az account show
            subprocess.CalledProcessError(1, "az", stderr="token error"),
        ]
        comments_path = _make_ado_comments(tmp_path)
        args = _make_args(comments_path)
        with pytest.raises(SystemExit) as exc_info:
            fr.post_comments(args)
        assert exc_info.value.code == 1

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_review_body_fails_inline_succeeds(
        self, mock_run: MagicMock, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """Overall review body fails but inline comments still post."""
        comments_path = _make_ado_comments(
            tmp_path, inline_comments=SAMPLE_INLINE_COMMENTS[:1]
        )
        mock_run.side_effect = _az_preflight_success()
        mock_urlopen.side_effect = [
            urllib.error.HTTPError(
                "http://example.com", 403, "Forbidden", {},
                BytesIO(b"access denied"),
            ),
            _mock_urlopen_response(),  # inline succeeds
        ]

        args = _make_args(comments_path)
        with patch("builtins.print") as mock_print:
            fr.post_comments(args)

        result = json.loads(mock_print.call_args[0][0])
        assert result["status"] == "partial"
        assert result["comments_posted"] == 1
        assert result["comments_failed"] == 1
        assert result["errors"][0]["type"] == "review_body"


# ---------------------------------------------------------------------------
# ADO post_comments — sequential posting order
# ---------------------------------------------------------------------------


class TestPostCommentsAdoSequentialOrder:
    """Verify threads are posted sequentially in order."""

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_posts_in_order_review_body_first(
        self, mock_run: MagicMock, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """Review body is posted first, then inline comments in order."""
        comments_path = _make_ado_comments(
            tmp_path,
            review_body="Overall summary",
            inline_comments=SAMPLE_INLINE_COMMENTS[:2],
        )
        mock_run.side_effect = _az_preflight_success()
        mock_urlopen.return_value = _mock_urlopen_response()

        args = _make_args(comments_path)
        with patch("builtins.print"):
            fr.post_comments(args)

        assert mock_urlopen.call_count == 3
        calls = mock_urlopen.call_args_list

        # First call: review body (no threadContext)
        body0 = json.loads(calls[0][0][0].data.decode("utf-8"))
        assert "threadContext" not in body0
        assert body0["comments"][0]["content"] == "Overall summary"

        # Second call: first inline comment
        body1 = json.loads(calls[1][0][0].data.decode("utf-8"))
        assert body1["threadContext"]["filePath"] == "src/foo.cs"

        # Third call: second inline comment
        body2 = json.loads(calls[2][0][0].data.decode("utf-8"))
        assert body2["threadContext"]["filePath"] == "src/bar.cs"
