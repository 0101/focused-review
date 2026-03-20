#!/usr/bin/env python3
"""Focused review helper — deterministic operations for the focused-review plugin."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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
DEFAULT_CONCERNS_DIR = "review/concerns/"
BUILTIN_CONCERNS_DIR = Path(__file__).resolve().parent.parent / "defaults" / "concerns"

COPILOT_CMD = os.environ.get("COPILOT_CMD", "copilot")

_COMPLETE_SENTINEL = "Review Status: This review is complete."
_INCOMPLETE_SENTINEL = "Review Status: This review is incomplete"

CONCERN_HARD_TIMEOUT_SECS = int(os.environ.get("CONCERN_HARD_TIMEOUT", "1200"))
CONCERN_SOFT_TIMEOUT_SECS = int(os.environ.get("CONCERN_SOFT_TIMEOUT", "600"))
CONCERN_MAX_WORKERS = int(os.environ.get("CONCERN_MAX_WORKERS", "4"))

# Shorthand model names used in concern files → full CLI model identifiers.
# Unknown names pass through unchanged so users can specify full names directly.
MODEL_MAP: dict[str, str] = {
    "opus": "claude-opus-4.6",
    "sonnet": "claude-sonnet-4.6",
    "haiku": "claude-haiku-4.5",
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
                config_path = str(candidate).replace("\\", "/")
                return {
                    "rules_dir": rules_dir,
                    "sources": sources,
                    "concerns_dir": concerns_dir,
                    "config_file": config_path,
                }
            except (json.JSONDecodeError, AttributeError):
                pass

    return {
        "rules_dir": DEFAULT_RULES_DIR,
        "sources": [],
        "concerns_dir": DEFAULT_CONCERNS_DIR,
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

    Handles the common case where ``**/*.ext`` should also match files
    at the repository root (``PurePosixPath.match`` requires at least
    one directory segment for ``**/``).

    Negation patterns (``!**/*Tests*.cs``) invert the match: returns
    True for files that do NOT match the inner pattern.
    """
    if glob_pattern.startswith("!"):
        return not _file_matches_glob(filepath, glob_pattern[1:])
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
                "model": meta.get("model", "inherit"),
                "autofix": meta.get("autofix", False),
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

    Each prompt combines the concern body with the review context
    (changed files list + diff locations).  Writes files to
    ``work_dir/prompts/{concern}--{model}.md``.

    Returns a list of dispatch entries suitable for ``concern-dispatch.json``.
    """
    prompts_dir = work_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    # clean previous run
    _clean_dir(prompts_dir)

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

        for model in models:
            prompt_name = f"{concern['name']}--{model}"
            prompt_path = prompts_dir / f"{prompt_name}.md"

            lines = [
                body.strip(),
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
            plan_rel = _posix(
                work_dir / "scratchpad" / f"{concern['name']}--{model}--plan.md",
                relative_to=repo,
            )
            lines.extend([
                "",
                "---",
                "",
                "## Output Destination",
                "",
                f"Report file: `{finding_rel}`",
                f"Plan file: `{plan_rel}`",
                "",
                "On a **fresh run** (files don't exist), use the `create` tool to "
                "write your report. On a **continuation** (files already exist), use "
                "the `edit` tool to update/append to the existing report.",
                "Follow the Output Format above exactly.",
                "Do NOT print your findings to stdout — write them to the file.",
            ])

            # Inject the Working Protocol so agents write incrementally
            # and can be continued across multiple invocations.
            soft_timeout = CONCERN_SOFT_TIMEOUT_SECS
            lines.extend([
                "",
                "---",
                "",
                "## Working Protocol",
                "",
                "Follow this protocol exactly for every review invocation.",
                "",
                "### 1. Start Timer + Continuation Check",
                "",
                "Do these two things in your **very first tool call** (parallel):",
                "",
                "1. Start a background timer:",
                f'   `python -c "import time; time.sleep({soft_timeout})"` '
                "(async, non-detached)",
                "2. Check if your **report file** and **plan file** already exist",
                "",
                "Then:",
                "",
                f"- **Neither exists** → fresh review. Go to step 2.",
                "- **Both exist** → continuation. Read them, skip to step 3, "
                "resume from unchecked groups.",
                "- **Only one exists** → delete it, treat as fresh.",
                "",
                "### 2. Plan Your Work (BEFORE reading any code)",
                "",
                "**Your next tool call MUST create the plan file.** Do NOT read "
                "any diffs or source files first. Use only the Changed Files list "
                "above to create your plan.",
                "",
                "1. Group the changed files into logical clusters (by feature, "
                "module, or directory).",
                "2. Write the plan as a checklist to:",
                f"   `{plan_rel}`",
                "3. **Assess scope**: if you have many groups, you may not finish "
                "them all. Prioritize groups most likely to contain issues for "
                "your concern type. Put those first.",
                "",
                "### 3. Review One Group at a Time",
                "",
                "For each group in plan order:",
                "",
                "1. Read the diffs for this group. Trace into source files as "
                "needed — go as deep as the code requires to confirm or rule "
                "out a bug.",
                "2. **When you're done reviewing this group**, write to your "
                "report file before moving on:",
                f"   - Found issues → append findings to `{finding_rel}` "
                "(using the Output Format above).",
                "   - No issues → append: `<!-- no findings: [group name] -->`",
                "3. Mark the group `[x]` in your plan file.",
                "4. Move to the next group.",
                "",
                "### 4. React to Timer",
                "",
                "When the timer notification arrives, **stop investigating "
                "immediately**. Do not read any more files or run any more "
                "searches. Your process will be killed shortly after this signal.",
                "",
                "Write to disk NOW:",
                "",
                "1. Any confirmed findings for the current group (full format).",
                "2. Any hypothesis you are actively investigating — write it as "
                "`### [Hypothesis]` with what you've checked so far and what "
                "remains to verify. This will be picked up in the next session.",
                "3. Mark the current group `[~]` (partially reviewed) in your "
                "plan file.",
                "4. Go to step 5.",
                "",
                "If all groups were reviewed before the timer → go to step 5.",
                "",
                "### 5. Write Review Status",
                "",
                "At the **very top** of your report file, write exactly one of:",
                "",
                f"- `{_COMPLETE_SENTINEL}`",
                f"- `{_INCOMPLETE_SENTINEL}, please invoke the "
                "agent again to continue reviewing.`",
                "",
                "This line MUST be the first line of your report file.",
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
        for i, cp in enumerate(chunk_paths):
            cf = chunk_file_cache[str(cp)]
            if not _rule_matches_files(rule, cf):
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
    """Read committed rules, generate diff, filter, chunk, produce dispatch plan.

    Also reads concern files and generates per-file diffs and concern
    prompt files for the concern pipeline.
    """
    repo = Path(args.repo).resolve()
    config = _resolve_config(str(repo))
    raw_rules_dir = args.rules_dir if args.rules_dir is not None else str(config["rules_dir"])
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
    (work_dir / "scratchpad").mkdir(exist_ok=True)

    # -- determine changed files & chunks --------------------------------

    diff_text = ""
    if scope == "full":
        changed_files = _all_tracked_files(repo)
        chunk_paths: list[Path] = []
    else:
        diff_text, changed_files = _get_diff(scope, repo)
        if not diff_text.strip():
            # Write empty concern-dispatch so downstream run-concerns
            # doesn't crash with FileNotFoundError.
            concern_dispatch_path = work_dir / "concern-dispatch.json"
            concern_dispatch_path.write_text("[]", encoding="utf-8")
            summary = {
                "dispatch_path": None,
                "agents": 0,
                "scope": scope,
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
        "concerns_total": len(concerns),
        "concern_prompts": len(concern_prompts),
    }
    if scope != "full":
        summary["chunks"] = len(chunk_paths)

    print(json.dumps(summary))


# ---------------------------------------------------------------------------
# Continuation helpers
# ---------------------------------------------------------------------------


def _read_dispatch(path: Path) -> list[dict[str, str]]:
    """Read and parse a JSON dispatch file."""
    return json.loads(path.read_text(encoding="utf-8"))


def list_concern_findings(
    dispatch_path: Path,
    work_dir: Path,
    repo: Path,
) -> list[dict[str, object]]:
    """List finding file metadata for each dispatched concern.

    Returns a list of dicts with file existence and size info.
    Does NOT read file content — classification is the orchestrator's job.
    """
    entries = _read_dispatch(dispatch_path)
    results: list[dict[str, object]] = []
    for entry in entries:
        concern = entry["concern"]
        model = entry["model"]
        finding_rel = entry.get("finding_path", "")
        finding_abs = (
            Path(finding_rel) if Path(finding_rel).is_absolute()
            else repo / finding_rel
        ) if finding_rel else (
            work_dir / "findings" / f"concern--{concern}--{model}.md"
        )

        info: dict[str, object] = {
            "concern": concern,
            "model": model,
            "finding_path": _posix(finding_abs, relative_to=repo) if not Path(finding_rel).is_absolute() else str(finding_abs),
            "exists": finding_abs.is_file(),
            "size": finding_abs.stat().st_size if finding_abs.is_file() else 0,
        }
        trace_candidate = work_dir / "traces" / f"concern--{concern}--{model}.jsonl"
        if trace_candidate.is_file():
            info["trace_path"] = _posix(trace_candidate, relative_to=repo)
        results.append(info)
    return results


def build_continuation_dispatch(
    dispatch_path: Path,
    incomplete_pairs: list[tuple[str, str]],
    output_path: Path,
) -> Path:
    """Build a filtered dispatch file containing only incomplete (concern, model) pairs.

    Reads the original dispatch, filters to entries matching incomplete_pairs,
    writes to output_path. Returns output_path.
    """
    entries = _read_dispatch(dispatch_path)
    pair_set = set(incomplete_pairs)
    filtered = [
        e for e in entries
        if (e["concern"], e["model"]) in pair_set
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(filtered, indent=2), encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# Subcommand handlers: continuation
# ---------------------------------------------------------------------------


def _list_findings_cmd(args: argparse.Namespace) -> None:
    """Handler for list-findings subcommand."""
    repo = Path(args.repo).resolve()
    work_dir = Path(args.work_dir).resolve()
    dispatch_path = Path(args.dispatch).resolve()
    result = list_concern_findings(dispatch_path, work_dir, repo)
    print(json.dumps(result, indent=2))


def _build_continuation_cmd(args: argparse.Namespace) -> None:
    """Handler for build-continuation subcommand."""
    dispatch_path = Path(args.dispatch).resolve()
    output_path = Path(args.output).resolve()
    pairs: list[tuple[str, str]] = []
    for p in args.incomplete.split(","):
        p = p.strip()
        if not p:
            continue
        parts = p.split(":", 1)
        if len(parts) != 2:
            print(f"Warning: ignoring malformed pair '{p}' (expected concern:model)", file=sys.stderr)
            continue
        pairs.append((parts[0].strip(), parts[1].strip()))
    build_continuation_dispatch(dispatch_path, pairs, output_path)
    print(json.dumps({"written": str(output_path)}))


# ---------------------------------------------------------------------------
# Subcommand: run-concerns
# ---------------------------------------------------------------------------


def _run_single_concern(
    entry: dict[str, str],
    repo: Path,
    work_dir: Path,
    *,
    hard_timeout: int = CONCERN_HARD_TIMEOUT_SECS,
    inherit_model: str = "",
) -> dict[str, object]:
    """Launch one ``copilot -p`` session for a (concern × model) pair.

    Reads the prompt file from *entry["prompt_path"]*, invokes the copilot
    CLI, captures stdout, and writes findings to
    ``work_dir/findings/concern--{name}--{model}.md``.

    No retries — a single subprocess invocation with *hard_timeout*.
    Continuation of incomplete reviews is handled by the orchestrator.

    Returns a result dict with keys: ``concern``, ``model``, ``status``
    (``exited`` | ``timed_out`` | ``error``), and ``finding_path`` if the
    finding file exists on disk after the process finishes.
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
    trace_path = traces_dir / f"{finding_name}.jsonl"

    if not prompt_abs.is_file():
        return {
            "concern": concern,
            "model": model,
            "status": "error",
            "error": f"Prompt file not found: {prompt_rel}",
        }

    prompt_content = prompt_abs.read_text(encoding="utf-8")

    # Prompt passed as direct CLI argument — copilot CLI does not support
    # stdin piping via ``-p -``.
    cmd = [COPILOT_CMD, "-p", prompt_content, "--allow-all-tools",
           "--output-format", "json"]
    if model == "inherit":
        if inherit_model:
            cmd.extend(["--model", inherit_model])
    else:
        cmd.extend(["--model", _resolve_model(model)])

    pre_mtime = finding_path.stat().st_mtime if finding_path.is_file() else 0

    try:
        result = subprocess.run(
            cmd,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=hard_timeout,
            encoding="utf-8",
            errors="replace",
        )
        # Always save the raw session trace for debugging.
        if result.stdout.strip():
            trace_path.write_text(result.stdout, encoding="utf-8")

            if not finding_path.is_file():
                return {
                    "concern": concern,
                    "model": model,
                    "status": "failed",
                    "error": f"Agent did not write findings file. Check trace: {_posix(trace_path, relative_to=repo)}",
                }

        post_mtime = finding_path.stat().st_mtime if finding_path.is_file() else 0
        out: dict[str, object] = {
            "concern": concern,
            "model": model,
            "status": "exited",
            "returncode": result.returncode,
            "finding_updated": post_mtime > pre_mtime,
        }
        if finding_path.is_file():
            out["finding_path"] = _posix(finding_path, relative_to=repo)
        return out

    except subprocess.TimeoutExpired as exc:
        # Save whatever stdout was captured before the kill for debugging.
        if exc.stdout and exc.stdout.strip():
            trace_path.write_text(exc.stdout, encoding="utf-8")
        out = {
            "concern": concern,
            "model": model,
            "status": "timed_out",
        }
        if finding_path.is_file():
            out["finding_path"] = _posix(finding_path, relative_to=repo)
        return out
    except FileNotFoundError:
        return {
            "concern": concern,
            "model": model,
            "status": "error",
            "error": f"{COPILOT_CMD} is not installed or not on PATH",
        }
    except OSError as exc:
        # On Windows, CreateProcess has a 32 766-char command-line limit.
        # When the prompt exceeds this, subprocess.run raises OSError.
        return {
            "concern": concern,
            "model": model,
            "status": "error",
            "error": f"OS error (prompt may exceed CLI argument limit): {exc}",
        }


def run_concerns(args: argparse.Namespace) -> None:
    """Read concern-dispatch.json and launch ``copilot -p`` per entry.

    Uses :class:`~concurrent.futures.ThreadPoolExecutor` for parallel
    execution.  Each worker invokes :func:`_run_single_concern` with
    a hard timeout.

    Prints a JSON summary on stdout with per-entry results and counts.
    """
    repo = Path(args.repo).resolve()
    work_dir = repo / ".agents" / "focused-review"
    dispatch_path = (
        Path(args.dispatch).resolve()
        if getattr(args, "dispatch", None)
        else work_dir / "concern-dispatch.json"
    )

    if not dispatch_path.is_file():
        print(
            json.dumps({"error": f"Dispatch file not found: {_posix(dispatch_path, relative_to=repo)}"}),
            file=sys.stderr,
        )
        sys.exit(1)

    entries = _read_dispatch(dispatch_path)

    if not entries:
        print(json.dumps({
            "total": 0,
            "success": 0,
            "failed": 0,
            "results": [],
        }))
        return

    max_workers = args.max_workers
    hard_timeout = args.hard_timeout
    inherit_model = getattr(args, "inherit_model", "") or ""

    results: list[dict[str, object]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_entry = {
            executor.submit(
                _run_single_concern,
                entry,
                repo,
                work_dir,
                hard_timeout=hard_timeout,
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
                }
            results.append(result)
            status = result["status"]
            concern = result["concern"]
            model = result["model"]
            if result.get("finding_path"):
                print(
                    f"  ✓ {concern} ({model}): {result['finding_path']}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"  ✗ {concern} ({model}): {result.get('error', status)}",
                    file=sys.stderr,
                )

    success_count = sum(1 for r in results if r.get("finding_path"))
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
        "--hard-timeout",
        type=int,
        default=CONCERN_HARD_TIMEOUT_SECS,
        help=f"Hard timeout per copilot session in seconds (default: {CONCERN_HARD_TIMEOUT_SECS})",
    )
    concerns_parser.add_argument(
        "--dispatch",
        default=None,
        help="Path to an alternate dispatch JSON file (default: .agents/focused-review/concern-dispatch.json)",
    )
    concerns_parser.add_argument(
        "--inherit-model",
        default="",
        help="Model ID to use for entries with model='inherit' (the orchestrator's own model)",
    )
    concerns_parser.set_defaults(func=run_concerns)

    # list-findings subcommand
    list_findings_parser = subparsers.add_parser(
        "list-findings",
        help="List finding file metadata for each dispatched concern",
    )
    list_findings_parser.add_argument(
        "--dispatch",
        required=True,
        help="Path to dispatch JSON file",
    )
    list_findings_parser.add_argument(
        "--work-dir",
        default=".agents/focused-review",
        help="Working directory (default: .agents/focused-review)",
    )
    list_findings_parser.add_argument(
        "--repo",
        default=".",
        help="Path to the repository root (default: current directory)",
    )
    list_findings_parser.set_defaults(func=_list_findings_cmd)

    # build-continuation subcommand
    build_cont_parser = subparsers.add_parser(
        "build-continuation",
        help="Build a filtered dispatch file for incomplete concern/model pairs",
    )
    build_cont_parser.add_argument(
        "--dispatch",
        required=True,
        help="Path to original dispatch JSON file",
    )
    build_cont_parser.add_argument(
        "--incomplete",
        required=True,
        help="Comma-separated concern:model pairs (e.g. bugs:opus,security:gemini)",
    )
    build_cont_parser.add_argument(
        "--output",
        required=True,
        help="Output path for filtered dispatch JSON",
    )
    build_cont_parser.set_defaults(func=_build_continuation_cmd)

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

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
