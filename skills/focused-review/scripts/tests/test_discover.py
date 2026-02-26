"""Tests for the 'discover' subcommand of focused-review.py."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests import create_file

# Import the module under test via its hyphenated filename
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importlib

fr = importlib.import_module("focused-review")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_discover(repo: Path) -> list[str]:
    """Run ``_find_instruction_files`` + dedup, return posix-relative paths."""
    files = fr._resolve_and_deduplicate(fr._find_instruction_files(repo))
    return [fr._posix(f, relative_to=repo) for f in files]


# ---------------------------------------------------------------------------
# Core discovery tests
# ---------------------------------------------------------------------------


class TestDiscoverPatterns:
    """Verify each instruction-file pattern is found."""

    def test_claude_md_root(self, tmp_path: Path) -> None:
        create_file(tmp_path, "CLAUDE.md")
        result = _run_discover(tmp_path)
        assert "CLAUDE.md" in result

    def test_claude_md_one_level_deep(self, tmp_path: Path) -> None:
        create_file(tmp_path, "subdir/CLAUDE.md")
        result = _run_discover(tmp_path)
        assert "subdir/CLAUDE.md" in result

    def test_claude_md_two_levels_deep(self, tmp_path: Path) -> None:
        create_file(tmp_path, "a/b/CLAUDE.md")
        result = _run_discover(tmp_path)
        assert "a/b/CLAUDE.md" in result

    def test_claude_md_three_levels_deep_not_found(self, tmp_path: Path) -> None:
        """Pattern only goes two levels deep for CLAUDE.md subdirectories."""
        create_file(tmp_path, "a/b/c/CLAUDE.md")
        result = _run_discover(tmp_path)
        assert "a/b/c/CLAUDE.md" not in result

    def test_gemini_md(self, tmp_path: Path) -> None:
        create_file(tmp_path, "GEMINI.md")
        result = _run_discover(tmp_path)
        assert "GEMINI.md" in result

    def test_agents_md_uppercase(self, tmp_path: Path) -> None:
        create_file(tmp_path, "AGENTS.md")
        result = _run_discover(tmp_path)
        assert "AGENTS.md" in result

    def test_agents_md_lowercase(self, tmp_path: Path) -> None:
        create_file(tmp_path, "agents.md")
        result = _run_discover(tmp_path)
        assert "agents.md" in result

    def test_copilot_instructions(self, tmp_path: Path) -> None:
        create_file(tmp_path, ".github/copilot-instructions.md")
        result = _run_discover(tmp_path)
        assert ".github/copilot-instructions.md" in result

    def test_github_instructions_glob(self, tmp_path: Path) -> None:
        create_file(tmp_path, ".github/instructions/cs/style.instructions.md")
        create_file(tmp_path, ".github/instructions/general.instructions.md")
        result = _run_discover(tmp_path)
        assert ".github/instructions/general.instructions.md" in result
        assert ".github/instructions/cs/style.instructions.md" in result

    def test_cursor_rules_md(self, tmp_path: Path) -> None:
        create_file(tmp_path, ".cursor/rules/my-rule.md")
        result = _run_discover(tmp_path)
        assert ".cursor/rules/my-rule.md" in result

    def test_cursor_rules_mdc(self, tmp_path: Path) -> None:
        create_file(tmp_path, ".cursor/rules/my-rule.mdc")
        result = _run_discover(tmp_path)
        assert ".cursor/rules/my-rule.mdc" in result

    def test_cursorrules(self, tmp_path: Path) -> None:
        create_file(tmp_path, ".cursorrules")
        result = _run_discover(tmp_path)
        assert ".cursorrules" in result

    def test_windsurfrules(self, tmp_path: Path) -> None:
        create_file(tmp_path, ".windsurfrules")
        result = _run_discover(tmp_path)
        assert ".windsurfrules" in result

    def test_clinerules(self, tmp_path: Path) -> None:
        create_file(tmp_path, ".clinerules")
        result = _run_discover(tmp_path)
        assert ".clinerules" in result


class TestDiscoverAllPatterns:
    """Verify all patterns are found in a single scan."""

    def test_finds_all_known_patterns(self, tmp_path: Path) -> None:
        # On Windows, AGENTS.md and agents.md are the same file (case-insensitive FS),
        # so we test them separately. Here we test all non-conflicting patterns together.
        files_to_create = [
            "CLAUDE.md",
            "subdir/CLAUDE.md",
            "GEMINI.md",
            "AGENTS.md",
            ".github/copilot-instructions.md",
            ".github/instructions/style.instructions.md",
            ".cursor/rules/rule1.md",
            ".cursor/rules/rule2.mdc",
            ".cursorrules",
            ".windsurfrules",
            ".clinerules",
        ]
        for rel in files_to_create:
            create_file(tmp_path, rel)

        result = _run_discover(tmp_path)

        # Check that all patterns produced results. On Windows AGENTS.md
        # may appear as lowercase depending on filesystem, so we normalize.
        result_lower = [r.lower() for r in result]
        for rel in files_to_create:
            assert rel.lower() in result_lower, f"Expected {rel!r} in results (case-insensitive)"
        # At least 11 unique files found
        assert len(result) >= len(files_to_create)

    def test_ignores_non_matching_files(self, tmp_path: Path) -> None:
        create_file(tmp_path, "CLAUDE.md")
        create_file(tmp_path, "README.md")
        create_file(tmp_path, "src/main.py")
        create_file(tmp_path, ".github/workflows/ci.yml")

        result = _run_discover(tmp_path)
        assert "CLAUDE.md" in result
        assert "README.md" not in result
        assert "src/main.py" not in result


# ---------------------------------------------------------------------------
# Symlink resolution & deduplication
# ---------------------------------------------------------------------------


class TestSymlinksAndDedup:

    @pytest.mark.skipif(
        sys.platform == "win32" and not os.environ.get("CI"),
        reason="Symlinks may require elevated privileges on Windows",
    )
    def test_symlink_resolved_and_deduped(self, tmp_path: Path) -> None:
        """A symlink pointing to an already-discovered file → single entry."""
        real = create_file(tmp_path, "CLAUDE.md")
        link = tmp_path / "subdir" / "CLAUDE.md"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)

        result = _run_discover(tmp_path)
        # Both match CLAUDE.md and */CLAUDE.md patterns, but resolve to same file
        assert result.count("CLAUDE.md") == 1 or len(result) == 1

    def test_duplicates_from_same_resolved_path(self, tmp_path: Path) -> None:
        """If two pattern matches resolve to the same file, deduplicate."""
        create_file(tmp_path, "CLAUDE.md")
        paths = [tmp_path / "CLAUDE.md", tmp_path / "CLAUDE.md"]  # duplicate entries
        deduped = fr._resolve_and_deduplicate(paths)
        assert len(deduped) == 1


# ---------------------------------------------------------------------------
# COPILOT_CUSTOM_INSTRUCTIONS_DIRS env var
# ---------------------------------------------------------------------------


class TestCustomInstructionDirs:

    def test_reads_from_env_var(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        create_file(custom_dir, "my-instructions.md", "# Custom")

        with patch.dict(os.environ, {"COPILOT_CUSTOM_INSTRUCTIONS_DIRS": str(custom_dir)}):
            files = fr._resolve_and_deduplicate(fr._find_instruction_files(repo))

        names = [f.name for f in files]
        assert "my-instructions.md" in names

    def test_multiple_dirs_separated(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        d1 = tmp_path / "dir1"
        d2 = tmp_path / "dir2"
        d1.mkdir()
        d2.mkdir()
        create_file(d1, "a.md")
        create_file(d2, "b.md")

        dirs_str = f"{d1}{os.pathsep}{d2}"
        with patch.dict(os.environ, {"COPILOT_CUSTOM_INSTRUCTIONS_DIRS": dirs_str}):
            files = fr._resolve_and_deduplicate(fr._find_instruction_files(repo))

        names = [f.name for f in files]
        assert "a.md" in names
        assert "b.md" in names

    def test_nonexistent_custom_dir_skipped(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        nonexistent = tmp_path / "does-not-exist"

        with patch.dict(os.environ, {"COPILOT_CUSTOM_INSTRUCTIONS_DIRS": str(nonexistent)}):
            files = fr._find_instruction_files(repo)

        assert len(files) == 0

    def test_empty_env_var(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        with patch.dict(os.environ, {"COPILOT_CUSTOM_INSTRUCTIONS_DIRS": ""}):
            files = fr._find_instruction_files(repo)

        assert len(files) == 0

    def test_no_env_var_set(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        with patch.dict(os.environ, {}, clear=True):
            # Ensure the key is absent
            os.environ.pop("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", None)
            files = fr._find_instruction_files(repo)

        assert len(files) == 0


# ---------------------------------------------------------------------------
# discover() CLI entry point
# ---------------------------------------------------------------------------


class TestDiscoverCli:

    def test_outputs_valid_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        create_file(tmp_path, "CLAUDE.md")
        create_file(tmp_path, ".cursorrules")

        args = argparse.Namespace(repo=str(tmp_path))
        # Clear env var to avoid interference
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", None)
            fr.discover(args)

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert isinstance(result, list)
        assert len(result) == 2

    def test_nonexistent_repo_exits(self, tmp_path: Path) -> None:
        args = argparse.Namespace(repo=str(tmp_path / "nonexistent"))
        with pytest.raises(SystemExit) as exc_info:
            fr.discover(args)
        assert exc_info.value.code == 1
