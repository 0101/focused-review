#!/usr/bin/env python3
"""Focused review helper — deterministic operations for the focused-review plugin."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
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

# ---------------------------------------------------------------------------
# records.json envelope schema (Phase 2)
#
# The reporter agent serializes the final compiled review as a single JSON
# envelope ({run_dir}/records.json). Python validates it mechanically — it never
# interprets prose — and downstream render-review (Phase 3) templates it into
# review.md / terminal / canvas. See docs/spec/canvas-review-report.md.
# ---------------------------------------------------------------------------

RECORDS_SCHEMA_VERSION = 1

# Enum values validated strictly because the renderer depends on them:
# severity → CSS severity class, verdict → section grouping, fix_complexity →
# ordinal sort, type → "found by" tag styling. Sourced from the discovery /
# assessor / consolidator agent contracts.
VALID_SEVERITIES = ("Critical", "High", "Medium", "Low")
VALID_FIX_COMPLEXITIES = ("quickfix", "moderate", "complex")
VALID_VERDICTS = ("Confirmed", "Questionable", "Invalid")
VALID_FINDING_TYPES = ("rule", "concern", "mixed")
# Mirrors the prepare-review --scope choices.
VALID_RUN_SCOPES = ("branch", "commit", "staged", "unstaged", "full")
# Findings that are shown in the numbered (actionable) sections must carry a
# positional display_number; Invalid findings render in a separate table keyed
# by assessment id, so their display_number is optional.
_VERDICTS_REQUIRING_DISPLAY_NUMBER = ("Confirmed", "Questionable")

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
DEFAULT_CONCERNS_DIR = "review/concerns/"
DEFAULT_BASE_BRANCH = "origin/main"
BUILTIN_CONCERNS_DIR = Path(__file__).resolve().parent.parent / "defaults" / "concerns"
CONCERN_FRAMEWORK_PATH = Path(__file__).resolve().parent.parent / "defaults" / "concern-framework.md"

COPILOT_CMD = os.environ.get("COPILOT_CMD", "copilot")
CONCERN_TIMEOUT_SECS = int(os.environ.get("CONCERN_TIMEOUT", "1200"))
CONCERN_RETRIES = int(os.environ.get("CONCERN_RETRIES", "2"))
CONCERN_MAX_WORKERS = int(os.environ.get("CONCERN_MAX_WORKERS", "4"))

# Shorthand model names used in concern files → full CLI model identifiers.
# Unknown names pass through unchanged so users can specify full names directly.
MODEL_MAP: dict[str, str] = {
    "opus": "claude-opus-4.6-1m",
    "sonnet": "claude-sonnet-4.6",
    "haiku": "claude-haiku-4.5",
    "gpt": "gpt-5.5",
    "codex": "gpt-5.3-codex",
    "gemini": "gemini-3-pro-preview",
}


def _resolve_model(shorthand: str) -> str:
    """Map a shorthand model name to the full CLI identifier.

    Lookup is case-insensitive so that concern files using ``Opus`` or ``OPUS``
    resolve identically to ``opus``.

    Returns the full name from :data:`MODEL_MAP` if the shorthand is a known
    alias, otherwise returns *shorthand* unchanged (passthrough for full names
    or any future model identifiers the user specifies directly).
    """
    return MODEL_MAP.get(shorthand.lower(), shorthand)


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

    Returns a dict with ``rules_dir`` (str), ``sources`` (list[str]),
    ``concerns_dir`` (str), and
    ``config_file`` (str | None, the path that was loaded).
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
                concerns_raw = data.get("concerns_dir", DEFAULT_CONCERNS_DIR)
                concerns_dir = str(concerns_raw).replace("\\", "/")
                base_branch_raw = data.get("base_branch", DEFAULT_BASE_BRANCH)
                base_branch = (
                    str(base_branch_raw).strip()
                    if isinstance(base_branch_raw, str) and str(base_branch_raw).strip()
                    else DEFAULT_BASE_BRANCH
                )
                config_path = str(candidate).replace("\\", "/")
                return {
                    "rules_dir": rules_dir,
                    "sources": sources,
                    "concerns_dir": concerns_dir,
                    "base_branch": base_branch,
                    "config_file": config_path,
                }
            except (json.JSONDecodeError, AttributeError):
                pass

    return {
        "rules_dir": DEFAULT_RULES_DIR,
        "sources": [],
        "concerns_dir": DEFAULT_CONCERNS_DIR,
        "base_branch": DEFAULT_BASE_BRANCH,
        "config_file": None,
    }


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
    """Print resolved config values as JSON.

    Output includes ``rules_dir``, ``sources``, ``concerns_dir``,
    ``script_path``, and ``defaults_dir`` so that the
    orchestrator (SKILL.md) can resolve all paths in a single call
    without platform-specific features.
    """
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
    concerns_dir = str(config["concerns_dir"])
    if not concerns_dir.endswith("/"):
        concerns_dir += "/"
    config["concerns_dir"] = concerns_dir

    script_file = Path(__file__).resolve()
    config["script_path"] = str(script_file).replace("\\", "/")
    defaults_dir = str(script_file.parent.parent / "defaults").replace("\\", "/")
    if not defaults_dir.endswith("/"):
        defaults_dir += "/"
    config["defaults_dir"] = defaults_dir

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


def _split_yaml_list(s: str) -> list[str]:
    """Split a YAML inline list body on commas, respecting ``{}`` brace groups.

    ``"opus, codex"``         → ``["opus", "codex"]``
    ``"**/*.{cs,fs}, !Tests"`` → ``["**/*.{cs,fs}", "!Tests"]``
    """
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(s):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(depth - 1, 0)
        elif ch == "," and depth == 0:
            parts.append(s[start:i])
            start = i + 1
    parts.append(s[start:])
    return parts


def _parse_frontmatter(content: str) -> tuple[dict[str, object], str]:
    """Parse simple YAML-like ``key: value`` frontmatter.

    Returns ``(metadata, body)`` where *body* is everything after the
    closing ``---``.  Supports flat scalars (string, bool) and inline
    YAML lists (``[item1, item2]``).
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
        # detect inline YAML list [item1, item2]
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1]
            metadata[key] = [v.strip().strip("'\"") for v in _split_yaml_list(inner) if v.strip()]
        # coerce booleans
        elif value.lower() == "true":
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

    Negation patterns (``!**/*Tests*.cs``) invert the match: returns
    True for files that do NOT match the inner pattern.

    Uses ``PurePosixPath.full_match`` (Python 3.13+) for correct
    recursive ``**`` handling, with ``fnmatch`` fallback for older
    Python where ``PurePosixPath.match`` treats ``**`` as ``*``.
    """
    if glob_pattern.startswith("!"):
        return not _file_matches_glob(filepath, glob_pattern[1:])
    filepath = filepath.replace("\\", "/")

    # full_match (Python 3.13+) handles ** as zero-or-more directories
    _full_match = getattr(PurePosixPath, "full_match", None)
    if _full_match is not None:
        return bool(PurePosixPath(filepath).full_match(glob_pattern))

    # Fallback: fnmatch handles ** for depth >= 1
    if fnmatch.fnmatch(filepath, glob_pattern):
        return True
    # ** means "zero or more dirs" — collapse **/ for depth-0 match
    if "**/" in glob_pattern:
        return fnmatch.fnmatch(filepath, glob_pattern.replace("**/", "", 1))
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
                "model": meta.get("model", "inherit"),
                "applies_to": meta.get("applies-to"),
                "source": meta.get("source"),
            }
        )
    return rules


# ---------------------------------------------------------------------------
# Concern reader
# ---------------------------------------------------------------------------


def _read_concerns(concerns_dir: Path, repo: Path) -> list[dict[str, object]]:
    """Read ``*.md`` concern files from *concerns_dir* and parse frontmatter.

    Only files with ``type: concern`` in frontmatter are included.
    Returns a list of dicts with keys: ``path``, ``name``, ``display_name``,
    ``models``, ``priority``, ``applies_to``, ``body``.
    """
    concerns: list[dict[str, object]] = []
    if not concerns_dir.is_dir():
        return concerns

    for concern_file in sorted(concerns_dir.glob("*.md")):
        if not concern_file.is_file():
            continue
        content = concern_file.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(content)

        if meta.get("type") != "concern":
            continue

        heading = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        display_name = heading.group(1).strip() if heading else concern_file.stem

        models = meta.get("models", ["opus"])
        if isinstance(models, str):
            models = [models]

        concerns.append(
            {
                "path": _posix(concern_file, relative_to=repo),
                "name": concern_file.stem,
                "display_name": display_name,
                "models": models,
                "priority": meta.get("priority", "standard"),
                "applies_to": meta.get("applies-to"),
                "body": body,
            }
        )
    return concerns


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


def _get_diff(
    scope: str, repo: Path, pathspecs: list[str] | None = None,
    *, base_branch: str = DEFAULT_BASE_BRANCH,
) -> tuple[str, list[str]]:
    """Run ``git diff`` for *scope* and return ``(diff_text, changed_files)``.

    When *pathspecs* is provided, the diff is restricted to matching paths.
    *base_branch* controls the ref used for ``branch`` scope (default: ``origin/main``).
    """
    scope_args = {
        "branch": [f"{base_branch}...HEAD"],
        "commit": ["HEAD~1..HEAD"],
        "staged": ["--cached"],
        "unstaged": [],
    }
    if scope not in scope_args:
        raise ValueError(f"Unknown diff scope: {scope}")

    base = ["git", "--no-pager", "diff", "--no-color"]
    extra = scope_args[scope]
    path_args = ["--"] + pathspecs if pathspecs else []

    diff_result = _run_git(base + extra + path_args, repo)
    if diff_result.returncode != 0:
        print(
            json.dumps({"error": f"git diff failed: {diff_result.stderr.strip()}"}),
            file=sys.stderr,
        )
        sys.exit(1)

    names_result = _run_git(base + ["--name-only"] + extra + path_args, repo)
    changed = [f for f in names_result.stdout.splitlines() if f.strip()]

    return diff_result.stdout, changed


def _changed_files_from_diff(diff_text: str) -> list[str]:
    """Extract the ``b/`` side file paths from a unified diff."""
    return [m.group(1) for m in re.finditer(r"^diff --git a/.+? b/(.+?)$", diff_text, re.MULTILINE)]


def _make_pathspecs(paths: list[str]) -> list[str]:
    """Convert user-provided path filters into git pathspec arguments.

    Plain paths pass through unchanged (git treats them as prefix matches).
    Paths with glob characters (``*``, ``?``, ``[``) are wrapped in
    ``:(glob)`` so git interprets them correctly.
    """
    specs: list[str] = []
    for p in paths:
        p = p.replace("\\", "/")
        if any(c in p for c in ("*", "?", "[")):
            specs.append(f":(glob){p}")
        else:
            specs.append(p)
    return specs


def _all_tracked_files(repo: Path, pathspecs: list[str] | None = None) -> list[str]:
    """``git ls-files`` — for full-codebase scope.

    When *pathspecs* is provided, only files matching at least one spec
    are returned (filtering is done by git itself).
    """
    cmd = ["git", "--no-pager", "ls-files"]
    if pathspecs:
        cmd += ["--"] + pathspecs
    result = _run_git(cmd, repo)
    if result.returncode != 0:
        print(
            json.dumps({"error": f"git ls-files failed: {result.stderr.strip()}"}),
            file=sys.stderr,
        )
        sys.exit(1)
    return [f for f in result.stdout.splitlines() if f.strip()]


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------


def _clean_dir(directory: Path) -> None:
    """Remove all files from *directory*, leaving subdirectories intact."""
    for old in directory.iterdir():
        if old.is_file():
            old.unlink()


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
    _clean_dir(chunks_dir)

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
# Per-file diffs and concern prompts
# ---------------------------------------------------------------------------


def _write_per_file_diffs(diff_text: str, work_dir: Path) -> dict[str, Path]:
    """Split a unified diff into per-file patches under ``work_dir/diffs/``.

    Returns a mapping from original file path to the written patch path.
    Filenames are sanitised by replacing ``/`` with ``--``.
    """
    diffs_dir = work_dir / "diffs"
    diffs_dir.mkdir(parents=True, exist_ok=True)

    # clean previous run
    _clean_dir(diffs_dir)

    file_diffs = _split_diff_by_file(diff_text)
    result: dict[str, Path] = {}
    for filename, section in file_diffs:
        safe_name = filename.replace("/", "--").replace("\\", "--")
        patch_path = diffs_dir / f"{safe_name}.patch"
        patch_path.write_text(section, encoding="utf-8")
        result[filename] = patch_path
    return result


def _generate_concern_prompts(
    concerns: list[dict[str, object]],
    changed_files: list[str],
    work_dir: Path,
    repo: Path,
    *,
    scope: str = "branch",
) -> list[dict[str, str]]:
    """Generate one prompt file per ``(concern × model)`` pair.

    Each prompt wraps the concern body with the concern-runner framework
    (generic instructions, output format, phase awareness) and appends
    review context (changed files list + diff locations).

    Returns a list of dispatch entries suitable for ``concern-dispatch.json``.
    """
    prompts_dir = work_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    # clean previous run
    _clean_dir(prompts_dir)

    # Load the generic concern framework template
    framework = ""
    if CONCERN_FRAMEWORK_PATH.exists():
        framework = CONCERN_FRAMEWORK_PATH.read_text(encoding="utf-8")

    prompt_entries: list[dict[str, str]] = []

    for concern in concerns:
        applies_to = concern.get("applies_to")
        patterns = _normalize_patterns(applies_to)
        if patterns:
            relevant_files = [
                f for f in changed_files
                if any(_file_matches_glob(f, p) for p in patterns)
            ]
        else:
            relevant_files = list(changed_files)

        if not relevant_files:
            continue

        body: str = str(concern.get("body", ""))
        models: list[str] = list(concern.get("models", ["opus"]))  # type: ignore[arg-type]

        # Build the combined prompt: framework wrapping concern body
        if framework:
            combined_body = framework.replace("{concern_body}", body.strip())
        else:
            combined_body = body.strip()

        for model in models:
            prompt_name = f"{concern['name']}--{model}"
            prompt_path = prompts_dir / f"{prompt_name}.md"

            lines = [
                combined_body,
                "",
                "---",
                "",
                "## Review Context",
                "",
                "### Changed Files",
                "",
            ]
            for f in relevant_files:
                lines.append(f"- `{f}`")

            if scope == "full":
                lines.extend([
                    "",
                    "### Scope",
                    "",
                    "This is a **full-repository review** (no diff). Examine the listed files in their entirety.",
                ])
            else:
                diffs_rel = _posix(work_dir / "diffs", relative_to=repo)
                diff_patch_rel = _posix(work_dir / "diff.patch", relative_to=repo)
                lines.extend([
                    "",
                    "### Diffs",
                    "",
                    f"Per-file diffs are available in `{diffs_rel}/`.",
                    f"Full diff: `{diff_patch_rel}`.",
                ])

            # Tell the agent where to write its findings so we get
            # clean output without tool-call traces mixed in.
            finding_rel = _posix(
                work_dir / "findings" / f"concern--{concern['name']}--{model}.md",
                relative_to=repo,
            )
            lines.extend([
                "",
                "---",
                "",
                "## Output Destination",
                "",
                f"Write your findings report to `{finding_rel}` using the `create` tool.",
                "Follow the Output Format above exactly.",
                "If no findings, write a single line: `NO FINDINGS`.",
                "Do NOT print your findings to stdout — write them to the file.",
            ])

            prompt_path.write_text("\n".join(lines), encoding="utf-8")

            prompt_entries.append({
                "concern": str(concern["name"]),
                "model": model,
                "priority": str(concern.get("priority", "standard")),
                "prompt_path": _posix(prompt_path, relative_to=repo),
                "finding_path": finding_rel,
            })

    return prompt_entries


# ---------------------------------------------------------------------------
# Dispatch planning
# ---------------------------------------------------------------------------


def _normalize_patterns(pattern: object) -> list[str]:
    """Normalize an ``applies_to`` value into a list of glob strings.

    Handles both single string values (``"**/*.cs"``) and inline YAML
    list values (``["**/*.cs", "**/*.fs"]``).
    """
    if pattern is None:
        return []
    if isinstance(pattern, str):
        return [pattern]
    return [str(p) for p in pattern]


def _rule_matches_files(rule: dict[str, object], files: list[str]) -> bool:
    """Does the rule's ``applies_to`` glob match any file in *files*?"""
    pattern = rule.get("applies_to")
    if pattern is None:
        return True  # no constraint → matches everything
    patterns = _normalize_patterns(pattern)
    return any(_file_matches_glob(f, p) for f in files for p in patterns)


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
        for i, cp in enumerate(chunk_paths):
            cf = chunk_file_cache[str(cp)]
            if not _rule_matches_files(rule, cf):
                continue
            dispatch.append(
                {
                    "rule_path": rule["path"],
                    "model": rule["model"],
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
    """Read committed rules, generate diff, filter, chunk, produce dispatch plan.

    Also reads concern files and generates per-file diffs and concern
    prompt files for the concern pipeline.
    """
    repo = Path(args.repo).resolve()
    config = _resolve_config(str(repo))
    raw_rules_dir = args.rules_dir if args.rules_dir is not None else str(config["rules_dir"])
    rules_dir = repo / Path(raw_rules_dir.replace("\\", "/"))
    scope: str = args.scope
    base_branch: str = (
        getattr(args, "base", None)
        or str(config["base_branch"])
    )

    rules = _read_rules(rules_dir, repo)
    if not rules:
        print(
            json.dumps(
                {"error": "No review rules found", "rules_dir": _posix(rules_dir, repo)}
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    base_dir = repo / ".agents" / "focused-review"
    base_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    work_dir = base_dir / timestamp
    work_dir.mkdir(parents=True, exist_ok=True)

    # -- determine changed files & chunks --------------------------------

    pathspecs = _make_pathspecs(args.path) if getattr(args, "path", None) else None

    diff_text = ""
    if scope == "full":
        changed_files = _all_tracked_files(repo, pathspecs)
        chunk_paths: list[Path] = []
    else:
        diff_text, changed_files = _get_diff(scope, repo, pathspecs, base_branch=base_branch)
        if not diff_text.strip():
            # Write empty concern-dispatch so downstream run-concerns
            # doesn't crash with FileNotFoundError.
            concern_dispatch_path = work_dir / "concern-dispatch.json"
            concern_dispatch_path.write_text("[]", encoding="utf-8")
            summary = {
                "run_dir": _posix(work_dir, relative_to=repo),
                "dispatch_path": None,
                "agents": 0,
                "scope": scope,
                "base_branch": base_branch,
                "rules_total": len(rules),
                "rules_matched": 0,
                "changed_files": 0,
                "chunks": 0,
                "concerns_total": 0,
                "concern_prompts": 0,
            }
            print(json.dumps(summary))
            return
        chunk_paths = _write_chunks(diff_text, work_dir)

    # write changed-files list
    (work_dir / "changed-files.txt").write_text(
        "\n".join(changed_files), encoding="utf-8"
    )

    # -- per-file diffs --------------------------------------------------

    if diff_text.strip():
        _write_per_file_diffs(diff_text, work_dir)

    # -- concerns --------------------------------------------------------

    raw_concerns_dir = str(config["concerns_dir"]).replace("\\", "/")
    project_concerns_dir = repo / Path(raw_concerns_dir)
    concerns = _read_concerns(project_concerns_dir, repo)
    if not concerns:
        concerns = _read_concerns(BUILTIN_CONCERNS_DIR, repo)

    concern_prompts: list[dict[str, str]] = []
    if concerns and changed_files:
        concern_prompts = _generate_concern_prompts(
            concerns, changed_files, work_dir, repo, scope=scope
        )

    # write concern dispatch
    concern_dispatch_path = work_dir / "concern-dispatch.json"
    concern_dispatch_path.write_text(
        json.dumps(concern_prompts, indent=2), encoding="utf-8"
    )

    # -- dispatch plan ---------------------------------------------------

    dispatch = _build_dispatch(rules, chunk_paths, changed_files, scope, repo)

    dispatch_path = work_dir / "dispatch.json"
    dispatch_path.write_text(json.dumps(dispatch, indent=2), encoding="utf-8")

    summary = {
        "run_dir": _posix(work_dir, relative_to=repo),
        "dispatch_path": _posix(dispatch_path, relative_to=repo),
        "agents": len(dispatch),
        "scope": scope,
        "base_branch": base_branch,
        "rules_total": len(rules),
        "rules_matched": len({d["rule_path"] for d in dispatch}),
        "changed_files": len(changed_files),
        "concerns_total": len(concerns),
        "concern_prompts": len(concern_prompts),
    }
    if scope != "full":
        summary["chunks"] = len(chunk_paths)

    print(json.dumps(summary))


# ---------------------------------------------------------------------------
# Subcommand: run-concerns
# ---------------------------------------------------------------------------


def _run_single_concern(
    entry: dict[str, str],
    repo: Path,
    work_dir: Path,
    *,
    timeout: int = CONCERN_TIMEOUT_SECS,
    retries: int = CONCERN_RETRIES,
    inherit_model: str = "",
) -> dict[str, object]:
    """Launch one ``copilot -p`` session for a (concern × model) pair.

    Reads the prompt file from *entry["prompt_path"]*, invokes the copilot
    CLI, captures stdout, and writes findings to
    ``work_dir/findings/concern--{name}--{model}.md``.

    Retries on non-zero exit or timeout up to *retries* times.

    Returns a result dict with keys: ``concern``, ``model``, ``status``,
    ``finding_path`` (on success), ``error`` (on failure), ``attempt``.
    """
    concern = entry["concern"]
    model = entry["model"]
    prompt_rel = entry["prompt_path"]
    prompt_abs = repo / prompt_rel

    finding_name = f"concern--{concern}--{model}"
    findings_dir = work_dir / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    finding_path = findings_dir / f"{finding_name}.md"

    # The agent is instructed to write clean findings directly to
    # finding_path.  If the dispatch entry includes the path the prompt
    # advertised, use that (it may differ from our default).
    expected_finding_rel = entry.get("finding_path")
    if expected_finding_rel:
        finding_path = repo / Path(expected_finding_rel.replace("/", os.sep))
        findings_dir = finding_path.parent
        findings_dir.mkdir(parents=True, exist_ok=True)

    traces_dir = work_dir / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    trace_path = traces_dir / f"{finding_name}.md"

    if not prompt_abs.is_file():
        return {
            "concern": concern,
            "model": model,
            "status": "error",
            "error": f"Prompt file not found: {prompt_rel}",
            "attempt": 0,
        }

    prompt_content = prompt_abs.read_text(encoding="utf-8")

    # Prompt passed as direct CLI argument — copilot CLI does not support
    # stdin piping via ``-p -``.
    cmd = [COPILOT_CMD, "-p", prompt_content, "--allow-all-tools"]
    if model == "inherit":
        if inherit_model:
            cmd.extend(["--model", inherit_model])
    else:
        cmd.extend(["--model", _resolve_model(model)])

    last_error = ""
    total_attempts = retries + 1
    for attempt in range(1, total_attempts + 1):
        try:
            result = subprocess.run(
                cmd,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0 and result.stdout.strip():
                # Always save the raw session trace for debugging.
                trace_path.write_text(result.stdout, encoding="utf-8")

                # The agent was instructed to write clean findings to
                # finding_path via the create tool.  If it didn't,
                # don't create a findings file — the trace is saved
                # for debugging.  Report as a failure so downstream
                # phases don't treat a missing report as "no findings".
                if not finding_path.is_file():
                    return {
                        "concern": concern,
                        "model": model,
                        "status": "failed",
                        "error": f"Agent did not write findings file. Check trace: {_posix(trace_path, relative_to=repo)}",
                        "attempt": attempt,
                    }

                return {
                    "concern": concern,
                    "model": model,
                    "status": "success",
                    "finding_path": _posix(finding_path, relative_to=repo),
                    "attempt": attempt,
                }
            last_error = result.stderr.strip() or f"Exit code {result.returncode}"
            if result.returncode == 0 and not result.stdout.strip():
                last_error = "Empty output"
        except subprocess.TimeoutExpired:
            last_error = f"Timed out after {timeout}s"
        except FileNotFoundError:
            return {
                "concern": concern,
                "model": model,
                "status": "error",
                "error": f"{COPILOT_CMD} is not installed or not on PATH",
                "attempt": attempt,
            }
        except OSError as exc:
            # On Windows, CreateProcess has a 32 766-char command-line limit.
            # When the prompt exceeds this, subprocess.run raises OSError.
            # Not retryable — the prompt won't shrink between attempts.
            return {
                "concern": concern,
                "model": model,
                "status": "error",
                "error": f"OS error (prompt may exceed CLI argument limit): {exc}",
                "attempt": attempt,
            }

    return {
        "concern": concern,
        "model": model,
        "status": "failed",
        "error": last_error,
        "attempt": total_attempts,
    }


def run_concerns(args: argparse.Namespace) -> None:
    """Read concern-dispatch.json and launch ``copilot -p`` per entry.

    Uses :class:`~concurrent.futures.ThreadPoolExecutor` for parallel
    execution.  Each worker invokes :func:`_run_single_concern` with
    retry and timeout logic.

    Prints a JSON summary on stdout with per-entry results and counts.
    """
    repo = Path(args.repo).resolve()
    if args.run_dir:
        work_dir = Path(args.run_dir) if Path(args.run_dir).is_absolute() else repo / args.run_dir
    else:
        work_dir = repo / ".agents" / "focused-review"
    dispatch_path = work_dir / "concern-dispatch.json"

    if not dispatch_path.is_file():
        print(
            json.dumps({"error": f"Dispatch file not found: {_posix(dispatch_path, relative_to=repo)}"}),
            file=sys.stderr,
        )
        sys.exit(1)

    entries: list[dict[str, str]] = json.loads(
        dispatch_path.read_text(encoding="utf-8")
    )

    if not entries:
        print(json.dumps({
            "total": 0,
            "success": 0,
            "failed": 0,
            "results": [],
        }))
        return

    max_workers = args.max_workers
    timeout = args.timeout
    retries = args.retries
    inherit_model = getattr(args, "inherit_model", "") or ""

    results: list[dict[str, object]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_entry = {
            executor.submit(
                _run_single_concern,
                entry,
                repo,
                work_dir,
                timeout=timeout,
                retries=retries,
                inherit_model=inherit_model,
            ): entry
            for entry in entries
        }

        for future in as_completed(future_to_entry):
            try:
                result = future.result()
            except Exception as exc:
                entry = future_to_entry[future]
                result = {
                    "concern": entry.get("concern", "unknown"),
                    "model": entry.get("model", "unknown"),
                    "status": "error",
                    "error": f"Unexpected: {exc}",
                    "attempt": 0,
                }
            results.append(result)
            status = result["status"]
            concern = result["concern"]
            model = result["model"]
            if status == "success":
                print(
                    f"  ✓ {concern} ({model}): {result.get('finding_path', '')}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"  ✗ {concern} ({model}): {result.get('error', status)}",
                    file=sys.stderr,
                )

    success_count = sum(1 for r in results if r["status"] == "success")
    failed_count = len(results) - success_count

    summary = {
        "total": len(results),
        "success": success_count,
        "failed": failed_count,
        "results": results,
    }
    print(json.dumps(summary))


# ---------------------------------------------------------------------------
# PR URL parsing
# ---------------------------------------------------------------------------

# GitHub: https://github.com/{owner}/{repo}/pull/{number}[/files|/commits|...]
_GITHUB_PR_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<pr_number>\d+)"
)

# ADO new-style: https://dev.azure.com/{org}/{project}/_git/{repo}/pullrequest/{id}
_ADO_NEW_RE = re.compile(
    r"^https://dev\.azure\.com/(?P<org>[^/]+)/(?P<project>[^/]+)/_git/(?P<repo>[^/]+)/pullrequest/(?P<pr_number>\d+)"
)

# ADO old-style: https://{org}.visualstudio.com/{project}/_git/{repo}/pullrequest/{id}
_ADO_OLD_RE = re.compile(
    r"^https://(?P<org>[^.]+)\.visualstudio\.com/(?P<project>[^/]+)/_git/(?P<repo>[^/]+)/pullrequest/(?P<pr_number>\d+)"
)


def parse_pr_url(args: argparse.Namespace) -> None:
    """Parse a GitHub or Azure DevOps PR URL and output JSON to stdout.

    Extracts platform, owner/org, project (ADO only), repo, and pr_number.
    Exits with code 1 and an error message on stderr for unrecognised URLs.
    """
    url: str = args.url.strip()

    m = _GITHUB_PR_RE.match(url)
    if m:
        print(json.dumps({
            "platform": "github",
            "owner": m.group("owner"),
            "repo": m.group("repo"),
            "pr_number": int(m.group("pr_number")),
        }))
        return

    m = _ADO_NEW_RE.match(url)
    if not m:
        m = _ADO_OLD_RE.match(url)
    if m:
        print(json.dumps({
            "platform": "ado",
            "org": m.group("org"),
            "project": m.group("project"),
            "repo": m.group("repo"),
            "pr_number": int(m.group("pr_number")),
        }))
        return

    print(
        json.dumps({"error": f"Unrecognised PR URL: {url}"}),
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# PR user identity
# ---------------------------------------------------------------------------


def get_pr_user(args: argparse.Namespace) -> None:
    """Get the authenticated user identity for GitHub or ADO.

    GitHub: calls ``gh api /user`` and parses login + name from JSON.
    ADO: calls ``az account show --query user.name -o tsv``.

    Outputs JSON with ``username`` and ``display_name`` to stdout.
    Exits with code 1 on failure.
    """
    platform: str = args.platform

    if platform == "github":
        try:
            raw = subprocess.run(
                ["gh", "api", "/user"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        except FileNotFoundError:
            print(
                json.dumps({"error": "gh CLI not found. Install it and run 'gh auth login'."}),
                file=sys.stderr,
            )
            sys.exit(1)
        except subprocess.CalledProcessError as exc:
            print(
                json.dumps({"error": f"gh CLI failed: {exc.stderr.strip()}"}),
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            user = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            print(
                json.dumps({"error": "gh api /user returned invalid JSON"}),
                file=sys.stderr,
            )
            sys.exit(1)
        username = user.get("login", "")
        display_name = user.get("name") or username

        print(json.dumps({
            "username": username,
            "display_name": display_name,
        }))

    elif platform == "ado":
        try:
            display_name = subprocess.run(
                ["az", "account", "show", "--query", "user.name", "-o", "tsv"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        except FileNotFoundError:
            print(
                json.dumps({"error": "az CLI not found. Install it and run 'az login'."}),
                file=sys.stderr,
            )
            sys.exit(1)
        except subprocess.CalledProcessError as exc:
            print(
                json.dumps({"error": f"az CLI failed: {exc.stderr.strip()}"}),
                file=sys.stderr,
            )
            sys.exit(1)

        # NOTE: ADO returns the user's display name (e.g. "John Doe"), not a
        # unique login ID.  This is a platform limitation — az CLI does not
        # expose unique identifiers for the signed-in user.  The display name
        # is used for both username and display_name in the attribution footer.
        print(json.dumps({
            "username": display_name,
            "display_name": display_name,
        }))

    else:
        print(
            json.dumps({"error": f"Unsupported platform: {platform}"}),
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Post comments to PR
# ---------------------------------------------------------------------------


def _check_gh_cli() -> None:
    """Verify gh CLI is installed and authenticated.

    Exits with code 1 and a message on stderr if the check fails.
    """
    try:
        subprocess.run(
            ["gh", "--version"],
            capture_output=True, text=True, check=True,
        )
    except FileNotFoundError:
        print("Error: gh CLI not found. Install it from https://cli.github.com/ and run 'gh auth login'.", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError:
        print("Error: gh CLI check failed.", file=sys.stderr)
        sys.exit(1)

    try:
        subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        print("Error: gh CLI not authenticated. Run 'gh auth login' first.", file=sys.stderr)
        sys.exit(1)


def _check_az_cli() -> None:
    """Verify az CLI is installed and authenticated.

    Exits with code 1 and a message on stderr if the check fails.
    """
    try:
        subprocess.run(
            ["az", "--version"],
            capture_output=True, text=True, check=True,
        )
    except FileNotFoundError:
        print(
            "Error: az CLI not found. Install it from https://aka.ms/installazurecli and run 'az login'.",
            file=sys.stderr,
        )
        sys.exit(1)
    except subprocess.CalledProcessError:
        print("Error: az CLI check failed.", file=sys.stderr)
        sys.exit(1)

    try:
        subprocess.run(
            ["az", "account", "show"],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        print("Error: az CLI not authenticated. Run 'az login' first.", file=sys.stderr)
        sys.exit(1)


def _get_ado_token() -> str:
    """Get an Azure DevOps bearer token via ``az account get-access-token``.

    Returns the access token string.
    Exits with code 1 if the token cannot be obtained.
    """
    try:
        result = subprocess.run(
            [
                "az", "account", "get-access-token",
                "--resource", "499b84ac-1321-427f-aa17-267ca6975798",
                "--query", "accessToken",
                "-o", "tsv",
            ],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"Error: Failed to get ADO access token: {exc.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    token = result.stdout.strip()
    if not token:
        print("Error: az returned empty access token.", file=sys.stderr)
        sys.exit(1)
    return token


def _post_ado_thread(
    token: str,
    org: str,
    project: str,
    repo: str,
    pr_id: int,
    thread_body: dict,
) -> dict:
    """Post a single comment thread to an Azure DevOps pull request.

    Returns the parsed JSON response from the API, or an empty dict if the
    response body is empty or not valid JSON.
    Raises ``urllib.error.URLError`` or ``urllib.error.HTTPError`` on failure.
    """
    enc_org = urllib.parse.quote(org, safe="")
    enc_project = urllib.parse.quote(project, safe="")
    enc_repo = urllib.parse.quote(repo, safe="")
    url = (
        f"https://dev.azure.com/{enc_org}/{enc_project}/_apis/git/repositories/{enc_repo}"
        f"/pullRequests/{pr_id}/threads?api-version=7.0"
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(thread_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        raw = resp.read().decode("utf-8")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}


def _post_comments_github(
    data: dict,
    inline_comments: list[dict],
    exclude_ids: set[int],
) -> None:
    """Post review comments to a GitHub PR via ``gh api``."""
    owner: str = data["owner"]
    repo: str = data["repo"]
    pr_number: int = data["pr_number"]
    review_body: str = data.get("review_body", "")

    _check_gh_cli()

    gh_comments = [
        {
            "path": c["path"],
            "line": c["line"],
            "side": "RIGHT",
            "body": c["body"],
        }
        for c in inline_comments
    ]

    payload: dict = {
        "body": review_body,
        "event": "COMMENT",
    }
    if gh_comments:
        payload["comments"] = gh_comments

    endpoint = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    cmd = [
        "gh", "api",
        "-X", "POST",
        endpoint,
        "--input", "-",
    ]

    try:
        result = subprocess.run(
            cmd,
            input=json.dumps(payload),
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr_msg = exc.stderr.strip()
        try:
            err_data = json.loads(exc.stdout)
            api_msg = err_data.get("message", stderr_msg)
        except (json.JSONDecodeError, AttributeError):
            api_msg = stderr_msg

        print(json.dumps({
            "status": "error",
            "error": api_msg,
            "endpoint": endpoint,
            "comments_attempted": len(gh_comments),
        }))
        sys.exit(1)

    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        response = {}

    review_url = response.get("html_url", "")
    print(json.dumps({
        "status": "posted",
        "review_url": review_url,
        "comments_posted": len(gh_comments),
        "comments_excluded": len(exclude_ids),
    }))


def _post_comments_ado(
    data: dict,
    inline_comments: list[dict],
    exclude_ids: set[int],
) -> None:
    """Post review comments to an Azure DevOps PR via REST API.

    Posts each comment as a separate thread (ADO has no batch endpoint).
    The overall review body is posted as a thread without ``threadContext``.
    Inline comments are posted with ``threadContext`` containing file path and
    line position.  Continues on failure and collects results.
    """
    org: str = data["org"]
    project: str = data["project"]
    repo: str = data["repo"]
    pr_number: int = data["pr_number"]
    review_body: str = data.get("review_body", "")

    _check_az_cli()
    token = _get_ado_token()

    posted: int = 0
    failed: int = 0
    errors: list[dict] = []

    # Post overall review body as a thread without threadContext ----------------
    if review_body:
        overall_thread: dict = {
            "comments": [{"content": review_body, "commentType": 1}],
            "status": 1,
        }
        try:
            _post_ado_thread(token, org, project, repo, pr_number, overall_thread)
            posted += 1
        except urllib.error.HTTPError as exc:
            failed += 1
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            errors.append({
                "type": "review_body",
                "error": f"HTTP {exc.code}: {body}",
            })
        except urllib.error.URLError as exc:
            failed += 1
            errors.append({
                "type": "review_body",
                "error": str(exc.reason),
            })

    # Post inline comments as individual threads with threadContext -------------
    for comment in inline_comments:
        thread: dict = {
            "comments": [{"content": comment["body"], "commentType": 1}],
            "status": 1,
            "threadContext": {
                "filePath": "/" + comment["path"].lstrip("/"),
                "rightFileStart": {"line": comment["line"], "offset": 1},
                "rightFileEnd": {"line": comment["line"], "offset": 1},
            },
        }
        try:
            _post_ado_thread(token, org, project, repo, pr_number, thread)
            posted += 1
        except urllib.error.HTTPError as exc:
            failed += 1
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            errors.append({
                "id": comment.get("id"),
                "path": comment["path"],
                "line": comment["line"],
                "error": f"HTTP {exc.code}: {body}",
            })
        except urllib.error.URLError as exc:
            failed += 1
            errors.append({
                "id": comment.get("id"),
                "path": comment["path"],
                "line": comment["line"],
                "error": str(exc.reason),
            })

    result_data: dict = {
        "status": "posted" if failed == 0 else ("partial" if posted > 0 else "error"),
        "comments_posted": posted,
        "comments_failed": failed,
        "comments_excluded": len(exclude_ids),
    }
    if errors:
        result_data["errors"] = errors

    print(json.dumps(result_data))
    if posted == 0 and failed > 0:
        sys.exit(1)


def post_comments(args: argparse.Namespace) -> None:
    """Post review comments to a PR via ``gh api`` (GitHub) or ADO REST API.

    Reads a ``comments.json`` file (written by the skill), optionally excludes
    specific findings by id, and posts review comments to the PR.

    Outputs a result JSON to stdout with the posted review status.
    Exits with code 1 on fatal errors.
    """
    comments_path: str = args.comments
    exclude_ids: set[int] = set()
    if args.exclude:
        try:
            exclude_ids = {int(x.strip()) for x in args.exclude.split(",") if x.strip()}
        except ValueError:
            print(
                "Error: --exclude must be comma-separated integers (e.g. '1,2,3')",
                file=sys.stderr,
            )
            sys.exit(1)

    # Load comments.json -------------------------------------------------------
    try:
        with open(comments_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Error: Comments file not found: {comments_path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"Error: Invalid JSON in {comments_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    platform: str = data.get("platform", "")
    if platform not in ("github", "ado"):
        print(f"Error: Unsupported platform: {platform!r}. Use 'github' or 'ado'.", file=sys.stderr)
        sys.exit(1)

    inline_comments: list[dict] = data.get("inline_comments", [])

    # Filter out excluded findings ---------------------------------------------
    if exclude_ids:
        inline_comments = [c for c in inline_comments if c.get("id") not in exclude_ids]

    if platform == "github":
        _post_comments_github(data, inline_comments, exclude_ids)
    else:
        _post_comments_ado(data, inline_comments, exclude_ids)



# ---------------------------------------------------------------------------
# records.json validation (Phase 2)
#
# `validate_records` is a pure function: it takes the parsed JSON and returns a
# list of structured, per-record error dicts (empty list == valid). Each error
# carries enough context — a JSON-ish `path`, the offending `field`, and the
# finding's stable identifiers (`record_id` / `assessment_id`) plus its
# positional `display_number` when known — for the orchestrator to relay an
# actionable message back to the reporter on a retry.
# ---------------------------------------------------------------------------

# Sentinel distinguishing "key absent" from an explicit JSON ``null``.
_MISSING = object()


def _json_type_name(value: object) -> str:
    """Return the JSON type name for *value* (for human-readable messages)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_int(value: object) -> bool:
    """True for a real integer (``bool`` is a subclass of ``int`` — exclude it)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonempty_str(value: object) -> bool:
    """True for a non-blank string."""
    return isinstance(value, str) and value.strip() != ""


def _records_error(
    scope: str,
    index: int | None,
    path: str,
    field: str | None,
    message: str,
    *,
    record_id: str | None = None,
    assessment_id: str | None = None,
    display_number: int | None = None,
) -> dict[str, object]:
    """Build one structured validation error.

    ``scope`` is one of ``envelope`` / ``run`` / ``finding`` /
    ``rebuttal_override`` / ``rule_quality_note``; ``index`` is the position
    within that array (``None`` for the singleton envelope/run scopes).
    """
    return {
        "scope": scope,
        "index": index,
        "path": path,
        "field": field,
        "record_id": record_id,
        "assessment_id": assessment_id,
        "display_number": display_number,
        "message": message,
    }


def _finding_identity(rec: dict) -> tuple[str | None, str | None, int | None]:
    """Best-effort extraction of a finding's stable ids for error attribution."""
    rid = rec.get("record_id")
    rid = rid if _is_nonempty_str(rid) else None
    aid = rec.get("assessment_id")
    aid = aid if _is_nonempty_str(aid) else None
    dnum = rec.get("display_number")
    dnum = dnum if _is_int(dnum) else None
    return rid, aid, dnum


def _validate_run(run: dict, errors: list[dict]) -> None:
    """Validate the ``run`` metadata object (counts cross-checked separately)."""

    def add(field: str | None, message: str) -> None:
        path = "run" if field is None else f"run.{field}"
        errors.append(_records_error("run", None, path, field, message))

    for field in ("run_id", "date"):
        if not _is_nonempty_str(run.get(field, _MISSING)):
            add(field, f"{field} is required and must be a non-empty string")

    scope = run.get("scope", _MISSING)
    if scope is _MISSING:
        add("scope", "scope is required and must be one of " + ", ".join(VALID_RUN_SCOPES))
    elif scope not in VALID_RUN_SCOPES:
        add("scope", f"scope must be one of {', '.join(VALID_RUN_SCOPES)} (got {scope!r})")

    for field in (
        "rule_count",
        "concern_count",
        "consolidated_count",
        "confirmed",
        "questionable",
        "invalid",
    ):
        value = run.get(field, _MISSING)
        if not _is_int(value) or value < 0:
            add(field, f"{field} is required and must be an integer >= 0")


def _validate_run_counts(run: dict, findings: list, errors: list[dict]) -> None:
    """Cross-check ``run`` tallies against the verdicts present in ``findings``.

    Only runs when every finding is an object (otherwise a structural error was
    already reported). Catches truncation and miscounts — both real reporter
    failure modes the spec calls out.
    """
    if not all(isinstance(rec, dict) for rec in findings):
        return

    tally = {verdict: 0 for verdict in VALID_VERDICTS}
    for rec in findings:
        verdict = rec.get("verdict")
        if verdict in tally:
            tally[verdict] += 1

    def add(field: str, message: str) -> None:
        errors.append(_records_error("run", None, f"run.{field}", field, message))

    # Only cross-check the per-verdict tallies when every finding has a valid
    # verdict. A finding with a bad/missing verdict drops out of the tally and
    # would otherwise produce a *misleading* "run.confirmed is N but M findings
    # have verdict 'Confirmed'" error — pointing the reporter at the wrong field
    # and risking a wasted retry. The real (verdict) error is reported per-finding.
    if all(rec.get("verdict") in tally for rec in findings):
        for field, verdict in (
            ("confirmed", "Confirmed"),
            ("questionable", "Questionable"),
            ("invalid", "Invalid"),
        ):
            value = run.get(field)
            if _is_int(value) and value >= 0 and value != tally[verdict]:
                add(
                    field,
                    f"run.{field} is {value} but {tally[verdict]} finding(s) have "
                    f"verdict '{verdict}'",
                )

    # Length-based, independent of verdicts — always safe to check, and the
    # primary signal for a truncated findings array.
    consolidated = run.get("consolidated_count")
    if _is_int(consolidated) and consolidated >= 0 and consolidated != len(findings):
        add(
            "consolidated_count",
            f"run.consolidated_count is {consolidated} but findings[] has "
            f"{len(findings)} entr(ies)",
        )


def _validate_provenance(prov: object, base_path: str, add) -> None:
    """Validate a finding's ``provenance`` list.

    Each entry is either a non-empty source label string or an object carrying a
    non-empty ``source`` field — both encodings give the renderer a label to show
    in the "Found by" column. ``add`` is the finding-scoped error accumulator.
    """
    if not isinstance(prov, list) or len(prov) == 0:
        add("provenance", "provenance is required and must be a non-empty array")
        return
    for j, entry in enumerate(prov):
        entry_path = f"{base_path}.provenance[{j}]"
        if _is_nonempty_str(entry):
            continue
        if isinstance(entry, dict):
            if not _is_nonempty_str(entry.get("source")):
                add(
                    "provenance",
                    f"provenance[{j}] object must have a non-empty 'source' string",
                )
            continue
        add(
            "provenance",
            f"provenance[{j}] must be a non-empty string or an object with a "
            f"'source' field (got {_json_type_name(entry)})",
        )


def _validate_finding(
    rec: object,
    index: int,
    errors: list[dict],
    seen_record_ids: set,
    seen_assessment_ids: set,
    seen_display_numbers: set,
) -> None:
    """Validate a single finding object and accumulate structured errors."""
    base_path = f"findings[{index}]"
    if not isinstance(rec, dict):
        errors.append(
            _records_error(
                "finding",
                index,
                base_path,
                None,
                f"finding must be a JSON object, got {_json_type_name(rec)}",
            )
        )
        return

    rid, aid, dnum = _finding_identity(rec)

    def add(field: str | None, message: str) -> None:
        path = base_path if field is None else f"{base_path}.{field}"
        errors.append(
            _records_error(
                "finding",
                index,
                path,
                field,
                message,
                record_id=rid,
                assessment_id=aid,
                display_number=dnum,
            )
        )

    def require_nonempty_str(field: str) -> None:
        if not _is_nonempty_str(rec.get(field, _MISSING)):
            add(field, f"{field} is required and must be a non-empty string")

    def require_str(field: str) -> None:
        value = rec.get(field, _MISSING)
        if value is _MISSING or not isinstance(value, str):
            add(field, f'{field} is required and must be a string (use "" when absent)')

    def require_enum(field: str, allowed: tuple) -> None:
        value = rec.get(field, _MISSING)
        if value is _MISSING:
            add(field, f"{field} is required and must be one of " + ", ".join(allowed))
        elif value not in allowed:
            add(field, f"{field} must be one of {', '.join(allowed)} (got {value!r})")

    # Stable identifier — required, unique.
    record_id = rec.get("record_id", _MISSING)
    if not _is_nonempty_str(record_id):
        add("record_id", "record_id is required and must be a non-empty string")
    elif record_id in seen_record_ids:
        add("record_id", f"duplicate record_id {record_id!r} (record_ids must be unique)")
    else:
        seen_record_ids.add(record_id)

    require_nonempty_str("title")
    require_nonempty_str("file")
    require_str("description")
    require_str("assessment")
    require_str("suggestion")

    require_enum("original_severity", VALID_SEVERITIES)
    require_enum("severity", VALID_SEVERITIES)
    require_enum("fix_complexity", VALID_FIX_COMPLEXITIES)
    require_enum("verdict", VALID_VERDICTS)
    require_enum("type", VALID_FINDING_TYPES)

    # introduced_by — optional display metadata; only type-checked when present
    # (the spec keeps it free-form: "type-checked only, no enum").
    introduced_by = rec.get("introduced_by", _MISSING)
    if introduced_by is not _MISSING and not isinstance(introduced_by, str):
        add("introduced_by", "introduced_by must be a string when present")

    # has_detail — required boolean; gates the optional detail sidecar.
    has_detail = rec.get("has_detail", _MISSING)
    if not isinstance(has_detail, bool):
        add("has_detail", "has_detail is required and must be a boolean")

    # assessment_id — nullable; the detail sidecar is located by it AND the
    # Invalid-findings table is keyed by it, so a non-null assessment_id must be
    # a non-empty string, unique across findings, and present when has_detail.
    assessment_id = rec.get("assessment_id", None)
    if assessment_id is not None and not _is_nonempty_str(assessment_id):
        add("assessment_id", "assessment_id must be a non-empty string or null")
    elif _is_nonempty_str(assessment_id):
        if assessment_id in seen_assessment_ids:
            add(
                "assessment_id",
                f"duplicate assessment_id {assessment_id!r} (assessment ids must be unique)",
            )
        else:
            seen_assessment_ids.add(assessment_id)
    if has_detail is True and not _is_nonempty_str(assessment_id):
        add(
            "assessment_id",
            "assessment_id must be a non-empty string when has_detail is true "
            "(the detail sidecar is located by assessment id)",
        )

    # line — nullable; an integer line number otherwise.
    line = rec.get("line", None)
    if line is not None and not (_is_int(line) and line >= 0):
        add("line", "line must be an integer >= 0 or null")

    # display_number — positional. Required and unique for the numbered
    # (actionable) Confirmed/Questionable sections; optional for Invalid findings
    # (which render in a separate table keyed by assessment_id), so an Invalid
    # finding's number does not participate in the uniqueness set.
    verdict = rec.get("verdict")
    requires_number = verdict in _VERDICTS_REQUIRING_DISPLAY_NUMBER
    display_number = rec.get("display_number", None)
    if display_number is None:
        if requires_number:
            add(
                "display_number",
                "display_number is required (integer >= 1) for "
                "Confirmed/Questionable findings",
            )
    elif not _is_int(display_number) or display_number < 1:
        add("display_number", "display_number must be an integer >= 1 or null")
    elif requires_number:
        if display_number in seen_display_numbers:
            add(
                "display_number",
                f"duplicate display_number {display_number} (display numbers must be unique)",
            )
        else:
            seen_display_numbers.add(display_number)

    _validate_provenance(rec.get("provenance", _MISSING), base_path, add)


def _validate_rebuttal_override(
    item: object, index: int, errors: list[dict], known_record_ids: set | None
) -> None:
    """Validate one rebuttal-override entry."""
    base_path = f"rebuttal_overrides[{index}]"
    if not isinstance(item, dict):
        errors.append(
            _records_error(
                "rebuttal_override",
                index,
                base_path,
                None,
                f"rebuttal override must be a JSON object, got {_json_type_name(item)}",
            )
        )
        return

    raw_rid = item.get("record_id")
    rid_ident = raw_rid if _is_nonempty_str(raw_rid) else None

    def add(field: str | None, message: str) -> None:
        path = base_path if field is None else f"{base_path}.{field}"
        errors.append(
            _records_error(
                "rebuttal_override", index, path, field, message, record_id=rid_ident
            )
        )

    if not _is_nonempty_str(raw_rid):
        add("record_id", "record_id is required and must be a non-empty string")
    elif known_record_ids is not None and raw_rid not in known_record_ids:
        add("record_id", f"record_id {raw_rid!r} does not match any finding")

    for field in ("original_severity", "severity"):
        value = item.get(field, _MISSING)
        if value is _MISSING:
            add(field, f"{field} is required and must be one of " + ", ".join(VALID_SEVERITIES))
        elif value not in VALID_SEVERITIES:
            add(field, f"{field} must be one of {', '.join(VALID_SEVERITIES)} (got {value!r})")

    if not _is_nonempty_str(item.get("reasoning", _MISSING)):
        add("reasoning", "reasoning is required and must be a non-empty string")


def _validate_rule_quality_note(item: object, index: int, errors: list[dict]) -> None:
    """Validate one rule-quality-note entry."""
    base_path = f"rule_quality_notes[{index}]"
    if not isinstance(item, dict):
        errors.append(
            _records_error(
                "rule_quality_note",
                index,
                base_path,
                None,
                f"rule quality note must be a JSON object, got {_json_type_name(item)}",
            )
        )
        return

    def add(field: str, message: str) -> None:
        errors.append(
            _records_error("rule_quality_note", index, f"{base_path}.{field}", field, message)
        )

    for field in ("rule", "observation", "suggestion"):
        if not _is_nonempty_str(item.get(field, _MISSING)):
            add(field, f"{field} is required and must be a non-empty string")


def validate_records(data: object) -> list[dict]:
    """Validate a parsed ``records.json`` envelope against the schema.

    Returns a list of structured, per-record error dicts; an empty list means
    the envelope is valid. Never raises on malformed *data* — every problem is
    reported as an error entry so the caller can relay it back to the reporter.
    """
    errors: list[dict] = []

    if not isinstance(data, dict):
        errors.append(
            _records_error(
                "envelope",
                None,
                "$",
                None,
                f"records.json root must be a JSON object, got {_json_type_name(data)}",
            )
        )
        return errors

    def add_envelope(field: str | None, message: str) -> None:
        path = "$" if field is None else field
        errors.append(_records_error("envelope", None, path, field, message))

    # schema_version --------------------------------------------------------
    schema_version = data.get("schema_version", _MISSING)
    if schema_version is _MISSING:
        add_envelope("schema_version", "schema_version is required")
    elif not _is_int(schema_version):
        add_envelope(
            "schema_version",
            f"schema_version must be the integer {RECORDS_SCHEMA_VERSION} "
            f"(got {_json_type_name(schema_version)})",
        )
    elif schema_version != RECORDS_SCHEMA_VERSION:
        add_envelope(
            "schema_version",
            f"unsupported schema_version {schema_version}; this tool supports "
            f"{RECORDS_SCHEMA_VERSION}",
        )

    # run -------------------------------------------------------------------
    run = data.get("run", _MISSING)
    if run is _MISSING:
        add_envelope("run", "run is required and must be an object")
    elif not isinstance(run, dict):
        add_envelope("run", f"run must be an object, got {_json_type_name(run)}")
    else:
        _validate_run(run, errors)

    # findings --------------------------------------------------------------
    findings = data.get("findings", _MISSING)
    record_ids: set = set()
    findings_is_list = isinstance(findings, list)
    if findings is _MISSING:
        add_envelope("findings", "findings is required and must be an array")
    elif not findings_is_list:
        add_envelope("findings", f"findings must be an array, got {_json_type_name(findings)}")
    else:
        assessment_ids: set = set()
        display_numbers: set = set()
        for i, rec in enumerate(findings):
            _validate_finding(rec, i, errors, record_ids, assessment_ids, display_numbers)

    # rebuttal_overrides ----------------------------------------------------
    overrides = data.get("rebuttal_overrides", _MISSING)
    if overrides is _MISSING:
        add_envelope(
            "rebuttal_overrides",
            "rebuttal_overrides is required and must be an array (use [] when none)",
        )
    elif not isinstance(overrides, list):
        add_envelope(
            "rebuttal_overrides",
            f"rebuttal_overrides must be an array, got {_json_type_name(overrides)}",
        )
    else:
        known = record_ids if findings_is_list else None
        for i, item in enumerate(overrides):
            _validate_rebuttal_override(item, i, errors, known)

    # rule_quality_notes ----------------------------------------------------
    notes = data.get("rule_quality_notes", _MISSING)
    if notes is _MISSING:
        add_envelope(
            "rule_quality_notes",
            "rule_quality_notes is required and must be an array (use [] when none)",
        )
    elif not isinstance(notes, list):
        add_envelope(
            "rule_quality_notes",
            f"rule_quality_notes must be an array, got {_json_type_name(notes)}",
        )
    else:
        for i, item in enumerate(notes):
            _validate_rule_quality_note(item, i, errors)

    # run/findings count cross-checks (only when both are well-formed) ------
    if isinstance(run, dict) and findings_is_list:
        _validate_run_counts(run, findings, errors)

    return errors


def load_and_validate_records(path: str | os.PathLike) -> tuple[object, list[dict]]:
    """Load *path* and validate it.

    Returns ``(data, errors)``. On a read/parse failure, ``data`` is ``None`` and
    ``errors`` holds a single envelope-scoped error.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return None, [
            _records_error("envelope", None, "$", None, f"records.json not found: {path}")
        ]
    except OSError as exc:
        return None, [
            _records_error("envelope", None, "$", None, f"could not read records.json: {exc}")
        ]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [
            _records_error("envelope", None, "$", None, f"records.json is not valid JSON: {exc}")
        ]

    return data, validate_records(data)


def validate_records_command(args: argparse.Namespace) -> None:
    """CLI: validate ``records.json`` and emit a structured result.

    On success, prints a small summary JSON to stdout (exit 0). On failure,
    prints the structured per-record errors as JSON to stderr and exits 1, so the
    orchestrator can relay them to the reporter for a retry.
    """
    path: str = args.records
    data, errors = load_and_validate_records(path)

    if errors:
        payload = {
            "valid": False,
            "schema_version": RECORDS_SCHEMA_VERSION,
            "records_path": str(path),
            "error_count": len(errors),
            "errors": errors,
        }
        print(json.dumps(payload, indent=2), file=sys.stderr)
        sys.exit(1)

    run = data.get("run", {}) if isinstance(data, dict) else {}
    findings = data.get("findings", []) if isinstance(data, dict) else []
    summary = {
        "valid": True,
        "schema_version": RECORDS_SCHEMA_VERSION,
        "records_path": str(path),
        "run_id": run.get("run_id") if isinstance(run, dict) else None,
        "findings": len(findings),
        "confirmed": run.get("confirmed") if isinstance(run, dict) else None,
        "questionable": run.get("questionable") if isinstance(run, dict) else None,
        "invalid": run.get("invalid") if isinstance(run, dict) else None,
    }
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
        "--path",
        nargs="*",
        default=None,
        help="Restrict review to files matching these paths (directories, globs). Multiple values allowed.",
    )
    prepare_parser.add_argument(
        "--base",
        default=None,
        help="Base ref for branch scope diff (default: resolved from focused-review.json, then origin/main). "
             "Accepts any git ref: origin/main, origin/dev, upstream/release/1.2, etc.",
    )
    prepare_parser.set_defaults(func=prepare_review)

    # run-concerns subcommand
    concerns_parser = subparsers.add_parser(
        "run-concerns",
        help="Launch copilot -p sessions for each concern in concern-dispatch.json",
    )
    concerns_parser.add_argument(
        "--repo",
        default=".",
        help="Path to the repository root (default: current directory)",
    )
    concerns_parser.add_argument(
        "--max-workers",
        type=int,
        default=CONCERN_MAX_WORKERS,
        help=f"Maximum parallel copilot sessions (default: {CONCERN_MAX_WORKERS})",
    )
    concerns_parser.add_argument(
        "--timeout",
        type=int,
        default=CONCERN_TIMEOUT_SECS,
        help=f"Timeout per copilot session in seconds (default: {CONCERN_TIMEOUT_SECS})",
    )
    concerns_parser.add_argument(
        "--retries",
        type=int,
        default=CONCERN_RETRIES,
        help=f"Number of retries per failed session (default: {CONCERN_RETRIES})",
    )
    concerns_parser.add_argument(
        "--inherit-model",
        default="",
        help="Model ID to use for entries with model='inherit' (the orchestrator's own model)",
    )
    concerns_parser.add_argument(
        "--run-dir",
        default="",
        help="Run directory for this review session",
    )
    concerns_parser.set_defaults(func=run_concerns)

    # parse-pr-url subcommand
    pr_url_parser = subparsers.add_parser(
        "parse-pr-url",
        help="Parse a GitHub or Azure DevOps PR URL into structured JSON",
    )
    pr_url_parser.add_argument(
        "--url",
        required=True,
        help="The PR URL to parse",
    )
    pr_url_parser.set_defaults(func=parse_pr_url)

    # get-pr-user subcommand
    pr_user_parser = subparsers.add_parser(
        "get-pr-user",
        help="Get the authenticated user identity for GitHub or ADO",
    )
    pr_user_parser.add_argument(
        "--platform",
        required=True,
        choices=["github", "ado"],
        help="Platform to query (github or ado)",
    )
    pr_user_parser.set_defaults(func=get_pr_user)

    # post-comments subcommand
    post_parser = subparsers.add_parser(
        "post-comments",
        help="Post review comments to a GitHub or ADO PR",
    )
    post_parser.add_argument(
        "--comments",
        required=True,
        help="Path to comments.json file",
    )
    post_parser.add_argument(
        "--exclude",
        default=None,
        help="Comma-separated list of finding IDs to exclude",
    )
    post_parser.set_defaults(func=post_comments)

    # validate-records subcommand
    validate_parser = subparsers.add_parser(
        "validate-records",
        help="Validate a records.json envelope against the schema",
    )
    validate_parser.add_argument(
        "--records",
        required=True,
        help="Path to the records.json file to validate",
    )
    validate_parser.set_defaults(func=validate_records_command)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
