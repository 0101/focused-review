"""Tests for concern reading, per-file diffs, and concern prompt generation."""

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


def _make_concern(
    name: str,
    *,
    models: str = "[opus]",
    priority: str = "standard",
    applies_to: str | None = None,
) -> str:
    """Build a concern markdown file with frontmatter."""
    lines = ["---"]
    lines.append("type: concern")
    lines.append(f"models: {models}")
    lines.append(f"priority: {priority}")
    if applies_to is not None:
        lines.append(f'applies-to: "{applies_to}"')
    lines.append("---")
    lines.append(f"# {name}")
    lines.append("## Role")
    lines.append(f"You are a {name.lower()} reviewer.")
    lines.append("## What to Check")
    lines.append(f"Check for {name.lower()} issues.")
    return "\n".join(lines)


def _make_rule(name: str) -> str:
    """Build a minimal rule file (reused from other tests)."""
    return f"---\nautofix: false\nmodel: haiku\n---\n# {name}\n## Rule\nCheck."


def _make_diff(*file_entries: tuple[str, int]) -> str:
    """Build a synthetic unified diff."""
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
    """Create side_effect for _run_git."""
    diff_mock = MagicMock()
    diff_mock.returncode = 0
    diff_mock.stdout = diff_text

    names_mock = MagicMock()
    names_mock.returncode = 0
    names_mock.stdout = "\n".join(changed_files)

    return [diff_mock, names_mock]


# ---------------------------------------------------------------------------
# Frontmatter: inline YAML list parsing
# ---------------------------------------------------------------------------


class TestFrontmatterListParsing:

    def test_single_element_list(self) -> None:
        content = "---\nmodels: [opus]\n---\nBody."
        meta, _body = fr._parse_frontmatter(content)
        assert meta["models"] == ["opus"]

    def test_multi_element_list(self) -> None:
        content = "---\nmodels: [opus, codex, gemini]\n---\nBody."
        meta, _body = fr._parse_frontmatter(content)
        assert meta["models"] == ["opus", "codex", "gemini"]

    def test_list_with_quoted_items(self) -> None:
        content = "---\nmodels: ['opus', \"codex\"]\n---\nBody."
        meta, _body = fr._parse_frontmatter(content)
        assert meta["models"] == ["opus", "codex"]

    def test_empty_list(self) -> None:
        content = "---\nmodels: []\n---\nBody."
        meta, _body = fr._parse_frontmatter(content)
        assert meta["models"] == []

    def test_list_coexists_with_scalars(self) -> None:
        content = "---\ntype: concern\nmodels: [opus]\npriority: high\nautofix: true\n---\nBody."
        meta, _body = fr._parse_frontmatter(content)
        assert meta["type"] == "concern"
        assert meta["models"] == ["opus"]
        assert meta["priority"] == "high"
        assert meta["autofix"] is True

    def test_brace_glob_preserved_in_list(self) -> None:
        content = '---\napplies-to: ["**/*.{cs,fs}", "!**/*Tests*.cs"]\n---\nBody.'
        meta, _body = fr._parse_frontmatter(content)
        assert meta["applies-to"] == ["**/*.{cs,fs}", "!**/*Tests*.cs"]

    def test_brace_glob_single_item(self) -> None:
        content = "---\napplies-to: [**/*.{cs,fs}]\n---\nBody."
        meta, _body = fr._parse_frontmatter(content)
        assert meta["applies-to"] == ["**/*.{cs,fs}"]

    def test_nested_braces_preserved(self) -> None:
        content = "---\npatterns: [a.{b,{c,d}}, e]\n---\nBody."
        meta, _body = fr._parse_frontmatter(content)
        assert meta["patterns"] == ["a.{b,{c,d}}", "e"]


# ---------------------------------------------------------------------------
# _read_concerns
# ---------------------------------------------------------------------------


class TestReadConcerns:

    def test_reads_concern_files(self, tmp_path: Path) -> None:
        concerns_dir = tmp_path / "concerns"
        concerns_dir.mkdir()
        create_file(concerns_dir, "bugs.md", _make_concern("Bug Finder"))
        create_file(concerns_dir, "security.md", _make_concern("Security Scanner"))

        concerns = fr._read_concerns(concerns_dir, tmp_path)
        assert len(concerns) == 2
        names = {c["name"] for c in concerns}
        assert "bugs" in names
        assert "security" in names

    def test_extracts_metadata(self, tmp_path: Path) -> None:
        concerns_dir = tmp_path / "concerns"
        concerns_dir.mkdir()
        create_file(
            concerns_dir,
            "bugs.md",
            _make_concern("Bug Finder", models="[opus, codex]", priority="high"),
        )

        concerns = fr._read_concerns(concerns_dir, tmp_path)
        assert len(concerns) == 1
        c = concerns[0]
        assert c["name"] == "bugs"
        assert c["display_name"] == "Bug Finder"
        assert c["models"] == ["opus", "codex"]
        assert c["priority"] == "high"

    def test_filters_by_type_concern(self, tmp_path: Path) -> None:
        """Only files with type: concern are included."""
        concerns_dir = tmp_path / "concerns"
        concerns_dir.mkdir()
        create_file(concerns_dir, "bugs.md", _make_concern("Bug Finder"))
        # A rule file in the concerns dir should be ignored
        create_file(
            concerns_dir,
            "stray-rule.md",
            "---\ntype: rule\nmodel: haiku\n---\n# Not a Concern\nBody.",
        )
        # A file without frontmatter should be ignored
        create_file(concerns_dir, "readme.md", "# README\nJust docs.")

        concerns = fr._read_concerns(concerns_dir, tmp_path)
        assert len(concerns) == 1
        assert concerns[0]["name"] == "bugs"

    def test_missing_directory_returns_empty(self, tmp_path: Path) -> None:
        concerns = fr._read_concerns(tmp_path / "nonexistent", tmp_path)
        assert concerns == []

    def test_applies_to_parsed(self, tmp_path: Path) -> None:
        concerns_dir = tmp_path / "concerns"
        concerns_dir.mkdir()
        create_file(
            concerns_dir,
            "cs-bugs.md",
            _make_concern("CS Bugs", applies_to="**/*.cs"),
        )

        concerns = fr._read_concerns(concerns_dir, tmp_path)
        assert concerns[0]["applies_to"] == "**/*.cs"

    def test_no_applies_to_is_none(self, tmp_path: Path) -> None:
        concerns_dir = tmp_path / "concerns"
        concerns_dir.mkdir()
        create_file(concerns_dir, "general.md", _make_concern("General"))

        concerns = fr._read_concerns(concerns_dir, tmp_path)
        assert concerns[0]["applies_to"] is None

    def test_body_contains_content_after_frontmatter(self, tmp_path: Path) -> None:
        concerns_dir = tmp_path / "concerns"
        concerns_dir.mkdir()
        create_file(concerns_dir, "bugs.md", _make_concern("Bug Finder"))

        concerns = fr._read_concerns(concerns_dir, tmp_path)
        body = concerns[0]["body"]
        assert "# Bug Finder" in body
        assert "## Role" in body
        assert "## What to Check" in body

    def test_models_string_wrapped_in_list(self, tmp_path: Path) -> None:
        """If models is a plain string (not list), it gets wrapped."""
        concerns_dir = tmp_path / "concerns"
        concerns_dir.mkdir()
        # Manually craft a concern with models as plain string
        content = "---\ntype: concern\nmodels: opus\npriority: standard\n---\n# Test\nBody."
        create_file(concerns_dir, "test.md", content)

        concerns = fr._read_concerns(concerns_dir, tmp_path)
        assert concerns[0]["models"] == ["opus"]

    def test_display_name_from_heading(self, tmp_path: Path) -> None:
        concerns_dir = tmp_path / "concerns"
        concerns_dir.mkdir()
        create_file(concerns_dir, "bugs.md", _make_concern("Adversarial Bug Finder"))

        concerns = fr._read_concerns(concerns_dir, tmp_path)
        assert concerns[0]["display_name"] == "Adversarial Bug Finder"
        assert concerns[0]["name"] == "bugs"

    def test_display_name_fallback_to_stem(self, tmp_path: Path) -> None:
        concerns_dir = tmp_path / "concerns"
        concerns_dir.mkdir()
        content = "---\ntype: concern\nmodels: [opus]\n---\nNo heading here."
        create_file(concerns_dir, "my-concern.md", content)

        concerns = fr._read_concerns(concerns_dir, tmp_path)
        assert concerns[0]["display_name"] == "my-concern"


# ---------------------------------------------------------------------------
# _write_per_file_diffs
# ---------------------------------------------------------------------------


class TestWritePerFileDiffs:

    def test_creates_per_file_patches(self, tmp_path: Path) -> None:
        work_dir = tmp_path / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        diff = _make_diff(("src/Foo.cs", 5), ("src/Bar.cs", 3))
        result = fr._write_per_file_diffs(diff, work_dir)

        assert len(result) == 2
        assert "src/Foo.cs" in result
        assert "src/Bar.cs" in result

    def test_patch_content_is_correct(self, tmp_path: Path) -> None:
        work_dir = tmp_path / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        diff = _make_diff(("src/Foo.cs", 5))
        result = fr._write_per_file_diffs(diff, work_dir)

        patch_path = result["src/Foo.cs"]
        content = patch_path.read_text(encoding="utf-8")
        assert "diff --git" in content
        assert "src/Foo.cs" in content

    def test_filenames_sanitised(self, tmp_path: Path) -> None:
        """Forward slashes in paths are replaced with -- in filenames."""
        work_dir = tmp_path / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        diff = _make_diff(("src/deep/nested/File.cs", 3))
        result = fr._write_per_file_diffs(diff, work_dir)

        patch_path = result["src/deep/nested/File.cs"]
        assert patch_path.name == "src--deep--nested--File.cs.patch"

    def test_cleans_previous_diffs(self, tmp_path: Path) -> None:
        work_dir = tmp_path / ".agents" / "focused-review"
        diffs_dir = work_dir / "diffs"
        diffs_dir.mkdir(parents=True)
        # Write a stale file
        (diffs_dir / "old-file.patch").write_text("stale", encoding="utf-8")

        diff = _make_diff(("src/New.cs", 3))
        fr._write_per_file_diffs(diff, work_dir)

        remaining = list(diffs_dir.iterdir())
        assert len(remaining) == 1
        assert remaining[0].name == "src--New.cs.patch"

    def test_empty_diff_returns_empty(self, tmp_path: Path) -> None:
        work_dir = tmp_path / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        result = fr._write_per_file_diffs("", work_dir)
        assert result == {}


# ---------------------------------------------------------------------------
# _generate_concern_prompts
# ---------------------------------------------------------------------------


class TestGenerateConcernPrompts:

    def _concern(
        self,
        name: str = "bugs",
        models: list[str] | None = None,
        priority: str = "standard",
        applies_to: str | list[str] | None = None,
    ) -> dict[str, object]:
        return {
            "name": name,
            "display_name": name.title(),
            "models": models or ["opus"],
            "priority": priority,
            "applies_to": applies_to,
            "body": f"# {name.title()}\n## Role\nYou review {name}.",
            "path": f"review/concerns/{name}.md",
        }

    def test_generates_prompt_file(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = repo / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        concerns = [self._concern("bugs")]
        changed = ["src/Foo.cs"]

        entries = fr._generate_concern_prompts(concerns, changed, work_dir, repo)

        assert len(entries) == 1
        assert entries[0]["concern"] == "bugs"
        assert entries[0]["model"] == "opus"
        assert entries[0]["priority"] == "standard"

        prompt_path = repo / entries[0]["prompt_path"]
        assert prompt_path.exists()
        content = prompt_path.read_text(encoding="utf-8")
        assert "# Bugs" in content
        assert "src/Foo.cs" in content

    def test_multi_model_generates_multiple_prompts(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = repo / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        concerns = [self._concern("bugs", models=["opus", "codex"])]
        changed = ["src/Foo.cs"]

        entries = fr._generate_concern_prompts(concerns, changed, work_dir, repo)

        assert len(entries) == 2
        models = {e["model"] for e in entries}
        assert models == {"opus", "codex"}

        # Both prompt files exist
        for e in entries:
            assert (repo / e["prompt_path"]).exists()

    def test_applies_to_filters_files(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = repo / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        concerns = [self._concern("cs-bugs", applies_to="**/*.cs")]
        changed = ["src/Foo.cs", "src/script.py"]

        entries = fr._generate_concern_prompts(concerns, changed, work_dir, repo)
        assert len(entries) == 1

        prompt_path = repo / entries[0]["prompt_path"]
        content = prompt_path.read_text(encoding="utf-8")
        assert "src/Foo.cs" in content
        assert "src/script.py" not in content

    def test_applies_to_no_match_skips_concern(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = repo / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        concerns = [self._concern("cs-bugs", applies_to="**/*.cs")]
        changed = ["src/script.py"]

        entries = fr._generate_concern_prompts(concerns, changed, work_dir, repo)
        assert len(entries) == 0

    def test_applies_to_list_matches_multiple_patterns(self, tmp_path: Path) -> None:
        """List-valued applies_to matches files against any pattern."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = repo / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        concerns = [self._concern("web-bugs", applies_to=["**/*.cs", "**/*.py"])]
        changed = ["src/Foo.cs", "src/script.py", "docs/readme.md"]

        entries = fr._generate_concern_prompts(concerns, changed, work_dir, repo)
        assert len(entries) == 1

        prompt_path = repo / entries[0]["prompt_path"]
        content = prompt_path.read_text(encoding="utf-8")
        assert "src/Foo.cs" in content
        assert "src/script.py" in content
        assert "docs/readme.md" not in content

    def test_applies_to_list_no_match_skips_concern(self, tmp_path: Path) -> None:
        """List-valued applies_to with no matching files skips the concern."""
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = repo / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        concerns = [self._concern("code-bugs", applies_to=["**/*.cs", "**/*.py"])]
        changed = ["docs/readme.md"]

        entries = fr._generate_concern_prompts(concerns, changed, work_dir, repo)
        assert len(entries) == 0

    def test_multiple_concerns_generate_separate_prompts(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = repo / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        concerns = [
            self._concern("bugs", priority="high"),
            self._concern("security", priority="high"),
            self._concern("general"),
        ]
        changed = ["src/Foo.cs"]

        entries = fr._generate_concern_prompts(concerns, changed, work_dir, repo)
        assert len(entries) == 3
        concern_names = {e["concern"] for e in entries}
        assert concern_names == {"bugs", "security", "general"}

    def test_prompt_contains_diff_references(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = repo / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        concerns = [self._concern("bugs")]
        changed = ["src/Foo.cs"]

        entries = fr._generate_concern_prompts(concerns, changed, work_dir, repo)
        content = (repo / entries[0]["prompt_path"]).read_text(encoding="utf-8")
        assert "diffs/" in content
        assert "diff.patch" in content

    def test_full_scope_prompt_omits_diff_references(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = repo / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        concerns = [self._concern("bugs")]
        changed = ["src/Foo.cs"]

        entries = fr._generate_concern_prompts(
            concerns, changed, work_dir, repo, scope="full"
        )
        content = (repo / entries[0]["prompt_path"]).read_text(encoding="utf-8")
        assert "### Diffs" not in content
        assert "diffs/" not in content
        assert "diff.patch" not in content
        assert "full-repository review" in content
        assert "### Scope" in content

    def test_cleans_previous_prompts(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = repo / ".agents" / "focused-review"
        prompts_dir = work_dir / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "old--opus.md").write_text("stale", encoding="utf-8")

        concerns = [self._concern("bugs")]
        changed = ["src/Foo.cs"]

        fr._generate_concern_prompts(concerns, changed, work_dir, repo)

        remaining = list(prompts_dir.iterdir())
        assert len(remaining) == 1
        assert remaining[0].name == "bugs--opus.md"

    def test_prompt_paths_are_posix(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        work_dir = repo / ".agents" / "focused-review"
        work_dir.mkdir(parents=True)

        concerns = [self._concern("bugs")]
        changed = ["src/Foo.cs"]

        entries = fr._generate_concern_prompts(concerns, changed, work_dir, repo)
        for e in entries:
            assert "\\" not in e["prompt_path"]


# ---------------------------------------------------------------------------
# _resolve_config: concerns_dir and scaling
# ---------------------------------------------------------------------------


class TestResolveConfigConcerns:

    def test_defaults_include_concerns_dir_and_scaling(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        with patch.object(fr, "CONFIG_SCAN_LOCATIONS", ["nonexistent.json"]), \
             patch.object(fr, "CONFIG_USER_LOCATIONS", []):
            config = fr._resolve_config(str(repo))
        assert config["concerns_dir"] == "review/concerns/"
        assert config["scaling"] == "standard"

    def test_reads_concerns_dir_from_config(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        config_path = repo / ".claude" / "focused-review.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({
                "rules_dir": "review/",
                "concerns_dir": "my-concerns/",
            }),
            encoding="utf-8",
        )

        config = fr._resolve_config(str(repo))
        assert config["concerns_dir"] == "my-concerns/"

    def test_reads_scaling_from_config(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        config_path = repo / ".claude" / "focused-review.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({
                "rules_dir": "review/",
                "scaling": "thorough",
            }),
            encoding="utf-8",
        )

        config = fr._resolve_config(str(repo))
        assert config["scaling"] == "thorough"

    def test_backslash_in_concerns_dir_normalised(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        config_path = repo / ".claude" / "focused-review.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({
                "rules_dir": "review/",
                "concerns_dir": "review\\concerns",
            }),
            encoding="utf-8",
        )

        config = fr._resolve_config(str(repo))
        assert config["concerns_dir"] == "review/concerns"


# ---------------------------------------------------------------------------
# resolve-config subcommand: concerns_dir and scaling output
# ---------------------------------------------------------------------------


class TestResolveConfigSubcommandConcerns:

    def test_outputs_concerns_dir_and_scaling(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "review").mkdir()
        create_file(repo / "review", "rule.md", _make_rule("Rule"))

        with patch.object(fr, "CONFIG_SCAN_LOCATIONS", ["nonexistent.json"]), \
             patch.object(fr, "CONFIG_USER_LOCATIONS", []):
            with patch("sys.argv", [
                "focused-review", "resolve-config", "--repo", str(repo),
            ]):
                fr.main()

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["concerns_dir"] == "review/concerns/"
        assert result["scaling"] == "standard"

    def test_concerns_dir_gets_trailing_slash(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        config_path = repo / ".claude" / "focused-review.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({
                "rules_dir": "review/",
                "concerns_dir": "my-concerns",
            }),
            encoding="utf-8",
        )

        with patch("sys.argv", [
            "focused-review", "resolve-config", "--repo", str(repo),
        ]):
            fr.main()

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["concerns_dir"] == "my-concerns/"


# ---------------------------------------------------------------------------
# prepare-review end-to-end with concerns
# ---------------------------------------------------------------------------


class TestPrepareReviewWithConcerns:

    def _setup_repo(
        self,
        tmp_path: Path,
        *,
        with_concerns: bool = True,
    ) -> Path:
        """Create a repo with rules and optionally concerns."""
        repo = tmp_path / "repo"
        repo.mkdir()
        rules_dir = repo / "review"
        rules_dir.mkdir()
        create_file(rules_dir, "rule.md", _make_rule("Test Rule"))

        if with_concerns:
            concerns_dir = repo / "review" / "concerns"
            concerns_dir.mkdir()
            create_file(concerns_dir, "bugs.md", _make_concern("Bug Finder", priority="high"))
            create_file(concerns_dir, "security.md", _make_concern("Security Scanner", priority="high"))
            create_file(concerns_dir, "general.md", _make_concern("General Reviewer"))

        return repo

    def test_summary_includes_concern_counts(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = self._setup_repo(tmp_path)
        diff = _make_diff(("src/Foo.cs", 10))

        args = argparse.Namespace(repo=str(repo), scope="branch", rules_dir="review/")

        with patch.object(fr, "_run_git", side_effect=_mock_git_results(diff, ["src/Foo.cs"])):
            fr.prepare_review(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["concerns_total"] == 3
        assert summary["concern_prompts"] == 3

    def test_per_file_diffs_created(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = self._setup_repo(tmp_path)
        diff = _make_diff(("src/Foo.cs", 5), ("src/Bar.cs", 3))

        args = argparse.Namespace(repo=str(repo), scope="branch", rules_dir="review/")

        with patch.object(fr, "_run_git", side_effect=_mock_git_results(diff, ["src/Foo.cs", "src/Bar.cs"])):
            fr.prepare_review(args)

        diffs_dir = repo / ".agents" / "focused-review" / "diffs"
        assert diffs_dir.is_dir()
        patch_files = list(diffs_dir.glob("*.patch"))
        assert len(patch_files) == 2

    def test_concern_prompts_created(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = self._setup_repo(tmp_path)
        diff = _make_diff(("src/Foo.cs", 10))

        args = argparse.Namespace(repo=str(repo), scope="branch", rules_dir="review/")

        with patch.object(fr, "_run_git", side_effect=_mock_git_results(diff, ["src/Foo.cs"])):
            fr.prepare_review(args)

        prompts_dir = repo / ".agents" / "focused-review" / "prompts"
        assert prompts_dir.is_dir()
        prompt_files = sorted(f.name for f in prompts_dir.glob("*.md"))
        assert "bugs--opus.md" in prompt_files
        assert "security--opus.md" in prompt_files
        assert "general--opus.md" in prompt_files

    def test_concern_dispatch_json_created(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = self._setup_repo(tmp_path)
        diff = _make_diff(("src/Foo.cs", 10))

        args = argparse.Namespace(repo=str(repo), scope="branch", rules_dir="review/")

        with patch.object(fr, "_run_git", side_effect=_mock_git_results(diff, ["src/Foo.cs"])):
            fr.prepare_review(args)

        dispatch_path = repo / ".agents" / "focused-review" / "concern-dispatch.json"
        assert dispatch_path.exists()
        dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
        assert len(dispatch) == 3
        concern_names = {d["concern"] for d in dispatch}
        assert concern_names == {"bugs", "security", "general"}

    def test_empty_diff_includes_concern_fields(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = self._setup_repo(tmp_path)

        args = argparse.Namespace(repo=str(repo), scope="branch", rules_dir="review/")

        with patch.object(fr, "_run_git", side_effect=_mock_git_results("", [])):
            fr.prepare_review(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["concerns_total"] == 0
        assert summary["concern_prompts"] == 0

    def test_no_project_concerns_falls_back_to_builtin(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When no project concerns exist, built-in defaults are used."""
        repo = self._setup_repo(tmp_path, with_concerns=False)
        diff = _make_diff(("src/Foo.cs", 10))

        args = argparse.Namespace(repo=str(repo), scope="branch", rules_dir="review/")

        with patch.object(fr, "_run_git", side_effect=_mock_git_results(diff, ["src/Foo.cs"])):
            fr.prepare_review(args)

        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        # Built-in concerns: bugs, security, architecture, general
        assert summary["concerns_total"] == 4
        assert summary["concern_prompts"] == 4

    def test_builtin_concern_prompts_contain_body(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Built-in concern prompts include the concern body text."""
        repo = self._setup_repo(tmp_path, with_concerns=False)
        diff = _make_diff(("src/Foo.cs", 10))

        args = argparse.Namespace(repo=str(repo), scope="branch", rules_dir="review/")

        with patch.object(fr, "_run_git", side_effect=_mock_git_results(diff, ["src/Foo.cs"])):
            fr.prepare_review(args)

        prompts_dir = repo / ".agents" / "focused-review" / "prompts"
        bugs_prompt = prompts_dir / "bugs--opus.md"
        assert bugs_prompt.exists()
        content = bugs_prompt.read_text(encoding="utf-8")
        assert "Bug Finder" in content or "bug finder" in content.lower()

    def test_full_scope_no_per_file_diffs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Full scope doesn't write per-file diffs (no diff text)."""
        repo = self._setup_repo(tmp_path)
        # For full scope, _all_tracked_files is called
        ls_mock = MagicMock()
        ls_mock.returncode = 0
        ls_mock.stdout = "src/Foo.cs\nsrc/Bar.cs\n"

        args = argparse.Namespace(repo=str(repo), scope="full", rules_dir="review/")

        with patch.object(fr, "_run_git", return_value=ls_mock):
            fr.prepare_review(args)

        # diffs dir should not be created (no diff text)
        diffs_dir = repo / ".agents" / "focused-review" / "diffs"
        assert not diffs_dir.exists()

        # But concerns should still be processed
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["concerns_total"] == 3
        assert summary["concern_prompts"] == 3


# ---------------------------------------------------------------------------
# Built-in concerns validation
# ---------------------------------------------------------------------------


class TestBuiltinConcerns:

    def test_builtin_concerns_dir_exists(self) -> None:
        """The built-in concerns directory exists in the package."""
        assert fr.BUILTIN_CONCERNS_DIR.is_dir()

    def test_builtin_concerns_readable(self) -> None:
        """All built-in concerns can be read and parsed."""
        concerns = fr._read_concerns(fr.BUILTIN_CONCERNS_DIR, fr.BUILTIN_CONCERNS_DIR.parent)
        assert len(concerns) >= 4
        names = {c["name"] for c in concerns}
        assert "bugs" in names
        assert "security" in names
        assert "architecture" in names
        assert "general" in names

    def test_builtin_concerns_have_valid_metadata(self) -> None:
        """Each built-in concern has required metadata fields."""
        concerns = fr._read_concerns(fr.BUILTIN_CONCERNS_DIR, fr.BUILTIN_CONCERNS_DIR.parent)
        for c in concerns:
            assert isinstance(c["models"], list)
            assert len(c["models"]) > 0
            assert c["priority"] in ("high", "standard")
            assert c["body"]
