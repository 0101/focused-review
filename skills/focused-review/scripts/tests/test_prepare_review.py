"""Tests for the 'prepare-review' subcommand of focused-review.py."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tests import create_file

# Import the module under test via its hyphenated filename
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importlib

fr = importlib.import_module("focused-review")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rule(
    name: str,
    model: str = "haiku",
    autofix: bool = False,
    applies_to: str | None = None,
    source: str = "CLAUDE.md",
) -> str:
    """Build a rule markdown file with frontmatter."""
    lines = ["---"]
    lines.append(f"autofix: {'true' if autofix else 'false'}")
    lines.append(f"model: {model}")
    if applies_to is not None:
        lines.append(f'applies-to: "{applies_to}"')
    lines.append(f"source: {source}")
    lines.append("---")
    lines.append(f"# {name}")
    lines.append("## Rule")
    lines.append(f"Check for {name.lower()}.")
    return "\n".join(lines)


def _make_diff(*file_entries: tuple[str, int]) -> str:
    """Build a synthetic unified diff.

    Each *file_entry* is ``(filename, num_lines)`` producing that many
    ``+added line`` entries under a diff header.
    """
    parts: list[str] = []
    for filename, num_lines in file_entries:
        parts.append(f"diff --git a/{filename} b/{filename}")
        parts.append(f"--- a/{filename}")
        parts.append(f"+++ b/{filename}")
        parts.append("@@ -1,0 +1,{} @@".format(num_lines))
        for i in range(num_lines):
            parts.append(f"+added line {i + 1}")
    return "\n".join(parts)


def _mock_git_results(diff_text: str, changed_files: list[str]) -> list[MagicMock]:
    """Create side_effect for _run_git that returns diff then name-only results."""
    diff_mock = MagicMock()
    diff_mock.returncode = 0
    diff_mock.stdout = diff_text

    names_mock = MagicMock()
    names_mock.returncode = 0
    names_mock.stdout = "\n".join(changed_files)

    return [diff_mock, names_mock]


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


class TestParseFrontmatter:

    def test_basic_frontmatter(self) -> None:
        content = "---\nautofix: true\nmodel: haiku\n---\n# Rule Name\nBody text."
        meta, body = fr._parse_frontmatter(content)
        assert meta["autofix"] is True
        assert meta["model"] == "haiku"
        assert "# Rule Name" in body

    def test_no_frontmatter(self) -> None:
        content = "# Just a heading\nSome text."
        meta, body = fr._parse_frontmatter(content)
        assert meta == {}
        assert body == content

    def test_quoted_values(self) -> None:
        content = '---\napplies-to: "**/*Tests*.cs"\nsource: \'CLAUDE.md\'\n---\nBody.'
        meta, body = fr._parse_frontmatter(content)
        assert meta["applies-to"] == "**/*Tests*.cs"
        assert meta["source"] == "CLAUDE.md"

    def test_boolean_false(self) -> None:
        content = "---\nautofix: false\n---\nBody."
        meta, _body = fr._parse_frontmatter(content)
        assert meta["autofix"] is False

    def test_comments_skipped(self) -> None:
        content = "---\n# this is a comment\nautofix: true\n---\nBody."
        meta, _body = fr._parse_frontmatter(content)
        assert "# this is a comment" not in meta
        assert meta["autofix"] is True


# ---------------------------------------------------------------------------
# Glob matching
# ---------------------------------------------------------------------------


class TestGlobMatching:

    def test_doublestar_matches_deep_path(self) -> None:
        assert fr._file_matches_glob("src/deep/nested/File.cs", "**/*.cs")

    def test_doublestar_matches_shallow(self) -> None:
        assert fr._file_matches_glob("File.cs", "**/*.cs")

    def test_star_only_matches_single_segment(self) -> None:
        assert fr._file_matches_glob("src/Test.cs", "src/*.cs")
        assert not fr._file_matches_glob("src/nested/Test.cs", "src/*.cs")

    def test_question_mark(self) -> None:
        assert fr._file_matches_glob("src/A.cs", "src/?.cs")
        assert not fr._file_matches_glob("src/AB.cs", "src/?.cs")

    def test_windows_paths_normalized(self) -> None:
        assert fr._file_matches_glob("src\\tests\\MyTest.cs", "**/*Test*.cs")

    def test_specific_test_pattern(self) -> None:
        pattern = "**/*Tests*.cs"
        assert fr._file_matches_glob("src/MyTests.cs", pattern)
        assert fr._file_matches_glob("src/deep/SomeTestsFile.cs", pattern)
        assert not fr._file_matches_glob("src/MyHelper.cs", pattern)


# ---------------------------------------------------------------------------
# Rule reader
# ---------------------------------------------------------------------------


class TestReadRules:

    def test_reads_rules_from_directory(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / "review"
        rules_dir.mkdir()
        create_file(rules_dir, "sealed.md", _make_rule("Sealed Classes"))
        create_file(rules_dir, "immutable.md", _make_rule("Immutable Data", model="sonnet"))

        rules = fr._read_rules(rules_dir, tmp_path)
        assert len(rules) == 2
        names = {r["name"] for r in rules}
        assert "Sealed Classes" in names
        assert "Immutable Data" in names

    def test_extracts_metadata(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / "review"
        rules_dir.mkdir()
        create_file(
            rules_dir,
            "test-rule.md",
            _make_rule("Test Rule", model="haiku", autofix=True, applies_to="**/*.cs"),
        )

        rules = fr._read_rules(rules_dir, tmp_path)
        assert len(rules) == 1
        r = rules[0]
        assert r["model"] == "haiku"
        assert r["autofix"] is True
        assert r["applies_to"] == "**/*.cs"

    def test_missing_directory_returns_empty(self, tmp_path: Path) -> None:
        rules = fr._read_rules(tmp_path / "nonexistent", tmp_path)
        assert rules == []

    def test_rule_name_from_heading(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / "review"
        rules_dir.mkdir()
        content = "---\nautofix: false\n---\n# My Custom Rule Name\nBody."
        create_file(rules_dir, "custom.md", content)

        rules = fr._read_rules(rules_dir, tmp_path)
        assert rules[0]["name"] == "My Custom Rule Name"

    def test_default_model_is_haiku(self, tmp_path: Path) -> None:
        """When a rule omits the 'model' field, it defaults to 'haiku'."""
        rules_dir = tmp_path / "review"
        rules_dir.mkdir()
        content = "---\nautofix: false\n---\n# No Model Rule\nBody."
        create_file(rules_dir, "no-model.md", content)

        rules = fr._read_rules(rules_dir, tmp_path)
        assert rules[0]["model"] == "haiku"

    def test_rule_name_fallback_to_stem(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / "review"
        rules_dir.mkdir()
        content = "---\nautofix: false\n---\nNo heading here, just text."
        create_file(rules_dir, "my-rule.md", content)

        rules = fr._read_rules(rules_dir, tmp_path)
        assert rules[0]["name"] == "my-rule"

    def test_rule_path_is_posix(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / "review"
        rules_dir.mkdir()
        create_file(rules_dir, "rule.md", _make_rule("Rule"))

        rules = fr._read_rules(rules_dir, tmp_path)
        assert "\\" not in rules[0]["path"]
        assert rules[0]["path"] == "review/rule.md"

    def test_no_chunking_field_in_rules(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / "review"
        rules_dir.mkdir()
        create_file(rules_dir, "rule.md", _make_rule("Rule"))

        rules = fr._read_rules(rules_dir, tmp_path)
        assert "chunking" not in rules[0]


# ---------------------------------------------------------------------------
# Diff parsing helpers
# ---------------------------------------------------------------------------


class TestDiffParsing:

    def test_changed_files_extracted(self) -> None:
        diff = _make_diff(("src/Foo.cs", 3), ("src/Bar.cs", 2))
        files = fr._changed_files_from_diff(diff)
        assert files == ["src/Foo.cs", "src/Bar.cs"]

    def test_empty_diff_returns_empty(self) -> None:
        assert fr._changed_files_from_diff("") == []

    def test_split_diff_by_file(self) -> None:
        diff = _make_diff(("a.cs", 5), ("b.cs", 3))
        parts = fr._split_diff_by_file(diff)
        assert len(parts) == 2
        assert parts[0][0] == "a.cs"
        assert parts[1][0] == "b.cs"


# ---------------------------------------------------------------------------
# Diff chunking
# ---------------------------------------------------------------------------


class TestDiffChunking:

    def test_small_diff_no_chunking(self, tmp_path: Path) -> None:
        work_dir = tmp_path / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        diff = _make_diff(("file.cs", 50))
        chunks = fr._write_chunks(diff, work_dir, target_lines=500)

        assert len(chunks) == 1
        assert chunks[0].name == "diff.patch"
        assert chunks[0].exists()

    def test_large_diff_chunks_at_file_boundaries(self, tmp_path: Path) -> None:
        work_dir = tmp_path / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        # 3 files, each ~200 lines → total ~600 lines > 500 target
        diff = _make_diff(("a.cs", 200), ("b.cs", 200), ("c.cs", 200))
        chunks = fr._write_chunks(diff, work_dir, target_lines=500)

        # Should create multiple chunks
        assert len(chunks) >= 2
        # All chunks should exist
        for c in chunks:
            assert c.exists()

    def test_diff_patch_always_written(self, tmp_path: Path) -> None:
        work_dir = tmp_path / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        diff = _make_diff(("a.cs", 300), ("b.cs", 300))
        fr._write_chunks(diff, work_dir, target_lines=500)

        assert (work_dir / "diff.patch").exists()

    def test_chunks_preserve_all_content(self, tmp_path: Path) -> None:
        work_dir = tmp_path / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        diff = _make_diff(("a.cs", 300), ("b.cs", 300))
        chunks = fr._write_chunks(diff, work_dir, target_lines=500)

        # Combine all chunk content
        combined_files: set[str] = set()
        for c in chunks:
            content = c.read_text(encoding="utf-8")
            combined_files.update(fr._changed_files_from_diff(content))

        assert "a.cs" in combined_files
        assert "b.cs" in combined_files

    def test_single_huge_file_stays_in_one_chunk(self, tmp_path: Path) -> None:
        """A single file > target can't be split — remains as one chunk."""
        work_dir = tmp_path / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        diff = _make_diff(("big.cs", 800))
        chunks = fr._write_chunks(diff, work_dir, target_lines=500)

        # With only one file diff, it can't be split at file boundaries
        assert len(chunks) == 1


# ---------------------------------------------------------------------------
# Dispatch plan building
# ---------------------------------------------------------------------------


class TestBuildDispatch:

    def _rule(
        self,
        name: str = "rule",
        applies_to: str | None = None,
        model: str = "haiku",
        autofix: bool = False,
    ) -> dict[str, object]:
        return {
            "path": f"review/{name}.md",
            "name": name,
            "model": model,
            "autofix": autofix,
            "applies_to": applies_to,
            "source": "CLAUDE.md",
        }

    def test_rule_without_applies_to_matches_all(self, tmp_path: Path) -> None:
        rules = [self._rule("all-files")]
        chunk_path = tmp_path / "diff.patch"
        chunk_path.write_text(_make_diff(("any/file.py", 10)), encoding="utf-8")

        dispatch = fr._build_dispatch(rules, [chunk_path], ["any/file.py"], "branch", tmp_path)
        assert len(dispatch) == 1
        assert dispatch[0]["chunk_index"] == 1
        assert dispatch[0]["total_chunks"] == 1

    def test_applies_to_filters_rules(self, tmp_path: Path) -> None:
        rules = [
            self._rule("cs-only", applies_to="**/*.cs"),
            self._rule("py-only", applies_to="**/*.py"),
        ]
        chunk_path = tmp_path / "diff.patch"
        chunk_path.write_text(_make_diff(("src/Main.cs", 10)), encoding="utf-8")

        dispatch = fr._build_dispatch(
            rules, [chunk_path], ["src/Main.cs"], "branch", tmp_path
        )
        # Only cs-only should match
        rule_paths = [d["rule_path"] for d in dispatch]
        assert "review/cs-only.md" in rule_paths
        assert "review/py-only.md" not in rule_paths

    def test_dispatch_contains_expected_fields(self, tmp_path: Path) -> None:
        rules = [self._rule("test", model="sonnet", autofix=True)]
        chunk_path = tmp_path / "diff.patch"
        chunk_path.write_text(_make_diff(("file.cs", 5)), encoding="utf-8")

        dispatch = fr._build_dispatch(rules, [chunk_path], ["file.cs"], "branch", tmp_path)
        assert len(dispatch) == 1
        entry = dispatch[0]
        assert entry["rule_path"] == "review/test.md"
        assert entry["model"] == "sonnet"
        assert entry["autofix"] is True
        assert entry["scope"] == "branch"
        assert "chunk_path" in entry
        assert entry["chunk_index"] == 1
        assert entry["total_chunks"] == 1

    def test_full_scope_no_chunk_path(self, tmp_path: Path) -> None:
        rules = [self._rule("all")]
        dispatch = fr._build_dispatch(rules, [], ["a.cs", "b.py"], "full", tmp_path)
        assert len(dispatch) == 1
        assert dispatch[0]["chunk_path"] is None
        assert dispatch[0]["chunk_index"] is None
        assert dispatch[0]["total_chunks"] is None
        assert dispatch[0]["scope"] == "full"

    def test_multiple_chunks_dispatched_per_rule(self, tmp_path: Path) -> None:
        rules = [self._rule("generic")]
        c1 = tmp_path / "c1.patch"
        c2 = tmp_path / "c2.patch"
        c1.write_text(_make_diff(("a.cs", 10)), encoding="utf-8")
        c2.write_text(_make_diff(("b.cs", 10)), encoding="utf-8")

        dispatch = fr._build_dispatch(
            rules, [c1, c2], ["a.cs", "b.cs"], "branch", tmp_path
        )
        assert len(dispatch) == 2

    def test_applies_to_filters_per_chunk(self, tmp_path: Path) -> None:
        """Rule with applies-to only dispatches to chunks containing matching files."""
        rules = [self._rule("cs-only", applies_to="**/*.cs")]
        c1 = tmp_path / "c1.patch"
        c2 = tmp_path / "c2.patch"
        c1.write_text(_make_diff(("src/Main.cs", 10)), encoding="utf-8")
        c2.write_text(_make_diff(("src/script.py", 10)), encoding="utf-8")

        dispatch = fr._build_dispatch(
            rules, [c1, c2], ["src/Main.cs", "src/script.py"], "branch", tmp_path
        )
        assert len(dispatch) == 1
        assert "c1.patch" in dispatch[0]["chunk_path"]
        assert dispatch[0]["chunk_index"] == 1
        assert dispatch[0]["total_chunks"] == 2

    def test_multiple_chunks_have_correct_indices(self, tmp_path: Path) -> None:
        """Each dispatch entry gets 1-based chunk_index and total_chunks."""
        rules = [self._rule("generic")]
        c1 = tmp_path / "c1.patch"
        c2 = tmp_path / "c2.patch"
        c1.write_text(_make_diff(("a.cs", 10)), encoding="utf-8")
        c2.write_text(_make_diff(("b.cs", 10)), encoding="utf-8")

        dispatch = fr._build_dispatch(
            rules, [c1, c2], ["a.cs", "b.cs"], "branch", tmp_path
        )
        assert len(dispatch) == 2
        assert dispatch[0]["chunk_index"] == 1
        assert dispatch[0]["total_chunks"] == 2
        assert dispatch[1]["chunk_index"] == 2
        assert dispatch[1]["total_chunks"] == 2


# ---------------------------------------------------------------------------
# prepare-review end-to-end (mocking git)
# ---------------------------------------------------------------------------


class TestPrepareReviewEndToEnd:

    def _setup_repo_with_rules(
        self, tmp_path: Path, rules: list[tuple[str, str]]
    ) -> Path:
        """Create a repo dir with rules in review/."""
        repo = tmp_path / "repo"
        repo.mkdir()
        rules_dir = repo / "review"
        rules_dir.mkdir()
        for name, content in rules:
            create_file(rules_dir, name, content)
        return repo

    def test_produces_dispatch_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = self._setup_repo_with_rules(
            tmp_path,
            [("sealed.md", _make_rule("Sealed Classes"))],
        )

        diff = _make_diff(("src/Foo.cs", 50))

        args = argparse.Namespace(repo=str(repo), scope="branch", rules_dir="review/")

        with patch.object(fr, "_run_git", side_effect=_mock_git_results(diff, ["src/Foo.cs"])):
            fr.prepare_review(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["agents"] == 1
        assert summary["scope"] == "branch"
        assert summary["rules_total"] == 1
        assert summary["rules_matched"] == 1

        # Verify dispatch.json was written
        dispatch_path = repo / ".agents" / "focused-review" / "dispatch.json"
        assert dispatch_path.exists()
        dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
        assert len(dispatch) == 1
        assert dispatch[0]["rule_path"] == "review/sealed.md"

    def test_applies_to_filtering_in_full_flow(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = self._setup_repo_with_rules(
            tmp_path,
            [
                ("cs-rule.md", _make_rule("CS Rule", applies_to="**/*.cs")),
                ("py-rule.md", _make_rule("PY Rule", applies_to="**/*.py")),
                ("all-rule.md", _make_rule("All Rule")),
            ],
        )

        diff = _make_diff(("src/Main.cs", 50))

        args = argparse.Namespace(repo=str(repo), scope="staged", rules_dir="review/")

        with patch.object(fr, "_run_git", side_effect=_mock_git_results(diff, ["src/Main.cs"])):
            fr.prepare_review(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        # CS Rule + All Rule match, PY Rule doesn't
        assert summary["rules_matched"] == 2
        assert summary["rules_total"] == 3

    def test_chunking_in_full_flow(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = self._setup_repo_with_rules(
            tmp_path,
            [("rule.md", _make_rule("Rule"))],
        )

        # Two files that together exceed DIFF_TARGET_CHUNK_LINES (3000)
        diff = _make_diff(("a.cs", 1600), ("b.cs", 1600))

        args = argparse.Namespace(repo=str(repo), scope="branch", rules_dir="review/")

        with patch.object(fr, "_run_git", side_effect=_mock_git_results(diff, ["a.cs", "b.cs"])):
            fr.prepare_review(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["chunks"] >= 2
        assert summary["agents"] >= 2

    def test_changed_files_written(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = self._setup_repo_with_rules(
            tmp_path,
            [("rule.md", _make_rule("Rule"))],
        )

        diff = _make_diff(("src/A.cs", 10), ("src/B.cs", 10))

        args = argparse.Namespace(repo=str(repo), scope="branch", rules_dir="review/")

        with patch.object(fr, "_run_git", side_effect=_mock_git_results(diff, ["src/A.cs", "src/B.cs"])):
            fr.prepare_review(args)

        changed_files_path = repo / ".agents" / "focused-review" / "changed-files.txt"
        assert changed_files_path.exists()
        content = changed_files_path.read_text(encoding="utf-8")
        assert "src/A.cs" in content
        assert "src/B.cs" in content

    def test_no_rules_exits_with_error(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        args = argparse.Namespace(repo=str(repo), scope="branch", rules_dir="review/")

        with pytest.raises(SystemExit) as exc_info:
            fr.prepare_review(args)
        assert exc_info.value.code == 1

    def test_empty_diff_returns_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = self._setup_repo_with_rules(
            tmp_path,
            [("rule.md", _make_rule("Rule"))],
        )

        args = argparse.Namespace(repo=str(repo), scope="branch", rules_dir="review/")

        with patch.object(fr, "_run_git", side_effect=_mock_git_results("", [])):
            fr.prepare_review(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["agents"] == 0
        assert summary["changed_files"] == 0

    def test_dispatch_chunk_paths_are_posix(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """All paths in dispatch.json use forward slashes (Windows compat)."""
        repo = self._setup_repo_with_rules(
            tmp_path,
            [("rule.md", _make_rule("Rule"))],
        )

        diff = _make_diff(("src/Foo.cs", 50))

        args = argparse.Namespace(repo=str(repo), scope="branch", rules_dir="review/")

        with patch.object(fr, "_run_git", side_effect=_mock_git_results(diff, ["src/Foo.cs"])):
            fr.prepare_review(args)

        dispatch_path = repo / ".agents" / "focused-review" / "dispatch.json"
        dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
        for entry in dispatch:
            if entry.get("chunk_path"):
                assert "\\" not in entry["chunk_path"], (
                    f"Backslash found in chunk_path: {entry['chunk_path']}"
                )
            assert "\\" not in entry["rule_path"], (
                f"Backslash found in rule_path: {entry['rule_path']}"
            )


# ---------------------------------------------------------------------------
# _posix helper
# ---------------------------------------------------------------------------


class TestPosixHelper:

    def test_forward_slashes(self) -> None:
        p = Path("src") / "test" / "file.cs"
        assert "\\" not in fr._posix(p)

    def test_relative_to(self, tmp_path: Path) -> None:
        full = tmp_path / "src" / "file.cs"
        result = fr._posix(full, relative_to=tmp_path)
        assert result == "src/file.cs"

    def test_outside_relative_to_keeps_path(self, tmp_path: Path) -> None:
        outside = Path("/other/path/file.cs")
        result = fr._posix(outside, relative_to=tmp_path)
        # Should not crash, just return posix of the full path
        assert "/" in result or result == "file.cs"
