#!/usr/bin/env python3
"""Focused review helper — deterministic operations for the focused-review plugin."""

from __future__ import annotations

import argparse
import fnmatch
import html
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

# Optional dependency: nh3 (Rust `ammonia`) sanitizes assessor-authored rich
# detail sidecars before they are embedded in the canvas. It is declared as a
# core dependency (installed out the gate), but the render path fails closed if
# the import is ever missing — without the sanitizer no raw HTML is emitted, the
# canvas falls back to escaped text, and the `rich_html` capability reports off.
try:
    import nh3 as _nh3
except ImportError:  # pragma: no cover - fail-closed path, exercised via monkeypatch
    _nh3 = None

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

# Packaged canvas template (Phase 1) that render-review (Phase 3) fills server-side.
CANVAS_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "review-canvas.html"
# Default canvas output, relative to the repo root. Gitignored and served by
# Treemon when it is running; written unconditionally so it is ready as a tab.
DEFAULT_CANVAS_RELPATH = os.path.join(".agents", "canvas", "focused-review.html")

# Trusted Treemon parent-app origin pinned into the canvas action-bar channel.
# Treemon serves the canvas iframe over http://127.0.0.1:5002, but the embedding
# parent app is a *different* origin (http://localhost:5000). render-review bakes
# this into the canvas data-parent-origin attribute so the action bar posts to —
# and accepts morph signals from — only that origin instead of the wildcard "*"
# (origin-validated channel; see docs/spec/canvas-review-report.md security model).
DEFAULT_PARENT_ORIGIN = "http://localhost:5000"

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
# render-review (Phase 3): render review.md / terminal summary / canvas HTML
# from a validated records.json envelope.
#
# Python's role is strictly mechanical: it never interprets prose. It groups the
# validated findings by verdict, formats the provenance labels, and templates the
# three artifacts. review.md preserves the heading/field shape the reporter used
# to hand-author (locked by a golden-file test, and read downstream by the
# post-mortem mode, which matches the "### {n}." headings and the
# rule:/concern: provenance labels). The canvas fills the version-controlled
# template, html.escape()-ing every structured text field.
# See docs/spec/canvas-review-report.md.
# ---------------------------------------------------------------------------

# Severity → abbreviation shown in the (space-constrained) canvas invalid table.
_SEV_ABBREV = {"Critical": "Crit", "High": "High", "Medium": "Med", "Low": "Low"}

# Relay trailer is intentionally NOT emitted here — see render_terminal_summary.


def _sev_class(severity: str) -> str:
    """CSS severity class for a severity word, e.g. 'High' -> 'sev-high'.

    The result is derived from the (untrusted) severity text, so callers that
    interpolate it into an HTML ``class="..."`` attribute must wrap it in
    ``html.escape(..., quote=True)`` — it is intentionally not pre-escaped here,
    so the escape stays visible at the attribute boundary like every other field.
    """
    return "sev-" + str(severity).strip().lower()


def _sev_abbrev(severity: str) -> str:
    """Short severity label for the canvas invalid table."""
    return _SEV_ABBREV.get(severity, str(severity))


def _location_str(file: object, line: object) -> str:
    """Render a finding's location as ``file:line`` (or just ``file`` when line is null)."""
    file_str = "" if file is None else str(file)
    if line is not None and _is_int(line):
        return f"{file_str}:{line}"
    return file_str


def _md_cell(text: object) -> str:
    """Escape a value for a one-line Markdown table cell.

    Collapses newlines to spaces and escapes the cell delimiter so a stray ``|``
    or line break in finding text can't break the table layout.
    """
    return (
        str("" if text is None else text)
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("|", "\\|")
        .strip()
    )


def _flatten(text: object) -> str:
    """Collapse a value to a single line (for table 'reason' cells)."""
    return " ".join(str("" if text is None else text).split())


def _invalid_summary_text(n: int) -> str:
    """e.g. '1 finding filtered as invalid' / '3 findings filtered as invalid'."""
    return f"{n} finding{'' if n == 1 else 's'} filtered as invalid"


# ── provenance → "Found by" labels ───────────────────────────────────────────
#
# Provenance entries are the reporter's filename-derived source labels — either a
# bare string or an object carrying a ``source`` field (validated in Phase 2).
# The canonical convention is ``rule--<name>`` and ``concern--<name>--<model>``
# (e.g. ``concern--bugs--opus``). Anything that doesn't match is shown verbatim.
# Three views derive from the same source list:
#   * review.md   — ungrouped, "N source(s): rule:<name>, concern:<name> (<model>)"
#                   (the post-mortem mode parses exactly these labels)
#   * terminal    — grouped, "<name>(<model>,<model>)" for concerns, "rule:<name>"
#   * canvas tags — grouped, colour-coded <span> tags (no "rule:" prefix needed)


def _parse_source_label(label: str) -> tuple[str, str, str | None]:
    """Parse one provenance source label into ``(kind, name, model)``.

    ``kind`` is ``rule`` / ``concern`` / ``other``. Unrecognised labels are
    returned verbatim as ``("other", label, None)``.
    """
    text = label.strip()
    if text.startswith("rule--"):
        return ("rule", text[len("rule--"):], None)
    if text.startswith("concern--"):
        rest = text[len("concern--"):]
        if "--" in rest:
            name, model = rest.rsplit("--", 1)
            return ("concern", name, model or None)
        return ("concern", rest, None)
    return ("other", text, None)


def _provenance_sources(provenance: object) -> list[tuple[str, str, str | None]]:
    """Parse a finding's provenance list into ordered ``(kind, name, model)`` tuples."""
    sources: list[tuple[str, str, str | None]] = []
    if not isinstance(provenance, list):
        return sources
    for entry in provenance:
        if isinstance(entry, dict):
            label = entry.get("source")
        else:
            label = entry
        if isinstance(label, str) and label.strip():
            sources.append(_parse_source_label(label))
    return sources


def _group_sources(
    sources: list[tuple[str, str, str | None]],
) -> list[tuple[str, str, list[str]]]:
    """Group sources by ``(kind, name)`` preserving order, collecting unique models."""
    groups: list[tuple[str, str, list[str]]] = []
    index: dict[tuple[str, str], int] = {}
    for kind, name, model in sources:
        key = (kind, name)
        if key not in index:
            index[key] = len(groups)
            groups.append((kind, name, []))
        if model:
            models = groups[index[key]][2]
            if model not in models:
                models.append(model)
    return groups


def _found_by_md(provenance: object) -> str:
    """review.md 'Found by' — ungrouped, count-prefixed, post-mortem-parseable."""
    sources = _provenance_sources(provenance)
    labels: list[str] = []
    for kind, name, model in sources:
        if kind == "rule":
            labels.append(f"rule:{name}")
        elif kind == "concern":
            labels.append(f"concern:{name} ({model})" if model else f"concern:{name}")
        else:
            labels.append(name)
    n = len(labels)
    if n == 0:
        return ""
    prefix = f"{n} source{'' if n == 1 else 's'}: "
    return prefix + ", ".join(labels)


def _short_group_label(kind: str, name: str, models: list[str]) -> str:
    """Grouped short label used by the terminal summary and canvas tags."""
    if kind == "concern":
        return f"{name}({','.join(models)})" if models else name
    if kind == "rule":
        return f"rule:{name}"
    return name


def _found_by_terminal(provenance: object) -> str:
    """terminal 'Found by' — grouped short labels joined by ', '."""
    groups = _group_sources(_provenance_sources(provenance))
    return ", ".join(_short_group_label(k, n, m) for k, n, m in groups)


def _found_tags_html(provenance: object) -> str:
    """canvas 'Found by' — colour-coded, escaped <span> tags (grouped)."""
    groups = _group_sources(_provenance_sources(provenance))
    parts: list[str] = []
    for kind, name, models in groups:
        if kind == "concern":
            text = f"{name}({','.join(models)})" if models else name
            cls = "found-tag-concern"
        else:
            # rule + any unrecognised label use the neutral rule styling.
            text = name
            cls = "found-tag-rule"
        parts.append(f'<span class="found-tag {cls}">{html.escape(text)}</span>')
    return "".join(parts)


# ── finding partitioning ─────────────────────────────────────────────────────


def _partition_findings(findings: list) -> tuple[list, list, list]:
    """Split findings into (confirmed, questionable, invalid) display order.

    Confirmed/Questionable share one numbered sequence and sort by
    ``display_number``; Invalid render in a table keyed by ``assessment_id``.
    """
    confirmed = [f for f in findings if f.get("verdict") == "Confirmed"]
    questionable = [f for f in findings if f.get("verdict") == "Questionable"]
    invalid = [f for f in findings if f.get("verdict") == "Invalid"]
    confirmed.sort(key=lambda f: (f.get("display_number") is None, f.get("display_number") or 0))
    questionable.sort(key=lambda f: (f.get("display_number") is None, f.get("display_number") or 0))
    invalid.sort(key=lambda f: str(f.get("assessment_id") or ""))
    return confirmed, questionable, invalid


# ── review.md ────────────────────────────────────────────────────────────────


def _md_finding_block(finding: dict) -> str:
    """One Confirmed/Questionable finding block in the review.md shape."""
    parts: list[str] = []
    parts.append(
        f"### {finding.get('display_number')}. "
        f"[{finding.get('severity', '')}] {finding.get('title', '')}"
    )
    meta = [
        f"**File:** `{_location_str(finding.get('file', ''), finding.get('line'))}`",
        f"**Fix complexity:** {finding.get('fix_complexity', '')}",
        f"**Found by:** {_found_by_md(finding.get('provenance'))}",
    ]
    parts.append("\n".join(meta))
    if finding.get("description"):
        parts.append(finding["description"])
    if finding.get("assessment"):
        parts.append(f"> **Assessment:** {finding['assessment']}")
    if finding.get("suggestion"):
        parts.append(f"**Suggestion:** {finding['suggestion']}")
    parts.append("---")
    return "\n\n".join(parts)


def _md_invalid_block(invalid: list) -> str:
    """The collapsible invalid-findings <details> table for review.md."""
    lines = [
        "<details>",
        f"<summary>{_invalid_summary_text(len(invalid))}</summary>",
        "",
        "| ID | Severity | File | Title | Reason |",
        "|----|----------|------|-------|--------|",
    ]
    for f in invalid:
        lines.append(
            f"| {_md_cell(f.get('assessment_id') or '')} "
            f"| {_md_cell(f.get('severity', ''))} "
            f"| `{_md_cell(_location_str(f.get('file', ''), f.get('line')))}` "
            f"| {_md_cell(f.get('title', ''))} "
            f"| {_md_cell(_flatten(f.get('assessment', '')))} |"
        )
    lines.append("")
    lines.append("</details>")
    return "\n".join(lines)


def _md_quality_block(notes: list) -> str:
    """The Rule Quality Notes section for review.md."""
    lines = ["## Rule Quality Notes", ""]
    for n in notes:
        lines.append(
            f"- **{n.get('rule', '')}**: {n.get('observation', '')} "
            f"— {n.get('suggestion', '')}"
        )
    return "\n".join(lines)


def render_review_markdown(data: dict) -> str:
    """Render the ``review.md`` report from a validated envelope.

    Preserves the heading/field shape the reporter agent used to hand-author, so
    downstream LLM readers (post-comments / post-mortem) and the golden-file test
    keep working. Empty Confirmed/Questionable/Invalid/quality sections are
    omitted (the reporter's "omit a section with no findings" rule).
    """
    run = data.get("run", {})
    findings = data.get("findings", [])
    notes = data.get("rule_quality_notes", []) or []
    confirmed, questionable, invalid = _partition_findings(findings)

    blocks: list[str] = ["# Unified Review Report"]
    blocks.append(
        "\n".join(
            [
                f"**Scope:** {run.get('scope', '')}",
                f"**Date:** {run.get('date', '')}",
                f"**Pipeline:** Discovery ({run.get('rule_count', 0)} rules, "
                f"{run.get('concern_count', 0)} concerns) → Consolidation → Assessment",
            ]
        )
    )
    blocks.append(
        "\n".join(
            [
                "## Summary",
                "",
                "| Verdict | Count |",
                "|---------|-------|",
                f"| ✅ Confirmed | {len(confirmed)} |",
                f"| ❓ Questionable | {len(questionable)} |",
                f"| ❌ Invalid (filtered) | {len(invalid)} |",
            ]
        )
    )
    blocks.append("---")

    if confirmed:
        blocks.append("## Confirmed Findings")
        blocks.extend(_md_finding_block(f) for f in confirmed)
    if questionable:
        blocks.append("## Questionable Findings")
        blocks.extend(_md_finding_block(f) for f in questionable)
    if invalid:
        blocks.append(_md_invalid_block(invalid))
    if notes:
        blocks.append(_md_quality_block(notes))

    return "\n\n".join(blocks) + "\n"


# ── terminal summary ─────────────────────────────────────────────────────────


def render_terminal_summary(data: dict, report_path: str) -> str:
    """Render the user-facing terminal summary string.

    Shape matches the reporter agent's relayed summary: report path, pipeline
    stats, a findings table (ALL Confirmed + Questionable, with a "Found by"
    column), and rule-quality notes. The "relay everything above this line"
    trailer is intentionally NOT emitted here: it was a control instruction for
    an LLM agent's output; render-review is a tool whose stdout is pure data, and
    the orchestrator (SKILL.md) owns the relay-verbatim guarantee.
    """
    run = data.get("run", {})
    findings = data.get("findings", [])
    notes = data.get("rule_quality_notes", []) or []
    confirmed, questionable, _invalid = _partition_findings(findings)
    actionable = confirmed + questionable
    actionable.sort(key=lambda f: (f.get("display_number") is None, f.get("display_number") or 0))

    blocks: list[str] = [f"📄 {report_path}"]
    blocks.append(
        f"{run.get('rule_count', 0)} rules + {run.get('concern_count', 0)} concerns "
        f"→ {run.get('consolidated_count', 0)} unique findings → {len(actionable)} actionable"
    )

    if actionable:
        table = [
            "| # | Verdict | Severity | Found by | File | Issue |",
            "|---|---------|----------|----------|------|-------|",
        ]
        for f in actionable:
            icon = "✅" if f.get("verdict") == "Confirmed" else "❓"
            table.append(
                f"| {f.get('display_number')} "
                f"| {icon} "
                f"| {_md_cell(f.get('severity', ''))} "
                f"| {_md_cell(_found_by_terminal(f.get('provenance')))} "
                f"| {_md_cell(_location_str(f.get('file', ''), f.get('line')))} "
                f"| {_md_cell(f.get('title', ''))} |"
            )
        blocks.append("\n".join(table))
    else:
        blocks.append("✅ No actionable findings.")

    if notes:
        quality = ["📝 Rule Quality Notes"]
        for n in notes:
            quality.append(
                f"- {n.get('rule', '')}: {n.get('observation', '')} — {n.get('suggestion', '')}"
            )
        blocks.append("\n".join(quality))

    return "\n\n".join(blocks) + "\n"


# ── canvas HTML ──────────────────────────────────────────────────────────────


def _strip_template_doc_comment(template: str) -> str:
    """Drop the template's leading documentation comment.

    The version-controlled template documents its placeholders in a leading
    ``<!-- … -->`` block that itself contains the literal FR markers and
    ``{{RUN_ID}}`` (the hand-filled fixture drops it). Removing everything from
    the first comment opener up to ``<head>`` leaves only the body placeholders
    for substitution, so a marker is never filled twice.
    """
    head_idx = template.find("<head>")
    if head_idx == -1:
        return template
    prefix = template[:head_idx]
    comment_idx = prefix.find("<!--")
    if comment_idx == -1:
        return template
    return prefix[:comment_idx] + template[head_idx:]


# ── Detail-sidecar sanitization (Phase 4: nh3, fail-closed) ──────────────────
#
# Reviewed content is untrusted (diffs may come from untrusted PR contributors),
# so an assessor-authored ``A-XX-detail.html`` sidecar is sanitized through nh3
# (Rust `ammonia`) before it is embedded in the canvas. Division of duties (see
# the template CSP <meta> + docs/spec/canvas-review-report.md):
#   * nh3 = script — an HTML + SVG tag/attribute allowlist that keeps inline
#     ``style`` but strips <script>/on*/``javascript:``/SVG animation
#     (animate/set/...)/foreignObject and every external href.
#   * CSP = exfil — img/font/connect-src 'self' data: blocks the residual
#     url()/<img>/fetch beacon vector for the sanitized-but-untrusted markup.
# Fail-closed: when nh3 is not importable, sanitize_detail_html returns None and
# the canvas panel renders the escaped structural text fields only — raw HTML is
# never emitted.

# HTML tags the rich-detail palette (code-block / callout / before-after / flow)
# and ordinary prose need. Scripting, embedding, and form tags are absent, so
# nh3 strips them.
_DETAIL_HTML_TAGS = frozenset({
    "div", "span", "p", "pre", "code", "samp", "kbd", "var",
    "strong", "em", "b", "i", "u", "s", "small", "mark", "sub", "sup",
    "br", "hr", "wbr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "col", "colgroup",
    "a", "abbr", "blockquote", "figure", "figcaption",
})

# SVG tags for flow / timing / call-chain diagrams. The scripting and animation
# vectors — ``script``, ``animate*``/``set``, and the ``foreignObject`` HTML
# escape hatch — are deliberately excluded (see _DETAIL_CLEAN_CONTENT_TAGS).
_DETAIL_SVG_TAGS = frozenset({
    "svg", "g", "defs", "symbol", "use", "title", "desc",
    "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan",
    "marker", "linearGradient", "radialGradient", "stop", "pattern", "clipPath", "mask",
})

_DETAIL_TAGS = _DETAIL_HTML_TAGS | _DETAIL_SVG_TAGS

# Attributes allowed on every tag: presentational/global HTML, ARIA, and the SVG
# geometry/paint/text presentation set. None of these can execute script (event
# handlers are simply absent, so nh3 drops them); ``style`` is kept on purpose
# (CSP owns the exfil vector). They are inert on HTML elements, so allowing the
# SVG attributes globally keeps the map small without widening the attack surface.
_DETAIL_GLOBAL_ATTRS = frozenset({
    "class", "style", "id", "title", "role", "lang", "dir",
    "aria-hidden", "aria-label", "aria-labelledby", "aria-describedby",
    # SVG geometry
    "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "dx", "dy",
    "width", "height", "d", "points", "transform", "viewBox", "preserveAspectRatio",
    "offset", "fx", "fy", "refX", "refY", "orient", "markerWidth", "markerHeight",
    "markerUnits", "gradientUnits", "gradientTransform", "spreadMethod",
    "patternUnits", "patternContentUnits", "clipPathUnits", "maskUnits",
    # SVG paint / stroke
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity",
    "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "stroke-dashoffset",
    "stroke-miterlimit", "opacity", "color", "stop-color", "stop-opacity",
    "marker-start", "marker-mid", "marker-end", "clip-path", "clip-rule", "mask",
    # SVG text
    "text-anchor", "dominant-baseline", "alignment-baseline", "baseline-shift",
    "font-family", "font-size", "font-weight", "font-style", "letter-spacing",
    "xml:space", "xmlns", "xmlns:xlink",
    # HTML table layout
    "colspan", "rowspan", "span", "scope", "headers", "abbr",
})

# href/xlink:href are allowed only where references make sense; their *values*
# are still restricted to same-document fragments by _detail_attribute_filter.
_DETAIL_ATTRIBUTES = {
    "*": _DETAIL_GLOBAL_ATTRS,
    "a": frozenset({"href"}),
    "use": frozenset({"href", "xlink:href"}),
}

# Tags whose entire CONTENT (not just the tag) is removed — scripting, raw CSS,
# SVG animation, and the foreignObject HTML-in-SVG escape hatch. None overlap the
# allowlist above (nh3 forbids a tag appearing in both sets).
_DETAIL_CLEAN_CONTENT_TAGS = frozenset({
    "script", "style", "foreignObject",
    "animate", "animateTransform", "animateMotion", "set", "mpath",
})

# URL-bearing attributes whose value must be a same-document fragment (``#...``).
_DETAIL_URL_ATTRS = frozenset({"href", "xlink:href", "src"})


def _detail_attribute_filter(tag: str, attr: str, value: str) -> str | None:
    """nh3 per-attribute filter enforcing the "no external href" rule.

    Any href/xlink:href/src is kept only when it is a same-document fragment
    (``#id`` — e.g. an SVG ``<use>`` or gradient reference); every external,
    ``javascript:``, or ``data:`` target is dropped. All other attributes pass
    through unchanged (the tag/attribute allowlist already constrained them).
    """
    if attr.lower() in _DETAIL_URL_ATTRS:
        return value if value.startswith("#") else None
    return value


def sanitize_detail_html(raw: str) -> str | None:
    """Sanitize an assessor-authored detail fragment via nh3 (fail-closed).

    Returns the nh3-cleaned HTML/SVG, or ``None`` when nh3 is unavailable — the
    fail-closed contract: without the sanitizer the caller emits the escaped
    structural text fields only, never raw HTML.
    """
    if _nh3 is None:
        return None
    return _nh3.clean(
        raw,
        tags=set(_DETAIL_TAGS),
        clean_content_tags=set(_DETAIL_CLEAN_CONTENT_TAGS),
        attributes={tag: set(attrs) for tag, attrs in _DETAIL_ATTRIBUTES.items()},
        attribute_filter=_detail_attribute_filter,
        strip_comments=True,
        link_rel="noopener noreferrer",
    )


def _detail_sidecar_path(run_dir: str, assessment_id: str) -> str:
    """Path to a finding's optional rich-detail sidecar within the run dir."""
    return os.path.join(run_dir, "assessments", f"{assessment_id}-detail.html")


def _resolve_finding_detail(run_dir: str | None, finding: dict) -> str | None:
    """Load + sanitize a finding's rich-detail sidecar, or ``None``.

    Returns ``None`` — so the caller falls back to escaped text — when: the
    finding has no detail, there is no run dir / assessment id to locate it, the
    ``assessment_id`` is not a safe filename token, the sidecar file is missing
    or unreadable, nh3 is unavailable (fail-closed), or the fragment sanitizes to
    nothing.
    """
    if not run_dir or not finding.get("has_detail"):
        return None
    assessment_id = finding.get("assessment_id")
    if not assessment_id:
        return None
    # assessment_id locates a file, and records.json is authored by an LLM from
    # untrusted diff content, so a prompt-injected id must not traverse out of
    # {run_dir}/assessments (e.g. "../../etc/passwd") or smuggle a null byte
    # (which open() raises ValueError on, aborting the always-on canvas render).
    # The assessor convention is a plain "A-NN" token; reject anything else.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", assessment_id):
        return None
    try:
        with open(_detail_sidecar_path(run_dir, assessment_id), encoding="utf-8") as fh:
            raw = fh.read()
    except (OSError, ValueError):
        return None
    cleaned = sanitize_detail_html(raw)
    if cleaned is None or not cleaned.strip():
        return None
    return cleaned


def _canvas_finding_block(
    finding: dict, detail_html: str | None = None, dimmed: bool = False
) -> str:
    """One ``.finding`` accordion block for the canvas (every text field escaped).

    ``detail_html`` is the already-nh3-sanitized rich-detail fragment (or
    ``None``). When present it is embedded after the structural fields, wrapped in
    ``<div class="rich-detail">``; when absent the panel renders the escaped
    structural fields only — the fail-closed "plain escaped text" behaviour.

    ``dimmed`` bakes the ``dimmed`` class onto the block so a finding the user
    persisted as *disregarded* (run state, read back by render-review) stays dimmed
    across every re-render — not just within the live JS session. The action-bar
    script seeds its own disregarded set from these server-rendered classes, so the
    persisted dim survives a cold canvas load too, not only an in-session morph.
    """
    rid = finding.get("record_id", "")
    number = finding.get("display_number")
    title = finding.get("title", "")
    severity = finding.get("severity", "")
    location = _location_str(finding.get("file", ""), finding.get("line"))
    aria = f"Select finding {number}: {title}"

    sections = [
        "          <div class=\"detail-section\">\n"
        "            <div class=\"detail-label\">Location</div>\n"
        f"            <span class=\"detail-file\">{html.escape(location)}</span>"
        f" · {html.escape(str(finding.get('fix_complexity', '')))}\n"
        "          </div>"
    ]
    if finding.get("description"):
        sections.append(
            "          <div class=\"detail-section\">\n"
            "            <div class=\"detail-label\">Description</div>\n"
            f"            <p>{html.escape(finding['description'])}</p>\n"
            "          </div>"
        )
    if finding.get("assessment"):
        sections.append(
            "          <div class=\"detail-assessment\">\n"
            "            <div class=\"detail-label\">Assessment</div>\n"
            f"            <p>{html.escape(finding['assessment'])}</p>\n"
            "          </div>"
        )
    if finding.get("suggestion"):
        sections.append(
            "          <div class=\"detail-suggestion\">\n"
            "            <div class=\"detail-label\">Suggestion</div>\n"
            f"            <p>{html.escape(finding['suggestion'])}</p>\n"
            "          </div>"
        )
    if detail_html:
        # Already nh3-sanitized; embedded raw (not escaped) inside the palette
        # wrapper. html.escape on it would defeat the whole rich-detail feature.
        sections.append(
            "          <div class=\"rich-detail\">\n"
            f"{detail_html}\n"
            "          </div>"
        )
    detail_content = "\n".join(sections)

    classes = "finding dimmed" if dimmed else "finding"
    return (
        f'      <div class="{classes}" data-record-id="{html.escape(str(rid), quote=True)}">\n'
        f'        <input type="checkbox" class="row-cb" aria-label="{html.escape(aria, quote=True)}">\n'
        '        <details class="finding-d" name="findings">\n'
        '          <summary class="finding-summary">\n'
        '            <span class="caret" aria-hidden="true"></span>\n'
        f'            <span class="num">{html.escape(str(number))}</span>\n'
        f'            <span class="sev {html.escape(_sev_class(severity), quote=True)}">{html.escape(str(severity))}</span>\n'
        f'            <span class="found-by">{_found_tags_html(finding.get("provenance"))}</span>\n'
        f'            <span class="title">{html.escape(str(title))}</span>\n'
        '          </summary>\n'
        '          <div class="detail-content">\n'
        f"{detail_content}\n"
        '          </div>\n'
        '        </details>\n'
        '      </div>'
    )


def _canvas_invalid_row(finding: dict) -> str:
    """One ``<tr>`` for the canvas invalid-findings table."""
    aid = finding.get("assessment_id") or ""
    severity = finding.get("severity", "")
    return (
        f"        <tr><td>{html.escape(str(aid))}</td>"
        f'<td><span class="sev {html.escape(_sev_class(severity), quote=True)}">{html.escape(_sev_abbrev(severity))}</span></td>'
        f"<td>{html.escape(str(finding.get('title', '')))}</td>"
        f"<td>{html.escape(_flatten(finding.get('assessment', '')))}</td></tr>"
    )


def _canvas_quality_item(note: dict) -> str:
    """One ``.quality-item`` block for the canvas."""
    return (
        f'    <div class="quality-item"><strong>{html.escape(str(note.get("rule", "")))}</strong>: '
        f'{html.escape(str(note.get("observation", "")))} — '
        f'{html.escape(str(note.get("suggestion", "")))}</div>'
    )


def render_canvas_html(
    data: dict,
    template: str,
    details: dict[str, str] | None = None,
    disregarded: set[str] | None = None,
    parent_origin: str = DEFAULT_PARENT_ORIGIN,
) -> str:
    """Fill the canvas template with pre-rendered, escaped markup.

    A single-pass regex substitution replaces every placeholder exactly once, so
    injected (already-escaped) content is never re-scanned for further markers.
    ``{{RUN_ID}}`` is escaped for an attribute context; escaped finding text can
    never forge a ``<!-- FR:* -->`` marker because ``html.escape`` turns ``<``
    into ``&lt;``.

    ``parent_origin`` is baked into ``{{PARENT_ORIGIN}}`` (the canvas
    ``data-parent-origin`` attribute): the action bar pins it as the postMessage
    target origin and validates it on inbound messages, so the channel is
    origin-validated rather than a wildcard ``"*"`` broadcast.

    ``details`` maps a finding's ``record_id`` to its already-nh3-sanitized
    rich-detail fragment (built by the render-review CLI handler, which owns the
    file IO + sanitization). Absent or unmapped findings render text-only — the
    fail-closed default, so callers without sidecars get the Phase 3 behaviour.

    ``disregarded`` is the set of ``record_id``s persisted as disregarded run state
    (read back from ``run-state.json``); their ``.finding`` block is rendered with
    the ``dimmed`` class so the dim survives across re-renders. Passed as data (no
    file IO here) to keep this function pure and testable, mirroring ``details``.
    """
    details = details or {}
    disregarded = disregarded or set()
    run = data.get("run", {})
    findings = data.get("findings", [])
    notes = data.get("rule_quality_notes", []) or []
    confirmed, questionable, invalid = _partition_findings(findings)
    actionable_count = len(confirmed) + len(questionable)

    def block(finding: dict) -> str:
        rid = finding.get("record_id")
        return _canvas_finding_block(finding, details.get(rid), dimmed=rid in disregarded)

    meta = (
        f"Scope: {html.escape(str(run.get('scope', '')))} · "
        f"{html.escape(str(run.get('date', '')))} · "
        f"{run.get('rule_count', 0)} rules + {run.get('concern_count', 0)} concerns → "
        f"{run.get('consolidated_count', 0)} unique → {actionable_count} actionable"
    )
    badges = (
        f'<span class="summary-badge badge-confirmed"><span class="count">{len(confirmed)}</span> Confirmed</span>'
        f'<span class="summary-badge badge-questionable"><span class="count">{len(questionable)}</span> Questionable</span>'
        f'<span class="summary-badge badge-invalid"><span class="count">{len(invalid)}</span> Invalid</span>'
    )

    replacements = {
        "{{RUN_ID}}": html.escape(str(run.get("run_id", "")), quote=True),
        "{{PARENT_ORIGIN}}": html.escape(str(parent_origin), quote=True),
        "<!-- FR:META -->": meta,
        "<!-- FR:SUMMARY_BADGES -->": badges,
        "<!-- FR:CONFIRMED_COUNT -->": str(len(confirmed)),
        "<!-- FR:CONFIRMED_ROWS -->": "\n".join(block(f) for f in confirmed),
        "<!-- FR:QUESTIONABLE_COUNT -->": str(len(questionable)),
        "<!-- FR:QUESTIONABLE_ROWS -->": "\n".join(block(f) for f in questionable),
        "<!-- FR:INVALID_SUMMARY -->": html.escape(_invalid_summary_text(len(invalid))),
        "<!-- FR:INVALID_ROWS -->": "\n".join(_canvas_invalid_row(f) for f in invalid),
        "<!-- FR:QUALITY_SUMMARY -->": html.escape(f"Rule Quality Notes ({len(notes)})"),
        "<!-- FR:QUALITY_NOTES -->": "\n".join(_canvas_quality_item(n) for n in notes),
    }

    stripped = _strip_template_doc_comment(template)
    # Longest-first keeps a marker that is a prefix of another from matching early.
    pattern = re.compile("|".join(re.escape(k) for k in sorted(replacements, key=len, reverse=True)))
    return pattern.sub(lambda m: replacements[m.group(0)], stripped)


# ── CLI handler ──────────────────────────────────────────────────────────────


def _write_text(path: str, text: str) -> None:
    """Write *text* to *path* as UTF-8, creating parent directories as needed."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _emit_stdout(text: str) -> None:
    """Write *text* to stdout as UTF-8 regardless of the ambient console encoding.

    The orchestrator captures this stream (a pipe) to relay the summary verbatim,
    so on Windows the ambient encoding is cp1252 and ``print`` would raise
    ``UnicodeEncodeError`` on the summary's glyphs (the doc/pipeline/verdict
    emoji and the ``->`` arrow). Writing UTF-8 bytes to the binary buffer keeps
    the relayed bytes deterministic; fall back to a text write for capture
    harnesses that expose no binary buffer.
    """
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode("utf-8"))
        buffer.flush()
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


def capabilities(args: argparse.Namespace) -> None:
    """CLI: report optional-capability availability as JSON to stdout.

    The orchestrator runs this BEFORE assessment to decide whether the assessor
    may author rich HTML/SVG detail sidecars: ``rich_html`` is on only when nh3
    is importable, because render-review fails closed to escaped text without it.
    Pure-ASCII JSON, so a plain ``print`` is safe under any console encoding.
    """
    nh3_available = _nh3 is not None
    payload = {
        "nh3": nh3_available,
        "rich_html": nh3_available,
        "nh3_version": getattr(_nh3, "__version__", None) if nh3_available else None,
        "schema_version": RECORDS_SCHEMA_VERSION,
    }
    print(json.dumps(payload))


def render_review(args: argparse.Namespace) -> None:
    """CLI: render review.md, the terminal summary, and the canvas from records.json.

    On a validation failure, emits the same structured per-record error JSON as
    ``validate-records`` to stderr and exits 1 (the orchestrator retries the
    reporter, then falls back to the legacy markdown path). On success, writes
    review.md and the always-on canvas file, then prints the terminal summary to
    stdout for the orchestrator to relay.
    """
    records_path: str = args.records
    data, errors = load_and_validate_records(records_path)

    if errors:
        payload = {
            "valid": False,
            "schema_version": RECORDS_SCHEMA_VERSION,
            "records_path": str(records_path),
            "error_count": len(errors),
            "errors": errors,
        }
        print(json.dumps(payload, indent=2), file=sys.stderr)
        sys.exit(1)

    run_dir = args.run_dir or os.path.dirname(records_path) or "."
    review_out = args.review_out or os.path.join(run_dir, "review.md")
    canvas_out = args.canvas_out or os.path.join(args.repo, DEFAULT_CANVAS_RELPATH)
    template_path = args.template or str(CANVAS_TEMPLATE_PATH)
    # Trusted parent origin pinned into the canvas postMessage channel (getattr keeps
    # callers that build a bare Namespace working; falls back to the safe default).
    parent_origin = getattr(args, "parent_origin", None) or DEFAULT_PARENT_ORIGIN

    # All-or-nothing: read everything that can fail BEFORE writing any artifact,
    # so a missing/unreadable --template fails the whole render (like the
    # validation-failure path above, which also writes nothing) instead of
    # leaving review.md half-written beside an unstructured traceback. The
    # template read is the only post-validation step that raises an uncaught
    # OSError; the sidecar/run-state reads below are individually fail-safe.
    with open(template_path, encoding="utf-8") as fh:
        template = fh.read()

    # Sanitize each finding's optional rich-detail sidecar (nh3, fail-closed):
    # has_detail findings get their {run_dir}/assessments/{A-XX}-detail.html
    # loaded + cleaned; anything else (no nh3, missing file, empty) renders as
    # escaped text only. Keyed by stable record_id for the canvas renderer.
    findings = data.get("findings", []) if isinstance(data, dict) else []
    details: dict[str, str] = {}
    for finding in findings:
        cleaned = _resolve_finding_detail(run_dir, finding)
        if cleaned is not None:
            details[finding.get("record_id")] = cleaned

    # Disregarded run state: render-review re-applies the user's persisted
    # "disregard" decisions (written by `validate-action --apply-disregard`) so the
    # canvas dims those findings on every re-render. Fail-open (empty) when the
    # state file is absent/unreadable or belongs to a different run_id.
    run = data.get("run", {}) if isinstance(data, dict) else {}
    expected_run_id = run.get("run_id") if isinstance(run, dict) else None
    run_state = load_run_state(run_dir, expected_run_id=expected_run_id)
    disregarded = set(run_state.get("disregarded", []))

    # Render both artifacts in memory, THEN write — so any render-time failure
    # also leaves no partial output. review.md is Markdown (raw text fields, no
    # HTML escaping); the canvas is ALWAYS written (gitignored, inert when
    # Treemon is down).
    review_md = render_review_markdown(data)
    canvas_html = render_canvas_html(data, template, details, disregarded, parent_origin)
    _write_text(review_out, review_md)
    _write_text(canvas_out, canvas_html)

    # Terminal summary -> stdout for verbatim relay (UTF-8, locale-independent).
    _emit_stdout(render_terminal_summary(data, review_out))


# ---------------------------------------------------------------------------
# validate-action (Phase 6): the canvas action-bar round-trip.
#
# The canvas posts {action, run_id, record_ids[], instructions} to the
# orchestrator. Before the orchestrator does anything (and only after a human
# confirms), it calls `validate-action` to validate/expand the payload against
# records.json: the posted run_id must match the rendered run (rejecting a forged
# action from injected canvas JS), every record_id must exist, and each resolves
# to its file/line/title/suggestion. Python's role stays mechanical — it validates
# a schema-defined contract and resolves stable ids; it never executes anything.
#
# `disregard` is the one action with persisted state: `--apply-disregard` records
# the ids in run-state.json so render-review re-applies the dim across re-renders.
# See docs/spec/canvas-review-report.md (postMessage actions + Security Model).
# ---------------------------------------------------------------------------

# Per-run state the canvas re-applies across renders (currently: disregarded ids).
# Lives beside records.json in the run directory.
RUN_STATE_FILENAME = "run-state.json"


def _run_state_path(run_dir: str | None) -> str:
    """Path to the run-state file inside *run_dir* (defaults to the cwd)."""
    return os.path.join(run_dir or ".", RUN_STATE_FILENAME)


def load_run_state(run_dir: str | None, expected_run_id: str | None = None) -> dict:
    """Read ``{run_dir}/run-state.json``; fail-open to an empty state.

    The persisted run state holds the ``disregarded`` record_ids the canvas dims
    on every re-render. Any read/parse problem — or a ``run_id`` that does not
    match *expected_run_id* (stale state from a different run) — yields an empty
    state: disregard is an additive canvas affordance, never a hard dependency, so
    a missing or unreadable file must never block the always-written canvas.
    """
    empty: dict = {"disregarded": []}
    path = _run_state_path(run_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    if expected_run_id is not None:
        # When the caller names the run it expects, honour the state only if it is
        # stamped with that exact run_id. A state file with a missing/blank or
        # mismatched run_id is treated as stale/foreign (empty) — render-review
        # always passes expected_run_id, so this never dims another run's findings.
        state_run_id = data.get("run_id")
        if not (_is_nonempty_str(state_run_id) and state_run_id == expected_run_id):
            return empty
    raw = data.get("disregarded")
    disregarded = [r for r in raw if _is_nonempty_str(r)] if isinstance(raw, list) else []
    return {"disregarded": disregarded}


def persist_disregard(run_dir: str | None, run_id: str, record_ids: list[str]) -> dict:
    """Merge *record_ids* into the run-state's disregarded set and write it.

    Disregard is monotonic within a run (add-only): the existing set is preserved
    and new ids appended in first-seen order, so render-review re-applies the dim
    on every subsequent re-render. Returns the persisted state dict. The caller is
    expected to have validated *record_ids* (via :func:`validate_action`) first.
    """
    existing = load_run_state(run_dir, expected_run_id=run_id)
    disregarded = list(existing.get("disregarded", []))
    seen = set(disregarded)
    for rid in record_ids:
        if _is_nonempty_str(rid) and rid not in seen:
            seen.add(rid)
            disregarded.append(rid)
    state = {
        "schema_version": RECORDS_SCHEMA_VERSION,
        "run_id": run_id,
        "disregarded": disregarded,
    }
    _write_text(_run_state_path(run_dir), json.dumps(state, indent=2) + "\n")
    return state


def _resolve_action_finding(finding: dict) -> dict:
    """Resolve a finding to the fields the orchestrator needs to act on it.

    The spec requires file/line/title/suggestion; the rest (severity, verdict,
    fix_complexity, the stable ids, display_number) give the orchestrator enough
    context to describe the action to the human and to scope a fix precisely.
    """
    return {
        "record_id": finding.get("record_id"),
        "assessment_id": finding.get("assessment_id"),
        "display_number": finding.get("display_number"),
        "title": finding.get("title", ""),
        "file": finding.get("file", ""),
        "line": finding.get("line"),
        "severity": finding.get("severity", ""),
        "verdict": finding.get("verdict", ""),
        "fix_complexity": finding.get("fix_complexity", ""),
        "suggestion": finding.get("suggestion", ""),
    }


def validate_action(
    data: object,
    run_id: object,
    record_ids: list[str],
    *,
    action: str | None = None,
    instructions: str = "",
) -> tuple[dict | None, list[dict]]:
    """Validate/expand a posted canvas action against a records.json envelope.

    Returns ``(expanded, errors)``. ``errors`` is a list of structured per-record
    error dicts (same shape as :func:`validate_records`, scope ``"action"``); when
    it is empty, ``expanded`` is the resolved action — every targeted ``record_id``
    resolved to file/line/title/suggestion. A mismatched/forged ``run_id`` or any
    unknown ``record_id`` is rejected. Never raises; never executes anything.
    """
    errors: list[dict] = []

    if not isinstance(data, dict):
        errors.append(
            _records_error(
                "action",
                None,
                "$",
                "run_id",
                f"records.json root must be a JSON object, got {_json_type_name(data)}",
            )
        )
        return None, errors

    run = data.get("run")
    actual_run_id = run.get("run_id") if isinstance(run, dict) else None

    # run_id must match the rendered run — this is the forgery gate.
    if not _is_nonempty_str(run_id):
        errors.append(
            _records_error(
                "action", None, "run_id", "run_id",
                "run_id is required to validate a canvas action",
            )
        )
    elif not _is_nonempty_str(actual_run_id):
        errors.append(
            _records_error(
                "action", None, "run_id", "run_id",
                "records.json has no run.run_id to match the posted action against",
            )
        )
    elif run_id != actual_run_id:
        errors.append(
            _records_error(
                "action", None, "run_id", "run_id",
                f"run_id mismatch: posted {run_id!r} does not match records.json "
                f"run_id {actual_run_id!r} (rejecting a possibly forged action)",
            )
        )

    # Index findings by stable record_id so each posted id resolves (or doesn't).
    findings = data.get("findings")
    by_id: dict[str, dict] = {}
    if isinstance(findings, list):
        for finding in findings:
            if isinstance(finding, dict):
                rid = finding.get("record_id")
                if _is_nonempty_str(rid):
                    by_id.setdefault(rid, finding)

    resolved: list[dict] = []
    if not record_ids:
        errors.append(
            _records_error(
                "action", None, "record_ids", "record_ids",
                "no record_ids provided; a canvas action must target at least one finding",
            )
        )
    for i, rid in enumerate(record_ids):
        if not _is_nonempty_str(rid):
            errors.append(
                _records_error(
                    "action", i, f"record_ids[{i}]", "record_id",
                    "record_id must be a non-empty string",
                    record_id=rid if isinstance(rid, str) else None,
                )
            )
            continue
        finding = by_id.get(rid)
        if finding is None:
            errors.append(
                _records_error(
                    "action", i, f"record_ids[{i}]", "record_id",
                    f"unknown record_id {rid!r}: not present in records.json",
                    record_id=rid,
                )
            )
        else:
            resolved.append(_resolve_action_finding(finding))

    if errors:
        return None, errors

    expanded = {
        "valid": True,
        "schema_version": RECORDS_SCHEMA_VERSION,
        "action": action,
        "run_id": run_id,
        "instructions": instructions or "",
        "record_count": len(resolved),
        "findings": resolved,
    }
    return expanded, []


def _load_records_only(path: str | os.PathLike) -> tuple[object, dict | None]:
    """Read + JSON-parse *path* without full schema validation.

    The action round-trip runs against an already-rendered (hence already
    schema-validated) records.json, so re-validating the whole envelope here would
    let an unrelated field error block a legitimate action. Returns ``(data,
    error)`` where ``error`` is a single envelope-scoped dict on a read/parse
    failure (``data`` then ``None``), mirroring :func:`load_and_validate_records`.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None, _records_error(
            "envelope", None, "$", None, f"records.json not found: {path}"
        )
    except OSError as exc:
        return None, _records_error(
            "envelope", None, "$", None, f"could not read records.json: {exc}"
        )
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, _records_error(
            "envelope", None, "$", None, f"records.json is not valid JSON: {exc}"
        )


def _split_record_ids(raw: str | None) -> list[str]:
    """Parse the comma-separated ``--record-ids`` value into a de-duped list.

    record_ids are per-run tokens (``r1``, ``r2``, …) so a comma is a safe
    separator. Order is preserved (first-seen) and blanks/dupes are dropped.
    """
    if not raw:
        return []
    seen: set[str] = set()
    ids: list[str] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok and tok not in seen:
            seen.add(tok)
            ids.append(tok)
    return ids


def _emit_action_error(
    path: str, run_id: object, action: str | None, errors: list[dict]
) -> None:
    """Print the structured action-validation failure payload to stderr."""
    payload = {
        "valid": False,
        "schema_version": RECORDS_SCHEMA_VERSION,
        "records_path": str(path),
        "run_id": run_id,
        "action": action,
        "error_count": len(errors),
        "errors": errors,
    }
    print(json.dumps(payload, indent=2), file=sys.stderr)


def validate_action_command(args: argparse.Namespace) -> None:
    """CLI: validate/expand a posted canvas action against records.json.

    On success prints the resolved action JSON to stdout (exit 0). On a forged
    run_id, unknown record_id, or a missing/unparseable records.json, prints the
    structured errors to stderr and exits 1 — so the orchestrator rejects the
    action instead of executing it. ``--apply-disregard`` additionally persists the
    (validated) ids as disregarded run state after a successful validation; it is
    the only side effect, gated behind the human-confirmation step in SKILL.md.
    """
    path: str = args.records
    posted_run_id = args.run_id
    action = args.action
    instructions = args.instructions or ""
    record_ids = _split_record_ids(args.record_ids)

    data, load_error = _load_records_only(path)
    if load_error is not None:
        _emit_action_error(path, posted_run_id, action, [load_error])
        sys.exit(1)

    resolved, errors = validate_action(
        data, posted_run_id, record_ids, action=action, instructions=instructions
    )
    if errors:
        _emit_action_error(path, posted_run_id, action, errors)
        sys.exit(1)

    assert resolved is not None  # errors empty => resolved populated
    resolved["records_path"] = str(path)

    # Disregard is the one action that persists state. Only after a successful
    # validation (so a forged run_id / unknown id can never write state).
    if getattr(args, "apply_disregard", False):
        run_dir = args.run_dir or os.path.dirname(path) or "."
        state = persist_disregard(run_dir, posted_run_id, record_ids)
        resolved["disregarded"] = state.get("disregarded", [])
        resolved["run_state_path"] = _run_state_path(run_dir)

    # ensure_ascii (default) keeps this pure-ASCII, so a plain print is encoding
    # safe even when the orchestrator pipes stdout under a non-UTF-8 console.
    print(json.dumps(resolved, indent=2))


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

    # capabilities subcommand
    capabilities_parser = subparsers.add_parser(
        "capabilities",
        help="Report optional-capability availability (nh3 / rich_html) as JSON",
    )
    capabilities_parser.set_defaults(func=capabilities)

    # render-review subcommand
    render_parser = subparsers.add_parser(
        "render-review",
        help="Render review.md, the terminal summary, and the canvas HTML from records.json",
    )
    render_parser.add_argument(
        "--records",
        required=True,
        help="Path to the records.json envelope to render",
    )
    render_parser.add_argument(
        "--run-dir",
        default=None,
        help="Run directory (default: the records.json file's directory)",
    )
    render_parser.add_argument(
        "--repo",
        default=".",
        help="Repository root, used to resolve the default canvas path (default: .)",
    )
    render_parser.add_argument(
        "--review-out",
        default=None,
        help="Output path for review.md (default: {run_dir}/review.md)",
    )
    render_parser.add_argument(
        "--canvas-out",
        default=None,
        help="Output path for the canvas HTML (default: {repo}/.agents/canvas/focused-review.html)",
    )
    render_parser.add_argument(
        "--template",
        default=None,
        help="Canvas template path (default: the packaged review-canvas.html)",
    )
    render_parser.add_argument(
        "--parent-origin",
        default=DEFAULT_PARENT_ORIGIN,
        help=(
            "Trusted Treemon parent-app origin pinned into the canvas postMessage "
            "channel; replaces the wildcard target origin (default: %(default)s)"
        ),
    )
    render_parser.set_defaults(func=render_review)

    # validate-action subcommand
    action_parser = subparsers.add_parser(
        "validate-action",
        help="Validate/expand a posted canvas action (fix/disregard/document) against records.json",
    )
    action_parser.add_argument(
        "--records",
        required=True,
        help="Path to the records.json envelope the canvas was rendered from",
    )
    action_parser.add_argument(
        "--run-id",
        required=True,
        help="The run_id from the posted action (must match records.json's run.run_id)",
    )
    action_parser.add_argument(
        "--record-ids",
        required=True,
        help="Comma-separated stable record_ids the action targets (e.g. r1,r3,r7)",
    )
    action_parser.add_argument(
        "--action",
        default=None,
        help="The namespaced action string (focused-review.fix/.disregard/.document)",
    )
    action_parser.add_argument(
        "--instructions",
        default="",
        help="Free-text instructions from the action bar (echoed back in the result)",
    )
    action_parser.add_argument(
        "--apply-disregard",
        action="store_true",
        help="After validation, persist the record_ids as disregarded run state "
             "(canvas dims them across re-renders). Use only after human confirmation.",
    )
    action_parser.add_argument(
        "--run-dir",
        default=None,
        help="Run directory for run-state.json (default: the records.json file's directory)",
    )
    action_parser.set_defaults(func=validate_action_command)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
