#!/usr/bin/env python3
"""Focused review helper — deterministic operations for the focused-review plugin."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INSTRUCTION_PATTERNS: list[str] = [
    "CLAUDE.md",
    "*/CLAUDE.md",
    "*/*/CLAUDE.md",
    "GEMINI.md",
    "AGENTS.md",
    "agents.md",
    ".github/copilot-instructions.md",
    ".github/instructions/**/*.instructions.md",
    ".cursor/rules/*.md",
    ".cursor/rules/*.mdc",
    ".cursorrules",
    ".windsurfrules",
    ".clinerules",
]

DIFF_TARGET_CHUNK_LINES = 3000

CONFIG_FILENAME = "focused-review.json"
CONFIG_SCAN_LOCATIONS: list[str] = [
    os.path.join(".claude", CONFIG_FILENAME),
    CONFIG_FILENAME,
    os.path.join(".github", CONFIG_FILENAME),
]
CONFIG_USER_LOCATIONS: list[str] = [
    os.path.join("~", ".claude", CONFIG_FILENAME),
    os.path.join("~", ".copilot", CONFIG_FILENAME),
]

DEFAULT_RULES_DIR = "review/"


# ---------------------------------------------------------------------------
# Config file resolution
# ---------------------------------------------------------------------------


def _resolve_config(repo: str = ".") -> dict[str, object]:
    """Scan config file locations and return the merged config values.

    Scan order (first match wins):
      1. .claude/focused-review.json  (project)
      2. focused-review.json          (repo root)
      3. .github/focused-review.json  (GitHub convention)
      4. ~/.claude/focused-review.json (user-wide, Claude Code)
      5. ~/.copilot/focused-review.json (user-wide, Copilot CLI)

    Returns a dict with ``rules_dir`` (str) and ``sources`` (list[str]).
    Falls back to defaults if no config file is found.
    """
    repo_path = Path(repo).resolve()

    candidates = [repo_path / loc for loc in CONFIG_SCAN_LOCATIONS] + [
        Path(os.path.expanduser(loc)) for loc in CONFIG_USER_LOCATIONS
    ]

    for candidate in candidates:
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                raw = data.get("rules_dir", DEFAULT_RULES_DIR)
                rules_dir = raw.replace("\\", "/")
                sources: list[str] = data.get("sources", [])
                return {"rules_dir": rules_dir, "sources": sources}
            except (json.JSONDecodeError, AttributeError):
                pass

    return {"rules_dir": DEFAULT_RULES_DIR, "sources": []}


def _resolve_rules_dir(repo: str = ".") -> str:
    """Scan config file locations and return the rules_dir value, or the default."""
    return str(_resolve_config(repo)["rules_dir"])


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _posix(path: Path, relative_to: Path | None = None) -> str:
    """Return a forward-slash path string, optionally relative to *relative_to*."""
    if relative_to is not None:
        try:
            path = path.relative_to(relative_to)
        except ValueError:
            pass  # outside repo — keep absolute
    return path.as_posix()


# ---------------------------------------------------------------------------
# Discover
# ---------------------------------------------------------------------------


def _find_instruction_files(repo: Path) -> list[Path]:
    """Scan *repo* for known instruction-file patterns."""
    found: list[Path] = []
    for pattern in INSTRUCTION_PATTERNS:
        for match in sorted(repo.glob(pattern)):
            if match.is_file():
                found.append(match)

    # COPILOT_CUSTOM_INSTRUCTIONS_DIRS env var
    custom_dirs = os.environ.get("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", "")
    if custom_dirs:
        for dir_str in custom_dirs.split(os.pathsep):
            dir_str = dir_str.strip()
            if not dir_str:
                continue
            d = Path(dir_str)
            if d.is_dir():
                for f in sorted(d.rglob("*")):
                    if f.is_file():
                        found.append(f)
    return found


def _resolve_and_deduplicate(paths: list[Path]) -> list[Path]:
    """Resolve symlinks and remove duplicate resolved paths (stable order)."""
    seen: set[Path] = set()
    result: list[Path] = []
    for p in paths:
        resolved = p.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def resolve_config(args: argparse.Namespace) -> None:
    """Print resolved config values as JSON (rules_dir and sources)."""
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(
            json.dumps({"error": f"Repository path not found: {_posix(repo)}"}),
            file=sys.stderr,
        )
        sys.exit(1)

    config = _resolve_config(str(repo))
    rules_dir = str(config["rules_dir"])
    if not rules_dir.endswith("/"):
        rules_dir += "/"
    config["rules_dir"] = rules_dir
    print(json.dumps(config))


def discover(args: argparse.Namespace) -> None:
    """Find instruction files in the repository and output JSON list."""
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(
            json.dumps({"error": f"Repository path not found: {_posix(repo)}"}),
            file=sys.stderr,
        )
        sys.exit(1)

    files = _resolve_and_deduplicate(_find_instruction_files(repo))

    output: list[str] = [_posix(f, relative_to=repo) for f in files]
    print(json.dumps(output, indent=2))


# ---------------------------------------------------------------------------
# Frontmatter parser (stdlib-only, no PyYAML dependency)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?\r?\n)---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def _parse_frontmatter(content: str) -> tuple[dict[str, object], str]:
    """Parse simple YAML-like ``key: value`` frontmatter.

    Returns ``(metadata, body)`` where *body* is everything after the
    closing ``---``.  Only flat scalars are supported (string, bool).
    """
    metadata: dict[str, object] = {}
    body = content

    m = _FRONTMATTER_RE.match(content)
    if not m:
        return metadata, body

    body = content[m.end() :]
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        colon = line.find(":")
        if colon == -1:
            continue
        key = line[:colon].strip()
        value: str = line[colon + 1 :].strip()
        # strip surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # coerce booleans
        if value.lower() == "true":
            metadata[key] = True
        elif value.lower() == "false":
            metadata[key] = False
        else:
            metadata[key] = value
    return metadata, body


# ---------------------------------------------------------------------------
# Glob matching  (applies-to filtering)
# ---------------------------------------------------------------------------


def _file_matches_glob(filepath: str, glob_pattern: str) -> bool:
    """Return True if *filepath* matches *glob_pattern* (``**`` aware).

    Handles the common case where ``**/*.ext`` should also match files
    at the repository root (``PurePosixPath.match`` requires at least
    one directory segment for ``**/``).
    """
    path = PurePosixPath(filepath.replace("\\", "/"))
    if path.match(glob_pattern):
        return True
    # **/ means "zero or more dirs" in user intent — retry under a synthetic parent
    if "**" in glob_pattern:
        return PurePosixPath("_" / path).match(glob_pattern)
    return False


# ---------------------------------------------------------------------------
# Rule reader
# ---------------------------------------------------------------------------


def _read_rules(rules_dir: Path, repo: Path) -> list[dict[str, object]]:
    """Read ``*.md`` rule files from *rules_dir* and parse their frontmatter."""
    rules: list[dict[str, object]] = []
    if not rules_dir.is_dir():
        return rules

    for rule_file in sorted(rules_dir.glob("*.md")):
        if not rule_file.is_file():
            continue
        content = rule_file.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(content)

        # rule name from first heading
        heading = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        name = heading.group(1).strip() if heading else rule_file.stem

        rules.append(
            {
                "path": _posix(rule_file, relative_to=repo),
                "name": name,
                "model": meta.get("model", "haiku"),
                "autofix": meta.get("autofix", False),
                "applies_to": meta.get("applies-to"),
                "source": meta.get("source"),
            }
        )
    return rules


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------


def _run_git(cmd: list[str], repo: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd, cwd=repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print(
            json.dumps({"error": "git is not installed or not on PATH"}),
            file=sys.stderr,
        )
        sys.exit(1)


def _get_diff(scope: str, repo: Path) -> tuple[str, list[str]]:
    """Run ``git diff`` for *scope* and return ``(diff_text, changed_files)``."""
    scope_args = {
        "branch": ["origin/main...HEAD"],
        "commit": ["HEAD~1..HEAD"],
        "staged": ["--cached"],
        "unstaged": [],
    }
    if scope not in scope_args:
        raise ValueError(f"Unknown diff scope: {scope}")

    base = ["git", "--no-pager", "diff", "--no-color"]
    extra = scope_args[scope]

    diff_result = _run_git(base + extra, repo)
    if diff_result.returncode != 0:
        print(
            json.dumps({"error": f"git diff failed: {diff_result.stderr.strip()}"}),
            file=sys.stderr,
        )
        sys.exit(1)

    names_result = _run_git(base + ["--name-only"] + extra, repo)
    changed = [f for f in names_result.stdout.splitlines() if f.strip()]

    return diff_result.stdout, changed


def _changed_files_from_diff(diff_text: str) -> list[str]:
    """Extract the ``b/`` side file paths from a unified diff."""
    return [m.group(1) for m in re.finditer(r"^diff --git a/.+? b/(.+?)$", diff_text, re.MULTILINE)]


def _all_tracked_files(repo: Path) -> list[str]:
    """``git ls-files`` — for full-codebase scope."""
    result = _run_git(["git", "--no-pager", "ls-files"], repo)
    if result.returncode != 0:
        print(
            json.dumps({"error": f"git ls-files failed: {result.stderr.strip()}"}),
            file=sys.stderr,
        )
        sys.exit(1)
    return [f for f in result.stdout.splitlines() if f.strip()]


# ---------------------------------------------------------------------------
# Diff chunking
# ---------------------------------------------------------------------------


def _split_diff_by_file(diff_text: str) -> list[tuple[str, str]]:
    """Split a unified diff into ``(filename, section)`` tuples."""
    parts = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
    file_diffs: list[tuple[str, str]] = []
    for part in parts:
        part = part.rstrip("\n")
        if not part:
            continue
        m = re.match(r"^diff --git a/.+? b/(.+?)$", part, re.MULTILINE)
        if m:
            file_diffs.append((m.group(1), part))
    return file_diffs


def _write_chunks(
    diff_text: str,
    work_dir: Path,
    target_lines: int = DIFF_TARGET_CHUNK_LINES,
) -> list[Path]:
    """Write diff to disk, chunking at file boundaries when large.

    Always writes ``diff.patch`` (full diff).  When the diff exceeds
    *target_lines* it also writes numbered chunks under ``chunks/``.
    Returns the list of chunk paths used for dispatch.
    """
    # Always persist full diff
    diff_path = work_dir / "diff.patch"
    diff_path.write_text(diff_text, encoding="utf-8")

    total_lines = len(diff_text.splitlines())
    if total_lines <= target_lines:
        return [diff_path]

    # Need to chunk
    file_diffs = _split_diff_by_file(diff_text)
    if not file_diffs:
        return [diff_path]

    chunks_dir = work_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    # clean old chunks
    for old in chunks_dir.iterdir():
        if old.is_file():
            old.unlink()

    chunk_paths: list[Path] = []
    current_parts: list[str] = []
    current_count = 0
    idx = 1

    for _filename, section in file_diffs:
        section_lines = len(section.splitlines())

        if current_count > 0 and current_count + section_lines > target_lines:
            p = chunks_dir / f"diff-{idx:03d}.patch"
            p.write_text("\n".join(current_parts), encoding="utf-8")
            chunk_paths.append(p)
            idx += 1
            current_parts = []
            current_count = 0

        current_parts.append(section)
        current_count += section_lines

    if current_parts:
        p = chunks_dir / f"diff-{idx:03d}.patch"
        p.write_text("\n".join(current_parts), encoding="utf-8")
        chunk_paths.append(p)

    return chunk_paths


# ---------------------------------------------------------------------------
# Dispatch planning
# ---------------------------------------------------------------------------


def _rule_matches_files(rule: dict[str, object], files: list[str]) -> bool:
    """Does the rule's ``applies_to`` glob match any file in *files*?"""
    pattern = rule.get("applies_to")
    if pattern is None:
        return True  # no constraint → matches everything
    if not isinstance(pattern, str):
        return False
    return any(_file_matches_glob(f, pattern) for f in files)


def _chunk_files(chunk_path: Path) -> list[str]:
    """Extract file paths mentioned in a diff chunk."""
    return _changed_files_from_diff(chunk_path.read_text(encoding="utf-8"))


def _build_dispatch(
    rules: list[dict[str, object]],
    chunk_paths: list[Path],
    changed_files: list[str],
    scope: str,
    repo: Path,
) -> list[dict[str, object]]:
    """Build the dispatch plan: one entry per (rule, chunk) pair."""
    dispatch: list[dict[str, object]] = []
    total_chunks = len(chunk_paths)

    if scope == "full":
        for rule in rules:
            if _rule_matches_files(rule, changed_files):
                dispatch.append(
                    {
                        "rule_path": rule["path"],
                        "model": rule["model"],
                        "autofix": rule["autofix"],
                        "chunk_path": None,
                        "chunk_index": None,
                        "total_chunks": None,
                        "scope": scope,
                    }
                )
        return dispatch

    chunk_file_cache: dict[str, list[str]] = {}
    for cp in chunk_paths:
        chunk_file_cache[str(cp)] = _chunk_files(cp)

    for rule in rules:
        if not _rule_matches_files(rule, changed_files):
            continue
        pattern = rule.get("applies_to")
        for i, cp in enumerate(chunk_paths):
            if pattern is not None and isinstance(pattern, str):
                cf = chunk_file_cache[str(cp)]
                if not any(_file_matches_glob(f, pattern) for f in cf):
                    continue
            dispatch.append(
                {
                    "rule_path": rule["path"],
                    "model": rule["model"],
                    "autofix": rule["autofix"],
                    "chunk_path": _posix(cp, relative_to=repo),
                    "chunk_index": i + 1,
                    "total_chunks": total_chunks,
                    "scope": scope,
                }
            )
    return dispatch


# ---------------------------------------------------------------------------
# Subcommand: prepare-review
# ---------------------------------------------------------------------------


def prepare_review(args: argparse.Namespace) -> None:
    """Read committed rules, generate diff, filter, chunk, produce dispatch plan."""
    repo = Path(args.repo).resolve()
    raw_rules_dir = args.rules_dir if args.rules_dir is not None else _resolve_rules_dir(str(repo))
    rules_dir = repo / Path(raw_rules_dir.replace("\\", "/"))
    scope: str = args.scope

    rules = _read_rules(rules_dir, repo)
    if not rules:
        print(
            json.dumps(
                {"error": "No review rules found", "rules_dir": _posix(rules_dir, repo)}
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    work_dir = repo / ".agents" / "focused-review"
    work_dir.mkdir(parents=True, exist_ok=True)

    # -- determine changed files & chunks --------------------------------

    if scope == "full":
        changed_files = _all_tracked_files(repo)
        chunk_paths: list[Path] = []
    else:
        diff_text, changed_files = _get_diff(scope, repo)
        if not diff_text.strip():
            summary = {
                "dispatch_path": None,
                "agents": 0,
                "scope": scope,
                "rules_total": len(rules),
                "rules_matched": 0,
                "changed_files": 0,
                "chunks": 0,
            }
            print(json.dumps(summary))
            return
        chunk_paths = _write_chunks(diff_text, work_dir)

    # write changed-files list
    (work_dir / "changed-files.txt").write_text(
        "\n".join(changed_files), encoding="utf-8"
    )

    # -- dispatch plan ---------------------------------------------------

    dispatch = _build_dispatch(rules, chunk_paths, changed_files, scope, repo)

    if getattr(args, "no_autofix", False):
        dispatch = [{**entry, "autofix": False} for entry in dispatch]

    dispatch_path = work_dir / "dispatch.json"
    dispatch_path.write_text(json.dumps(dispatch, indent=2), encoding="utf-8")

    summary = {
        "dispatch_path": _posix(dispatch_path, relative_to=repo),
        "agents": len(dispatch),
        "scope": scope,
        "rules_total": len(rules),
        "rules_matched": len({d["rule_path"] for d in dispatch}),
        "changed_files": len(changed_files),
    }
    if scope != "full":
        summary["chunks"] = len(chunk_paths)

    print(json.dumps(summary))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="focused-review",
        description="Deterministic helpers for the focused-review plugin",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # resolve-config subcommand
    config_parser = subparsers.add_parser(
        "resolve-config",
        help="Resolve config values (rules_dir and sources) from config file scan",
    )
    config_parser.add_argument(
        "--repo",
        default=".",
        help="Path to the repository root (default: current directory)",
    )
    config_parser.set_defaults(func=resolve_config)

    # discover subcommand
    discover_parser = subparsers.add_parser(
        "discover",
        help="Find instruction files in the repository",
    )
    discover_parser.add_argument(
        "--repo",
        default=".",
        help="Path to the repository root (default: current directory)",
    )
    discover_parser.set_defaults(func=discover)

    # prepare-review subcommand
    prepare_parser = subparsers.add_parser(
        "prepare-review",
        help="Read committed rules, generate diff, chunk, produce dispatch plan",
    )
    prepare_parser.add_argument(
        "--repo",
        default=".",
        help="Path to the repository root (default: current directory)",
    )
    prepare_parser.add_argument(
        "--scope",
        default="branch",
        choices=["branch", "commit", "staged", "unstaged", "full"],
        help="Diff scope (default: branch)",
    )
    prepare_parser.add_argument(
        "--rules-dir",
        default=None,
        help="Directory containing review rule files (default: resolved from focused-review.json, then review/)",
    )
    prepare_parser.add_argument(
        "--no-autofix",
        action="store_true",
        default=False,
        help="Force all rules to report-only mode, ignoring per-rule autofix settings",
    )
    prepare_parser.set_defaults(func=prepare_review)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
