"""Tests for --rules-dir resolution: config file scanning and CLI override."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tests import create_file

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importlib

fr = importlib.import_module("focused-review")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rule(name: str) -> str:
    return f"---\nautofix: false\nmodel: haiku\n---\n# {name}\n## Rule\nCheck."


def _mock_git_results(diff_text: str, changed_files: list[str]) -> list[MagicMock]:
    diff_mock = MagicMock()
    diff_mock.returncode = 0
    diff_mock.stdout = diff_text

    names_mock = MagicMock()
    names_mock.returncode = 0
    names_mock.stdout = "\n".join(changed_files)

    return [diff_mock, names_mock]


def _make_diff(filename: str, num_lines: int = 10) -> str:
    parts = [
        f"diff --git a/{filename} b/{filename}",
        f"--- a/{filename}",
        f"+++ b/{filename}",
        f"@@ -1,0 +1,{num_lines} @@",
    ]
    for i in range(num_lines):
        parts.append(f"+line {i + 1}")
    return "\n".join(parts)


def _setup_repo(tmp_path: Path, rules_dir_name: str = "review") -> Path:
    """Create a repo with one rule in the given rules directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    rules_dir = repo / rules_dir_name
    rules_dir.mkdir(parents=True)
    create_file(rules_dir, "example.md", _make_rule("Example Rule"))
    return repo


def _write_config(repo: Path, rel_path: str, rules_dir: str) -> Path:
    """Write a focused-review.json config file at the given relative path."""
    config_path = repo / rel_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"rules_dir": rules_dir}), encoding="utf-8")
    return config_path


def _write_config_with_sources(
    repo: Path, rel_path: str, rules_dir: str, sources: list[str]
) -> Path:
    """Write a focused-review.json config file with both rules_dir and sources."""
    config_path = repo / rel_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"rules_dir": rules_dir, "sources": sources}), encoding="utf-8"
    )
    return config_path


# ---------------------------------------------------------------------------
# Config file resolution for --rules-dir
# ---------------------------------------------------------------------------


class TestConfigFileResolution:
    """Config file scanning sets the default for --rules-dir."""

    def test_config_in_claude_dir_overrides_default(self, tmp_path: Path) -> None:
        """A config file in .claude/ overrides the default review/ directory."""
        repo = _setup_repo(tmp_path, "custom-rules")
        _write_config(repo, ".claude/focused-review.json", "custom-rules/")

        result = fr._resolve_rules_dir(str(repo))
        assert result == "custom-rules/"

    def test_config_at_repo_root_works(self, tmp_path: Path) -> None:
        """.focused-review.json at repo root is picked up."""
        repo = _setup_repo(tmp_path, "root-rules")
        _write_config(repo, "focused-review.json", "root-rules/")

        result = fr._resolve_rules_dir(str(repo))
        assert result == "root-rules/"

    def test_priority_order_correct(self, tmp_path: Path) -> None:
        """.claude/ config takes priority over repo root config."""
        repo = _setup_repo(tmp_path, "claude-rules")
        _write_config(repo, ".claude/focused-review.json", "claude-rules/")
        _write_config(repo, "focused-review.json", "root-rules/")
        _write_config(repo, ".github/focused-review.json", "github-rules/")

        result = fr._resolve_rules_dir(str(repo))
        assert result == "claude-rules/"

    def test_second_priority_when_first_missing(self, tmp_path: Path) -> None:
        """When .claude/ config is absent, repo root config is used."""
        repo = _setup_repo(tmp_path, "root-rules")
        _write_config(repo, "focused-review.json", "root-rules/")
        _write_config(repo, ".github/focused-review.json", "github-rules/")

        result = fr._resolve_rules_dir(str(repo))
        assert result == "root-rules/"

    def test_third_priority_github(self, tmp_path: Path) -> None:
        """When higher-priority configs are absent, .github/ config is used."""
        repo = _setup_repo(tmp_path, "github-rules")
        _write_config(repo, ".github/focused-review.json", "github-rules/")

        result = fr._resolve_rules_dir(str(repo))
        assert result == "github-rules/"

    def test_explicit_flag_overrides_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When both a config file and --rules-dir flag are present,
        the explicit flag wins."""
        repo = _setup_repo(tmp_path, "flag-rules")
        _write_config(repo, ".claude/focused-review.json", "config-rules/")

        diff = _make_diff("src/Foo.cs")

        with patch(
            "sys.argv",
            [
                "focused-review",
                "prepare-review",
                "--repo", str(repo),
                "--scope", "branch",
                "--rules-dir", "flag-rules/",
            ],
        ):
            with patch.object(
                fr, "_run_git", side_effect=_mock_git_results(diff, ["src/Foo.cs"])
            ):
                fr.main()

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["agents"] == 1

        dispatch_path = repo / ".agents" / "focused-review" / "dispatch.json"
        dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
        assert dispatch[0]["rule_path"] == "flag-rules/example.md"

    def test_no_config_falls_back_to_review(self, tmp_path: Path) -> None:
        """When no config file exists, _resolve_rules_dir returns review/."""
        repo = _setup_repo(tmp_path, "review")

        # Mocking CONFIG_SCAN_LOCATIONS to ensure no config file is found
        # in the test environment, regardless of the actual file system state.
        with patch.object(fr, "CONFIG_SCAN_LOCATIONS", ["nonexistent.json"]), \
             patch.object(fr, "CONFIG_USER_LOCATIONS", []):
             result = fr._resolve_rules_dir(str(repo))
        assert result == "review/"

    def test_backslash_in_config_value_normalized(self, tmp_path: Path) -> None:
        """Backslash path separators in config value are normalized to forward slashes."""
        repo = _setup_repo(tmp_path, os.path.join("custom", "rules"))
        _write_config(repo, ".claude/focused-review.json", "custom\\rules")

        result = fr._resolve_rules_dir(str(repo))
        assert result == "custom/rules"


# ---------------------------------------------------------------------------
# Integration: config resolution through prepare-review
# ---------------------------------------------------------------------------


class TestConfigResolutionIntegration:
    """Config file resolution works end-to-end through prepare-review."""

    def test_config_used_when_no_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """prepare-review uses config file when no --rules-dir flag is given."""
        repo = _setup_repo(tmp_path, "custom-rules")
        _write_config(repo, ".claude/focused-review.json", "custom-rules/")
        diff = _make_diff("src/Foo.cs")

        with patch(
            "sys.argv",
            [
                "focused-review",
                "prepare-review",
                "--repo", str(repo),
                "--scope", "branch",
            ],
        ):
            with patch.object(
                fr, "_run_git", side_effect=_mock_git_results(diff, ["src/Foo.cs"])
            ):
                fr.main()

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["agents"] == 1

        dispatch_path = repo / ".agents" / "focused-review" / "dispatch.json"
        dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
        assert dispatch[0]["rule_path"] == "custom-rules/example.md"

    def test_fallback_to_review_when_no_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """prepare-review falls back to review/ when no config file exists."""
        repo = _setup_repo(tmp_path, "review")
        diff = _make_diff("src/Foo.cs")

        # Mocking CONFIG_SCAN_LOCATIONS to avoid picking up the project's actual config
        with patch.object(fr, "CONFIG_SCAN_LOCATIONS", ["nonexistent.json"]), \
             patch.object(fr, "CONFIG_USER_LOCATIONS", []):
             with patch(
                "sys.argv",
                [
                    "focused-review",
                    "prepare-review",
                    "--repo", str(repo),
                    "--scope", "branch",
                ],
            ):
                with patch.object(
                    fr, "_run_git", side_effect=_mock_git_results(diff, ["src/Foo.cs"])
                ):
                    fr.main()

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["agents"] == 1

        dispatch_path = repo / ".agents" / "focused-review" / "dispatch.json"
        dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
        assert dispatch[0]["rule_path"] == "review/example.md"


# ---------------------------------------------------------------------------
# Path normalization through prepare-review
# ---------------------------------------------------------------------------


class TestRulesDirPathNormalization:
    """Windows-style backslashes in the rules dir path are normalized."""

    def test_backslash_path_normalized(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A rules dir with backslashes (e.g. from config on Windows) is handled correctly."""
        repo = _setup_repo(tmp_path, os.path.join("custom", "rules"))
        diff = _make_diff("src/Foo.cs")

        args = argparse.Namespace(
            repo=str(repo), scope="branch", rules_dir="custom\\rules"
        )

        with patch.object(
            fr, "_run_git", side_effect=_mock_git_results(diff, ["src/Foo.cs"])
        ):
            fr.prepare_review(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["agents"] == 1


# ---------------------------------------------------------------------------
# _resolve_config (unified config resolution)
# ---------------------------------------------------------------------------


class TestResolveConfig:
    """_resolve_config returns both rules_dir and sources from config file."""

    def test_returns_rules_dir_and_empty_sources_by_default(self, tmp_path: Path) -> None:
        """No config file returns defaults for both fields."""
        repo = _setup_repo(tmp_path)
        with patch.object(fr, "CONFIG_SCAN_LOCATIONS", ["nonexistent.json"]), \
             patch.object(fr, "CONFIG_USER_LOCATIONS", []):
            result = fr._resolve_config(str(repo))
        assert result["rules_dir"] == "review/"
        assert result["sources"] == []
        assert result["concerns_dir"] == "review/concerns/"
        assert result["scaling"] == "standard"
        assert result["config_file"] is None

    def test_returns_rules_dir_from_config(self, tmp_path: Path) -> None:
        """Config with only rules_dir returns it with empty sources."""
        repo = _setup_repo(tmp_path, "custom-rules")
        _write_config(repo, ".claude/focused-review.json", "custom-rules/")
        result = fr._resolve_config(str(repo))
        assert result["rules_dir"] == "custom-rules/"
        assert result["sources"] == []
        assert result["config_file"] is not None
        assert result["config_file"].endswith(".claude/focused-review.json")

    def test_returns_sources_from_config(self, tmp_path: Path) -> None:
        """Config with sources returns them."""
        repo = _setup_repo(tmp_path, "review")
        _write_config_with_sources(
            repo, ".claude/focused-review.json", "review/",
            [".github/skills/code-review/SKILL.md", "docs/review-guide.md"],
        )
        result = fr._resolve_config(str(repo))
        assert result["rules_dir"] == "review/"
        assert result["sources"] == [
            ".github/skills/code-review/SKILL.md",
            "docs/review-guide.md",
        ]

    def test_backslash_in_rules_dir_normalized(self, tmp_path: Path) -> None:
        """Backslash path separators in rules_dir are normalized."""
        repo = _setup_repo(tmp_path, os.path.join("custom", "rules"))
        _write_config(repo, ".claude/focused-review.json", "custom\\rules")
        result = fr._resolve_config(str(repo))
        assert result["rules_dir"] == "custom/rules"

    def test_priority_order_matches_resolve_rules_dir(self, tmp_path: Path) -> None:
        """_resolve_config uses the same priority order as _resolve_rules_dir."""
        repo = _setup_repo(tmp_path, "claude-rules")
        _write_config_with_sources(
            repo, ".claude/focused-review.json", "claude-rules/", ["source-a.md"],
        )
        _write_config_with_sources(
            repo, "focused-review.json", "root-rules/", ["source-b.md"],
        )
        result = fr._resolve_config(str(repo))
        assert result["rules_dir"] == "claude-rules/"
        assert result["sources"] == ["source-a.md"]


# ---------------------------------------------------------------------------
# resolve-config subcommand (CLI integration)
# ---------------------------------------------------------------------------


class TestResolveConfigSubcommand:
    """The resolve-config subcommand outputs JSON with rules_dir and sources."""

    def test_outputs_defaults_when_no_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With no config file, outputs default rules_dir and empty sources."""
        repo = _setup_repo(tmp_path)
        with patch.object(fr, "CONFIG_SCAN_LOCATIONS", ["nonexistent.json"]), \
             patch.object(fr, "CONFIG_USER_LOCATIONS", []):
            with patch("sys.argv", [
                "focused-review", "resolve-config", "--repo", str(repo),
            ]):
                fr.main()

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["rules_dir"] == "review/"
        assert result["sources"] == []
        assert result["concerns_dir"] == "review/concerns/"
        assert result["scaling"] == "standard"
        # script_path and defaults_dir are always present
        assert result["script_path"].endswith("focused-review.py")
        assert result["defaults_dir"].endswith("defaults/")

    def test_outputs_config_values(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With a config file, outputs its values."""
        repo = _setup_repo(tmp_path, "my-rules")
        _write_config_with_sources(
            repo, ".claude/focused-review.json", "my-rules/",
            ["docs/guide.md"],
        )
        with patch("sys.argv", [
            "focused-review", "resolve-config", "--repo", str(repo),
        ]):
            fr.main()

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["rules_dir"] == "my-rules/"
        assert result["sources"] == ["docs/guide.md"]
        assert result["concerns_dir"] == "review/concerns/"
        assert result["scaling"] == "standard"

    def test_rules_dir_gets_trailing_slash(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """rules_dir without trailing slash gets one added."""
        repo = _setup_repo(tmp_path, "norules")
        _write_config(repo, ".claude/focused-review.json", "norules")
        with patch("sys.argv", [
            "focused-review", "resolve-config", "--repo", str(repo),
        ]):
            fr.main()

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["rules_dir"] == "norules/"

    def test_invalid_repo_exits_with_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Non-existent repo path causes exit with error."""
        fake_repo = str(tmp_path / "nonexistent")
        with patch("sys.argv", [
            "focused-review", "resolve-config", "--repo", fake_repo,
        ]):
            with pytest.raises(SystemExit) as exc:
                fr.main()
            assert exc.value.code == 1

        captured = capsys.readouterr()
        assert "error" in captured.err.lower()
