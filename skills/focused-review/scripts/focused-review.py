#!/usr/bin/env python3
"""Focused review helper — deterministic operations for the focused-review plugin."""

from __future__ import annotations

import argparse
import copy
import fnmatch
import functools
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
from dataclasses import dataclass
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

# display_bucket makes the scope/display routing explicit and illegal states
# unrepresentable: Python DERIVES it from (verdict, introduced_by) during
# finalize and carries it on every finding so hidden / pre-existing items don't
# break the visible counts or the gap-free f# numbering. The reporter does NOT
# emit it. See docs/spec/unified-finding-numbering.md.
#   confirmed     — in-scope Confirmed (the gating "main tally")
#   needs-decision — in-scope Questionable (rendered as "Needs your decision")
#   pre-existing  — Confirmed but not introduced by this change (own section, non-gating)
#   hidden        — recorded but never shown (pre-existing Questionable + every Invalid)
VALID_DISPLAY_BUCKETS = ("confirmed", "needs-decision", "pre-existing", "hidden")
# Buckets that render in a numbered section; their findings receive the visible,
# gap-free f1..fK ids. Findings in the `hidden` bucket are recorded only and get
# trailing f# ids that are never shown.
_VISIBLE_DISPLAY_BUCKETS = ("confirmed", "needs-decision", "pre-existing")
# Canonical display order. finalize orders findings by this rank (then file/line)
# and assigns f# in that order, so the visible buckets occupy f1..fK and every
# `hidden` finding trails after them.
_BUCKET_ORDER = {"confirmed": 0, "needs-decision": 1, "pre-existing": 2, "hidden": 3}
# The introduced_by suffix that marks a finding as not introduced by the change
# under review (orthogonal to verdict; routes Confirmed → pre-existing and
# Questionable → hidden). The assessor emits a four-value vocabulary —
# diff | pre-existing | reclassified-pre-existing | reclassified-diff (see
# agents/review-assessor.agent.md) — and the two pre-existing spellings share
# this suffix, so `_is_pre_existing` suffix-matches it instead of comparing for
# exact equality (an exact match would silently route reclassified-pre-existing
# into the gating Confirmed tally, breaking the "pre-existing is non-gating"
# invariant).
PRE_EXISTING_MARKER = "pre-existing"

# Python-assigned id formats so the unified canvas action bar can disambiguate a
# flat id list by prefix: findings are f# (the numeric part is the visible display
# number), rule-quality notes are rq#. Lowercase in data; rendered uppercase
# (F2 / RQ1). The reporter does NOT emit these — finalize assigns them.
_FINDING_ID_RE = re.compile(r"^f[0-9]+$")
_RULE_QUALITY_NOTE_ID_RE = re.compile(r"^rq[0-9]+$")


def _is_pre_existing(introduced_by: object) -> bool:
    """Whether ``introduced_by`` marks a finding as *not* introduced by the change.

    Normalizes the assessor's four-value vocabulary (``diff`` | ``pre-existing``
    | ``reclassified-pre-existing`` | ``reclassified-diff``; see
    ``agents/review-assessor.agent.md``) onto the binary scope routing Python
    needs. Both spellings whose canonical scope is *pre-existing* end in
    :data:`PRE_EXISTING_MARKER`, so a suffix test captures ``pre-existing`` and
    ``reclassified-pre-existing`` without enumerating each one; everything else
    (``diff``, ``reclassified-diff``, ``""``, absent, or a non-string) is
    in-scope. An exact-equality test would route a ``reclassified-pre-existing``
    Confirmed into the gating Confirmed tally, breaking the "pre-existing is
    non-gating" invariant.
    """
    return isinstance(introduced_by, str) and introduced_by.endswith(PRE_EXISTING_MARKER)


def _derive_display_bucket(verdict: object, introduced_by: object) -> str | None:
    """Derive the canonical display_bucket from ``(verdict, introduced_by)``.

    Returns ``None`` when ``verdict`` is not a recognized enum value (the caller
    has already reported that error and must not derive a bucket from it). Scope
    is normalized via :func:`_is_pre_existing`, so both ``pre-existing`` and the
    assessor's ``reclassified-pre-existing`` route Confirmed → ``pre-existing``
    and Questionable → ``hidden``; ``diff`` / ``reclassified-diff`` / ``""`` /
    absent / a non-string all stay in-scope.
    """
    if verdict not in VALID_VERDICTS:
        return None
    pre_existing = _is_pre_existing(introduced_by)
    if verdict == "Invalid":
        return "hidden"
    if verdict == "Confirmed":
        return "pre-existing" if pre_existing else "confirmed"
    # Questionable
    return "hidden" if pre_existing else "needs-decision"

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

# Concern files use stable family shorthands (``opus``, ``gpt``, ``gemini`` …)
# instead of concrete CLI slugs, which Copilot rotates over time.  Each family
# is resolved to the *best currently-available* slug by querying the live model
# list (see :func:`_available_models`).  ``fallback`` is used only when the live
# list is unavailable (offline / enumeration failure) so reviews still run.


@dataclass(frozen=True)
class FamilyRule:
    """How to select the best concrete slug for a model-family shorthand.

    A candidate slug from the available set matches when it starts with
    *prefix*, contains every token in *require*, and contains no token in
    *exclude*.  Among matches, the *best* is chosen by *prefer* token first,
    then the highest version, then the plainest variant.  *fallback* is the
    offline default when the live model list cannot be enumerated.
    """

    prefix: str
    fallback: str
    require: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    prefer: tuple[str, ...] = ()


# Single source of truth for family shorthands.  Keys are the shorthands users
# write in concern frontmatter; values describe how to pick a live slug.
FAMILY_RULES: dict[str, FamilyRule] = {
    "opus": FamilyRule(prefix="claude-opus-", fallback="claude-opus-4.6-1m"),
    "sonnet": FamilyRule(prefix="claude-sonnet-", fallback="claude-sonnet-4.6"),
    "haiku": FamilyRule(prefix="claude-haiku-", fallback="claude-haiku-4.5"),
    "gpt": FamilyRule(prefix="gpt-", fallback="gpt-5.5", exclude=("codex", "mini")),
    "codex": FamilyRule(prefix="gpt-", fallback="gpt-5.3-codex", require=("codex",)),
    "gemini": FamilyRule(prefix="gemini-", fallback="gemini-3-pro-preview", prefer=("pro",)),
}

_MODEL_ITEM_RE = re.compile(r'^\s*-\s*"([^"]+)"\s*$')
_CONFIG_SETTING_RE = re.compile(r"^\s*`([A-Za-z0-9_.]+)`\s*:")
_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")


def _parse_model_list(help_text: str) -> tuple[str, ...]:
    """Extract available model slugs from ``copilot help config`` output.

    The ``model`` setting documents its accepted values as a quoted bullet
    list.  We scope extraction to that block (between the ```` `model`: ````
    line and the next setting) and return the slugs in order, de-duplicated.
    Returns an empty tuple if the block is absent.
    """
    in_model_block = False
    slugs: list[str] = []
    for line in help_text.splitlines():
        setting = _CONFIG_SETTING_RE.match(line)
        if setting:
            in_model_block = setting.group(1) == "model"
            continue
        if in_model_block:
            item = _MODEL_ITEM_RE.match(line)
            if item:
                slugs.append(item.group(1))
    return tuple(dict.fromkeys(slugs))


def _query_available_models() -> tuple[str, ...]:
    """Run ``copilot help config`` and parse the available model slugs.

    Fail-soft: returns an empty tuple on any error (CLI missing, non-zero
    exit, timeout) so resolution can fall back to static defaults.
    """
    try:
        result = subprocess.run(
            [COPILOT_CMD, "help", "config"],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if result.returncode != 0:
        return ()
    return _parse_model_list(result.stdout or "")


@functools.lru_cache(maxsize=1)
def _available_models() -> tuple[str, ...]:
    """Currently-available model slugs, cached for the lifetime of the run."""
    return _query_available_models()


def _rank(slug: str, rule: FamilyRule) -> tuple[int, tuple[int, ...], int]:
    """Sort key (higher is better) for picking the best slug in a family.

    Orders by: preferred-token match, then numeric version descending, then
    the plainest variant (shortest suffix after the version).
    """
    remainder = slug[len(rule.prefix):]
    version_match = _VERSION_RE.match(remainder)
    if version_match:
        version = tuple(int(part) for part in version_match.group(0).split("."))
        suffix = remainder[version_match.end():]
    else:
        version = ()
        suffix = remainder

    prefer_score = 0
    for index, token in enumerate(rule.prefer):
        if token in slug:
            prefer_score = len(rule.prefer) - index
            break

    return (prefer_score, version, -len(suffix))


def _best_match(family: str, available: tuple[str, ...]) -> str | None:
    """Return the best available slug for *family*, or ``None`` if none match."""
    rule = FAMILY_RULES[family]
    candidates = [
        slug
        for slug in available
        if slug.startswith(rule.prefix)
        and all(token in slug for token in rule.require)
        and not any(token in slug for token in rule.exclude)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda slug: _rank(slug, rule))


def _resolve_model(shorthand: str) -> str | None:
    """Resolve a model shorthand to a concrete, available CLI slug.

    Resolution is case-insensitive.  Behaviour by input:

    - **Known family** (``opus``, ``gpt`` …): pick the best slug from the live
      model list.  If the family has no available match, return ``None`` so the
      caller can skip that concern-model gracefully.  If the live list cannot be
      enumerated, fall back to the family's static default (offline-safe).
    - **Anything else** (a full slug, internal model, or future identifier):
      passed through unchanged so users can target exact slugs directly.
    """
    key = shorthand.lower()
    rule = FAMILY_RULES.get(key)
    if rule is None:
        return shorthand

    available = _available_models()
    if available:
        return _best_match(key, available)
    return rule.fallback


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
        resolved = _resolve_model(model)
        if resolved is None:
            return {
                "concern": concern,
                "model": model,
                "status": "skipped",
                "error": (
                    f"No available model matches family '{model}' in this "
                    "environment; skipping this concern-model."
                ),
                "attempt": 0,
            }
        cmd.extend(["--model", resolved])

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
            "skipped": 0,
            "failed": 0,
            "results": [],
        }))
        return

    max_workers = args.max_workers
    timeout = args.timeout
    retries = args.retries
    inherit_model = getattr(args, "inherit_model", "") or ""

    # Warm the available-model cache once before fanning out, but only if a
    # concern actually uses a family shorthand — otherwise enumeration is never
    # needed and we avoid an unnecessary ``copilot help config`` subprocess.
    if any(str(entry.get("model", "")).lower() in FAMILY_RULES for entry in entries):
        _available_models()

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
            elif status == "skipped":
                print(
                    f"  ⊘ {concern} ({model}): {result.get('error', status)}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"  ✗ {concern} ({model}): {result.get('error', status)}",
                    file=sys.stderr,
                )

    success_count = sum(1 for r in results if r["status"] == "success")
    skipped_count = sum(1 for r in results if r["status"] == "skipped")
    failed_count = len(results) - success_count - skipped_count

    summary = {
        "total": len(results),
        "success": success_count,
        "skipped": skipped_count,
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
# `validate_records` is a pure function that performs SEMANTIC validation of the
# reporter's output: it checks only the fields the reporter emits (titles,
# verdicts, provenance, assessment ids, rule-quality observations) and is lenient
# about the display layer (record_id / display_bucket / note id / run counts),
# which Python assigns later in `finalize_records`. It returns a list of
# structured, per-record error dicts (empty list == valid). Each error carries
# enough context — a JSON-ish `path`, the offending `field`, and the finding's
# stable identifiers (`assessment_id`, plus `record_id` once assigned) — for the
# orchestrator to relay an actionable message back to the reporter on a retry.
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
        "message": message,
    }


def _finding_identity(rec: dict) -> tuple[str | None, str | None]:
    """Best-effort extraction of a finding's stable ids for error attribution.

    Returns ``(record_id, assessment_id)``. During semantic validation the
    reporter has not yet been assigned a ``record_id``; ``assessment_id`` (the
    A-XX id minted by the assessor) is the stable handle the reporter knows.
    """
    rid = rec.get("record_id")
    rid = rid if _is_nonempty_str(rid) else None
    aid = rec.get("assessment_id")
    aid = aid if _is_nonempty_str(aid) else None
    return rid, aid


def _validate_run(run: dict, errors: list[dict]) -> None:
    """Semantically validate the ``run`` metadata the reporter emits.

    Only the reporter-supplied fields are required here: ``run_id``, ``date``,
    ``scope`` (an enum), and the dispatch inputs ``rule_count`` / ``concern_count``
    (which are not derivable from findings). The tally fields
    (``consolidated_count`` / ``confirmed`` / ``questionable`` / ``invalid``) are
    computed by ``finalize_records`` and checked by ``validate_finalized_records``,
    so they are neither required nor rejected here.
    """

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

    for field in ("rule_count", "concern_count"):
        value = run.get(field, _MISSING)
        if not _is_int(value) or value < 0:
            add(field, f"{field} is required and must be an integer >= 0")


def _validate_run_counts(run: dict, findings: list, errors: list[dict]) -> None:
    """Cross-check ``run`` tallies against the bucketed ``findings``.

    Only runs when every finding is an object (otherwise a structural error was
    already reported). Catches truncation and miscounts — both real reporter
    failure modes the spec calls out.

    Count semantics (verdict-model redesign):

    - ``confirmed`` / ``questionable`` are the **visible bucket** tallies, so
      pre-existing Confirmed (its own non-gating section) and hidden pre-existing
      Questionable are excluded from the headline actionable counts.
    - ``invalid`` stays the **verdict** tally — the false-positive count — even
      though every Invalid finding is hidden from the rendered report.
    """
    if not all(isinstance(rec, dict) for rec in findings):
        return

    def add(field: str, message: str) -> None:
        errors.append(_records_error("run", None, f"run.{field}", field, message))

    verdicts_ok = all(rec.get("verdict") in VALID_VERDICTS for rec in findings)
    introduced_ok = all(
        rec.get("introduced_by", _MISSING) is _MISSING
        or isinstance(rec.get("introduced_by"), str)
        for rec in findings
    )

    # Visible-bucket tallies (confirmed / needs-decision). Gated on valid verdict
    # AND well-formed introduced_by so a bad field doesn't yield a *misleading*
    # count mismatch — the real error is reported per finding.
    if verdicts_ok and introduced_ok:
        bucket_tally = {bucket: 0 for bucket in VALID_DISPLAY_BUCKETS}
        for rec in findings:
            bucket = _derive_display_bucket(rec.get("verdict"), rec.get("introduced_by"))
            if bucket in bucket_tally:
                bucket_tally[bucket] += 1
        for field, bucket in (("confirmed", "confirmed"), ("questionable", "needs-decision")):
            value = run.get(field)
            if _is_int(value) and value >= 0 and value != bucket_tally[bucket]:
                add(
                    field,
                    f"run.{field} is {value} but {bucket_tally[bucket]} finding(s) "
                    f"are in the '{bucket}' display bucket",
                )

    # invalid — verdict tally (false-positive count). Gated only on valid verdict.
    if verdicts_ok:
        invalid_count = sum(1 for rec in findings if rec.get("verdict") == "Invalid")
        value = run.get("invalid")
        if _is_int(value) and value >= 0 and value != invalid_count:
            add(
                "invalid",
                f"run.invalid is {value} but {invalid_count} finding(s) have "
                f"verdict 'Invalid'",
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
    seen_assessment_ids: set,
) -> None:
    """Semantically validate a single finding object (reporter output).

    Only the fields the reporter emits are checked here. The display layer
    (``record_id``, ``display_bucket``) is assigned later by ``finalize_records``
    and verified by ``validate_finalized_records`` — it is neither required nor
    rejected here, so re-validating an already-enriched file still passes.
    """
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

    rid, aid = _finding_identity(rec)

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
    # (the spec keeps it free-form: "type-checked only, no enum"). finalize uses
    # it together with verdict to derive display_bucket.
    introduced_by = rec.get("introduced_by", _MISSING)
    if introduced_by is not _MISSING and not isinstance(introduced_by, str):
        add("introduced_by", "introduced_by must be a string when present")

    # has_detail — required boolean; gates the optional detail sidecar.
    has_detail = rec.get("has_detail", _MISSING)
    if not isinstance(has_detail, bool):
        add("has_detail", "has_detail is required and must be a boolean")

    # assessment_id — nullable; the detail sidecar is located by it, so a non-null
    # assessment_id must be a non-empty string, unique across findings, and present
    # when has_detail.
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

    _validate_provenance(rec.get("provenance", _MISSING), base_path, add)


def _validate_rebuttal_override(
    item: object, index: int, errors: list[dict], known_assessment_ids: set | None
) -> None:
    """Validate one rebuttal-override entry.

    The reporter references the finding by its ``assessment_id`` (the A-XX id the
    assessor minted) — NOT the Python-assigned ``record_id``, which the reporter
    never sees. ``known_assessment_ids`` is the set of finding assessment ids
    (when available) for a referential-integrity check.
    """
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

    raw_aid = item.get("assessment_id")
    aid_ident = raw_aid if _is_nonempty_str(raw_aid) else None

    def add(field: str | None, message: str) -> None:
        path = base_path if field is None else f"{base_path}.{field}"
        errors.append(
            _records_error(
                "rebuttal_override", index, path, field, message, assessment_id=aid_ident
            )
        )

    if not _is_nonempty_str(raw_aid):
        add("assessment_id", "assessment_id is required and must be a non-empty string")
    elif known_assessment_ids is not None and raw_aid not in known_assessment_ids:
        add("assessment_id", f"assessment_id {raw_aid!r} does not match any finding")

    for field in ("original_severity", "severity"):
        value = item.get(field, _MISSING)
        if value is _MISSING:
            add(field, f"{field} is required and must be one of " + ", ".join(VALID_SEVERITIES))
        elif value not in VALID_SEVERITIES:
            add(field, f"{field} must be one of {', '.join(VALID_SEVERITIES)} (got {value!r})")

    if not _is_nonempty_str(item.get("reasoning", _MISSING)):
        add("reasoning", "reasoning is required and must be a non-empty string")


def _validate_rule_file(value: object, rules_dir: str, add) -> None:
    """Validate a rule-quality note's ``rule_file`` path.

    The trust boundary stays in Python: the agent later edits this file to apply
    a rule fix, so the path must be a *safe relative path under the configured
    rules directory*. Rejects absolute paths, ``..`` traversal, non-``.md``
    targets, and anything outside ``rules_dir``. ``add`` is the note-scoped error
    accumulator.
    """
    if not _is_nonempty_str(value):
        add("rule_file", "rule_file is required and must be a non-empty string")
        return
    raw = value.replace("\\", "/")
    if raw.startswith("/") or (len(raw) >= 2 and raw[0].isalpha() and raw[1] == ":"):
        add(
            "rule_file",
            "rule_file must be a relative path under the rules directory, not absolute",
        )
        return
    if ".." in Path(raw).parts:
        add("rule_file", "rule_file must not contain '..' path segments")
        return
    if not raw.endswith(".md"):
        add("rule_file", "rule_file must point to a .md rule file")
        return
    if rules_dir:
        norm_dir = Path(os.path.normpath(rules_dir))
        if not Path(os.path.normpath(raw)).is_relative_to(norm_dir):
            add(
                "rule_file",
                f"rule_file must live under the rules directory "
                f"'{norm_dir.as_posix()}' (got {value!r})",
            )


def _rule_file_source_mismatch(rule_source: object, rule_file: object) -> str | None:
    """Return an error message when *rule_file* is not the file *rule_source* names.

    ``rule_source`` is the canonical ``rule--<name>`` provenance label that ties a
    rule-quality note to the findings its fix invalidates (matched against their
    provenance by :func:`_rule_dependency_map`); ``rule_file`` is the *separate*
    path the agent later edits to apply that fix. The two are authored independently
    and can drift (Finding C-12): a note naming ``rule--no-comments`` while pointing
    ``rule_file`` at ``simplicity.md`` passes every path-safety check, yet the fix
    then edits ``simplicity.md`` while the no-comments findings are marked
    invalidated — silent, cross-wired corruption. This cross-check ties the two
    together: the ``rule_file`` stem must equal the name part of ``rule_source``.

    Returns ``None`` (nothing to report) when either field is empty/non-string or
    when ``rule_source`` is not in canonical ``rule--`` form — there is then no name
    to derive, and an un-prefixed source matches no finding provenance anyway, so its
    fix invalidates nothing (a no-op, not a corruption).
    """
    if not _is_nonempty_str(rule_source) or not _is_nonempty_str(rule_file):
        return None
    source = rule_source.strip()
    if not source.startswith("rule--"):
        return None
    expected_stem = source[len("rule--"):]
    actual_stem = PurePosixPath(rule_file.replace("\\", "/")).stem
    if actual_stem == expected_stem:
        return None
    return (
        f"rule_file stem {actual_stem!r} does not match rule_source {source!r} "
        f"(rule_source names rule {expected_stem!r}, so rule_file must be that "
        f"rule's {expected_stem}.md file under the rules directory); the file the "
        f"agent edits must be the rule whose findings the fix invalidates"
    )


def _collect_rule_file_errors(
    rule_file: object, rule_source: object, rules_dir: str
) -> list[str]:
    """Trust-boundary errors for a rule-quality note's ``rule_file`` path.

    Shared by the schema validator (:func:`_validate_rule_quality_note`, the
    render-review / validate-records path) and the action validator
    (:func:`validate_action`, the canvas round-trip) so both apply the *same* two
    checks at their respective trust boundaries: first the path-safety check
    (absolute / ``..`` traversal / non-``.md`` / outside ``rules_dir``), then — only
    when the path is safe — the ``rule_source`` consistency cross-check (Finding
    C-12 via :func:`_rule_file_source_mismatch`). The cross-check is skipped on an
    unsafe path because the path error is the actionable one and the stem of a
    traversal/absolute path is meaningless. Returns the messages in order; an empty
    list means the ``rule_file`` is trustworthy to edit.
    """
    messages: list[str] = []
    _validate_rule_file(rule_file, rules_dir, lambda _field, message: messages.append(message))
    if messages:
        return messages
    mismatch = _rule_file_source_mismatch(rule_source, rule_file)
    if mismatch is not None:
        messages.append(mismatch)
    return messages


def _validate_rule_quality_note(
    item: object,
    index: int,
    errors: list[dict],
    seen_rule_sources: set,
    rules_dir: str,
) -> None:
    """Semantically validate one rule-quality-note entry (reporter output).

    The reporter emits the *semantic* fields only: ``rule_sources`` (a non-empty
    list of canonical ``rule--<name>`` provenance labels tying the note to the
    findings its fix invalidates), ``rule_file`` (the safe path the agent later
    edits to apply the fix), and the ``observation`` / ``suggestion`` prose. The
    display layer — the ``id`` (``rq#``) and the human-readable ``rule`` label —
    is assigned by ``finalize_records``, so neither is required (nor rejected)
    here.

    Each source label is uniqueness-checked across the whole notes array (via
    *seen_rule_sources*) because :func:`_rule_dependency_map` keys its source->note
    map on the stripped label; two notes naming the same source would collapse to
    the first, so the later note's fix would silently invalidate no findings.
    Rejecting the duplicate here keeps that map one-to-one.
    """
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

    for field in ("observation", "suggestion"):
        if not _is_nonempty_str(item.get(field, _MISSING)):
            add(field, f"{field} is required and must be a non-empty string")

    # rule_sources — required non-empty list of canonical rule--<name> labels.
    # Each entry must be a non-empty string and unique across ALL notes (so the
    # source->note map _rule_dependency_map builds stays one-to-one). A label that
    # repeats within this same note is caught by the same accumulated set.
    valid_sources: list[str] = []
    rule_sources = item.get("rule_sources", _MISSING)
    if rule_sources is _MISSING or not isinstance(rule_sources, list):
        add("rule_sources", "rule_sources is required and must be a non-empty array of strings")
    elif len(rule_sources) == 0:
        add("rule_sources", "rule_sources must contain at least one rule source label")
    else:
        for j, source in enumerate(rule_sources):
            if not _is_nonempty_str(source):
                add("rule_sources", f"rule_sources[{j}] must be a non-empty string")
                continue
            normalized = source.strip()
            if normalized in seen_rule_sources:
                add(
                    "rule_sources",
                    f"duplicate rule_source {source!r} "
                    "(each rule may be named by at most one rule-quality note)",
                )
            else:
                seen_rule_sources.add(normalized)
                valid_sources.append(normalized)

    # rule_file — path safety, then per-source consistency (Finding C-12). The
    # path-safety check runs once; the rule_source cross-check runs for every
    # source label so a note naming several rules can't point rule_file at a file
    # that matches none of them.
    rule_file = item.get("rule_file", _MISSING)
    path_messages: list[str] = []
    _validate_rule_file(rule_file, rules_dir, lambda _f, message: path_messages.append(message))
    if path_messages:
        for message in path_messages:
            add("rule_file", message)
    else:
        for source in valid_sources:
            mismatch = _rule_file_source_mismatch(source, rule_file)
            if mismatch is not None:
                add("rule_file", mismatch)


def validate_records(data: object, *, rules_dir: str | None = None) -> list[dict]:
    """SEMANTICALLY validate a parsed ``records.json`` envelope (reporter output).

    Checks only the fields the reporter emits — titles, files, severities,
    verdicts, provenance, assessment ids, rule-quality observations. The display
    layer (finding ``record_id`` / ``display_bucket``, note ``id`` / ``rule``
    label, and the ``run`` tally counts) is assigned later by
    :func:`finalize_records` and verified by :func:`validate_finalized_records`,
    so it is neither required nor rejected here — re-validating an already-enriched
    file therefore still passes.

    Returns a list of structured, per-record error dicts; an empty list means
    the envelope is valid. Never raises on malformed *data* — every problem is
    reported as an error entry so the caller can relay it back to the reporter.

    ``rules_dir`` is the configured review-rules directory; a rule-quality note's
    ``rule_file`` is validated to live under it (the trust boundary for the
    rule-fix flow). When ``None`` it falls back to :data:`DEFAULT_RULES_DIR` so a
    caller that omits it still gets a secure-by-default ``review/`` prefix check;
    the CLI commands resolve and pass the repository's configured value.
    """
    effective_rules_dir = DEFAULT_RULES_DIR if rules_dir is None else rules_dir
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
    # SEMANTIC validation only: the display layer (record_id / display_bucket) is
    # assigned later by finalize_records, so it is neither required nor rejected
    # here. We collect assessment_ids for the rebuttal-override referential check.
    findings = data.get("findings", _MISSING)
    assessment_ids: set = set()
    findings_is_list = isinstance(findings, list)
    if findings is _MISSING:
        add_envelope("findings", "findings is required and must be an array")
    elif not findings_is_list:
        add_envelope("findings", f"findings must be an array, got {_json_type_name(findings)}")
    else:
        for i, rec in enumerate(findings):
            _validate_finding(rec, i, errors, assessment_ids)

    # rebuttal_overrides ----------------------------------------------------
    # The reporter references the finding by assessment_id (the A-XX id it knows),
    # not the Python-assigned record_id.
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
        known = assessment_ids if findings_is_list else None
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
        seen_rule_sources: set = set()
        for i, item in enumerate(notes):
            _validate_rule_quality_note(
                item, i, errors, seen_rule_sources, effective_rules_dir
            )

    return errors


# ---------------------------------------------------------------------------
# records.json finalization (the display layer)
#
# `validate_records` checks the reporter's *semantic* output; `finalize_records`
# then assigns the *display layer* deterministically in Python — the single source
# of truth for finding/note ids, ordering, buckets, and run tallies:
#
#   * display_bucket  — derived from (verdict, introduced_by) per finding
#   * findings order  — sorted by (bucket rank, file, line) so visible buckets
#                       precede hidden, file/line ascending within a bucket
#   * record_id       — f1..fN over that sorted order; the visible findings get
#                       the gap-free f1..fK, the hidden ones trail as f(K+1)..fN
#   * note id / rule  — rq1..rqM in array order, with the human-readable `rule`
#                       label derived from the rule_file stem
#   * run tallies     — consolidated_count / confirmed / questionable / invalid
#                       counted from the finalized findings
#
# It is a pure function: it deep-copies its input, never mutates the argument, and
# is idempotent — re-finalizing already-enriched data reproduces the same result
# (so SKILL Step 6d's re-render on an enriched file is safe). It is also robust to
# malformed input (it runs only after `validate_records` passes, but it must never
# raise even on partially-shaped data).
# ---------------------------------------------------------------------------


def _finding_sort_key(finding: object, bucket: str) -> tuple:
    """Order key for a finding: (bucket rank, file, line-present, line).

    Visible buckets (rank 0-2) precede ``hidden`` (rank 3). Within a bucket,
    findings sort by file then line; a null line sorts *before* a numbered line in
    the same file. Ties preserve input order because Python's sort is stable.
    """
    rank = _BUCKET_ORDER.get(bucket, _BUCKET_ORDER["hidden"])
    if isinstance(finding, dict):
        file_str = finding.get("file")
        file_str = file_str if isinstance(file_str, str) else ""
        line = finding.get("line")
        has_line = _is_int(line)
        line_val = line if has_line else 0
    else:
        file_str = ""
        has_line = False
        line_val = 0
    # has_line False (null line) sorts first within the same file.
    return (rank, file_str, has_line, line_val)


def finalize_records(data: object) -> object:
    """Return a deep-copied, display-enriched copy of a validated *records.json*.

    Assigns ``display_bucket``, reorders ``findings``, stamps the gap-free ``f#``
    ``record_id`` sequence, the ``rq#`` note ids and derived ``rule`` labels, and
    the ``run`` tally counts. Never mutates *data*; idempotent; never raises on
    malformed input (guards every shape and degrades gracefully).
    """
    result = copy.deepcopy(data)
    if not isinstance(result, dict):
        return result

    # --- findings: derive bucket, reorder, assign f# ----------------------
    findings = result.get("findings")
    if isinstance(findings, list):
        # Derive the display_bucket for every finding first; fall back to
        # "hidden" only if the verdict is unrecognized (validation would have
        # already rejected that — this just keeps finalize total/non-crashing).
        for finding in findings:
            if isinstance(finding, dict):
                bucket = _derive_display_bucket(
                    finding.get("verdict"), finding.get("introduced_by")
                )
                finding["display_bucket"] = bucket if bucket is not None else "hidden"

        def bucket_of(finding: object) -> str:
            if isinstance(finding, dict):
                b = finding.get("display_bucket")
                if b in VALID_DISPLAY_BUCKETS:
                    return b
            return "hidden"

        findings.sort(key=lambda f: _finding_sort_key(f, bucket_of(f)))

        # f1..fN over the sorted order: visible buckets occupy f1..fK gap-free,
        # hidden findings trail as f(K+1)..fN (never shown).
        for position, finding in enumerate(findings, start=1):
            if isinstance(finding, dict):
                finding["record_id"] = f"f{position}"

        result["findings"] = findings

    # --- rule_quality_notes: assign rq# + derive display label ------------
    notes = result.get("rule_quality_notes")
    if isinstance(notes, list):
        for position, note in enumerate(notes, start=1):
            if isinstance(note, dict):
                note["id"] = f"rq{position}"
                rule_file = note.get("rule_file")
                if isinstance(rule_file, str) and rule_file.strip():
                    note["rule"] = PurePosixPath(rule_file.replace("\\", "/")).stem
                else:
                    note["rule"] = ""

    # --- run: compute the tally counts from the finalized findings --------
    run = result.get("run")
    if isinstance(run, dict):
        finding_dicts = (
            [f for f in findings if isinstance(f, dict)]
            if isinstance(findings, list)
            else []
        )
        run["consolidated_count"] = len(finding_dicts)
        run["confirmed"] = sum(
            1 for f in finding_dicts if f.get("display_bucket") == "confirmed"
        )
        run["questionable"] = sum(
            1 for f in finding_dicts if f.get("display_bucket") == "needs-decision"
        )
        run["invalid"] = sum(1 for f in finding_dicts if f.get("verdict") == "Invalid")

    return result


def validate_finalized_records(data: object, *, rules_dir: str | None = None) -> list[dict]:
    """Invariant self-check on display-enriched *records.json* (post-finalize).

    Confirms the display layer :func:`finalize_records` is responsible for is
    internally consistent: the ``f#`` ids are a gap-free 1..N sequence *in array
    order* (so the array IS display order, visible buckets first), each
    ``display_bucket`` agrees with its derivation and the bucket ranks never
    decrease across the array, the ``rq#`` note ids are 1..M in order, and the
    ``run`` tallies match the finalized findings. This runs the full semantic
    :func:`validate_records` first (the enriched file must still be semantically
    valid), then layers the assigned-field invariants on top.

    Returns a list of structured error dicts (empty == valid). It is defensive:
    on correctly finalized data it always returns ``[]``; it exists to catch a
    finalize regression or hand-tampered enriched file, never to gate the reporter.
    """
    errors = validate_records(data, rules_dir=rules_dir)
    if not isinstance(data, dict):
        return errors

    def add_envelope(field: str | None, message: str) -> None:
        path = "$" if field is None else field
        errors.append(_records_error("envelope", None, path, field, message))

    # --- findings: f# gap-free in display order + bucket ordering ----------
    findings = data.get("findings")
    if isinstance(findings, list):
        previous_rank = -1
        for i, finding in enumerate(findings):
            expected_id = f"f{i + 1}"
            if not isinstance(finding, dict):
                continue
            rid, aid = _finding_identity(finding)

            def add(field: str, message: str, *, _rid=rid, _aid=aid) -> None:
                errors.append(
                    _records_error(
                        "finding",
                        i,
                        f"findings[{i}].{field}",
                        field,
                        message,
                        record_id=_rid,
                        assessment_id=_aid,
                    )
                )

            record_id = finding.get("record_id")
            if not _is_nonempty_str(record_id) or not _FINDING_ID_RE.match(record_id):
                add(
                    "record_id",
                    f"record_id must match ^f[0-9]+$ (e.g. f1, f2); got {record_id!r}",
                )
            elif record_id != expected_id:
                add(
                    "record_id",
                    f"record_id {record_id!r} is out of sequence; findings must carry "
                    f"a gap-free f1..fN run in display order (expected {expected_id!r} "
                    f"at position {i + 1})",
                )

            bucket = finding.get("display_bucket")
            derived = _derive_display_bucket(
                finding.get("verdict"), finding.get("introduced_by")
            )
            if bucket not in VALID_DISPLAY_BUCKETS:
                add(
                    "display_bucket",
                    "display_bucket is required and must be one of "
                    + ", ".join(VALID_DISPLAY_BUCKETS),
                )
            else:
                if derived is not None and bucket != derived:
                    add(
                        "display_bucket",
                        f"display_bucket {bucket!r} is inconsistent with "
                        f"(verdict={finding.get('verdict')!r}, "
                        f"introduced_by={finding.get('introduced_by')!r}); "
                        f"expected {derived!r}",
                    )
                rank = _BUCKET_ORDER.get(bucket, _BUCKET_ORDER["hidden"])
                if rank < previous_rank:
                    add(
                        "display_bucket",
                        f"findings must be ordered by display bucket "
                        f"({' < '.join(VALID_DISPLAY_BUCKETS)}); a {bucket!r} finding "
                        f"appears after a later-bucket finding",
                    )
                previous_rank = max(previous_rank, rank)

    # --- rule_quality_notes: rq# 1..M in order ----------------------------
    notes = data.get("rule_quality_notes")
    if isinstance(notes, list):
        for i, note in enumerate(notes):
            if not isinstance(note, dict):
                continue
            expected_id = f"rq{i + 1}"
            note_id = note.get("id")
            if not _is_nonempty_str(note_id) or not _RULE_QUALITY_NOTE_ID_RE.match(note_id):
                errors.append(
                    _records_error(
                        "rule_quality_note",
                        i,
                        f"rule_quality_notes[{i}].id",
                        "id",
                        f"id must match ^rq[0-9]+$ (e.g. rq1, rq2); got {note_id!r}",
                    )
                )
            elif note_id != expected_id:
                errors.append(
                    _records_error(
                        "rule_quality_note",
                        i,
                        f"rule_quality_notes[{i}].id",
                        "id",
                        f"id {note_id!r} is out of sequence; rule-quality notes must "
                        f"carry a gap-free rq1..rqM run (expected {expected_id!r} at "
                        f"position {i + 1})",
                    )
                )

    # --- run: tallies must match the finalized findings -------------------
    run = data.get("run")
    if isinstance(run, dict) and isinstance(findings, list):
        _validate_run_counts(run, findings, errors)

    return errors


def load_and_validate_records(
    path: str | os.PathLike, *, rules_dir: str | None = None
) -> tuple[object, list[dict]]:
    """Load *path* and validate it.

    Returns ``(data, errors)``. On a read/parse failure, ``data`` is ``None`` and
    ``errors`` holds a single envelope-scoped error. ``rules_dir`` is forwarded to
    :func:`validate_records` for the ``rule_file`` under-rules-dir check.
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

    return data, validate_records(data, rules_dir=rules_dir)


def _emit_validation_errors(records_path: str, errors: list[dict]) -> None:
    """Print the structured records-validation failure payload to stderr.

    Shared by ``validate-records`` and ``render-review`` so both commands emit
    byte-identical failure JSON. Emit-only (mirroring ``_emit_action_error``):
    callers keep their own ``sys.exit(1)`` so control flow stays visible at the
    call site.
    """
    payload = {
        "valid": False,
        "schema_version": RECORDS_SCHEMA_VERSION,
        "records_path": str(records_path),
        "error_count": len(errors),
        "errors": errors,
    }
    print(json.dumps(payload, indent=2), file=sys.stderr)


def validate_records_command(args: argparse.Namespace) -> None:
    """CLI: validate ``records.json`` and emit a structured result.

    On success, prints a small summary JSON to stdout (exit 0). On failure,
    prints the structured per-record errors as JSON to stderr and exits 1, so the
    orchestrator can relay them to the reporter for a retry.

    The review-rules directory (for the ``rule_file`` under-rules-dir check) comes
    from ``--rules-dir`` when given, else from the repo's resolved config
    (``--repo``). ``getattr`` keeps programmatic callers that build a bare
    Namespace working.
    """
    path: str = args.records
    repo = getattr(args, "repo", None) or "."
    rules_dir = getattr(args, "rules_dir", None) or _resolve_rules_dir(repo)
    data, errors = load_and_validate_records(path, rules_dir=rules_dir)

    if errors:
        _emit_validation_errors(path, errors)
        sys.exit(1)

    # The reporter emits semantic fields only; the display-layer tallies
    # (confirmed / questionable / invalid) are Python-assigned, so finalize in
    # memory to report accurate counts. This command does NOT persist the
    # enriched records.json — render-review owns that.
    finalized = finalize_records(data)
    run = finalized.get("run", {}) if isinstance(finalized, dict) else {}
    findings = finalized.get("findings", []) if isinstance(finalized, dict) else []
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
# post-mortem mode, which matches each finding's "(record_id)" heading anchor and
# the rule:/concern: provenance labels). The canvas fills the version-controlled
# template, html.escape()-ing every structured text field.
# See docs/spec/canvas-review-report.md.
# ---------------------------------------------------------------------------

# Relay trailer is intentionally NOT emitted here — see render_terminal_summary.


def _sev_class(severity: str) -> str:
    """CSS severity class for a severity word, e.g. 'High' -> 'sev-high'.

    The result is derived from the (untrusted) severity text, so callers that
    interpolate it into an HTML ``class="..."`` attribute must wrap it in
    ``html.escape(..., quote=True)`` — it is intentionally not pre-escaped here,
    so the escape stays visible at the attribute boundary like every other field.
    """
    return "sev-" + str(severity).strip().lower()


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


def _raw_source_labels(provenance: object) -> list[str]:
    """The finding's raw, stripped provenance source labels in order.

    Each entry is either a bare string or an object carrying a ``source`` field
    (validated in Phase 2). Returns the canonical labels verbatim (e.g.
    ``rule--no-comments``, ``concern--bugs--opus``) so callers can both classify
    them (:func:`_parse_source_label`) and match them against a rule-quality
    note's canonical ``rule_source``.
    """
    labels: list[str] = []
    if not isinstance(provenance, list):
        return labels
    for entry in provenance:
        label = entry.get("source") if isinstance(entry, dict) else entry
        if isinstance(label, str) and label.strip():
            labels.append(label.strip())
    return labels


def _provenance_sources(provenance: object) -> list[tuple[str, str, str | None]]:
    """Parse a finding's provenance list into ordered ``(kind, name, model)`` tuples."""
    return [_parse_source_label(label) for label in _raw_source_labels(provenance)]


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


def _finding_bucket(finding: dict) -> str | None:
    """The finding's ``display_bucket`` — the validated visible/hidden routing key.

    Prefers the carried (validated) ``display_bucket`` and falls back to deriving
    it from ``(verdict, introduced_by)``, so the renderer still routes a hand-built
    envelope that omitted the derived field. See :func:`_derive_display_bucket`.
    """
    bucket = finding.get("display_bucket")
    if bucket in VALID_DISPLAY_BUCKETS:
        return bucket
    return _derive_display_bucket(finding.get("verdict"), finding.get("introduced_by"))


def _display_label(raw_id: object) -> str:
    """Render an internal ``f#``/``rq#`` id as its uppercase display label.

    The id assigned by :func:`finalize_records` *is* the visible label — there is
    no second number — so rendering just uppercases it (``f2`` → ``F2``, ``rq1`` →
    ``RQ1``). A missing/empty id renders as ``""`` rather than ``"None"`` (only
    reachable on hand-built, un-finalized input).
    """
    return str(raw_id).upper() if _is_nonempty_str(raw_id) else ""


def _finding_label(finding: object) -> str:
    """The uppercase ``F#`` display label for a finding (from its ``record_id``)."""
    return _display_label(finding.get("record_id") if isinstance(finding, dict) else None)


def _finding_number(finding: object) -> int:
    """Integer position of a finding's ``f#`` id (``f12`` → ``12``); the display sort key.

    :func:`finalize_records` already stamps ``f1..fN`` in display order, so sorting a
    bucket by this key reproduces that order deterministically. A missing or malformed
    id sorts last rather than raising.
    """
    if isinstance(finding, dict):
        rid = finding.get("record_id")
        if isinstance(rid, str) and _FINDING_ID_RE.match(rid):
            return int(rid[1:])
    return 10**9


def _partition_findings(findings: list) -> tuple[list, list, list]:
    """Split findings into the three *visible* sections by ``display_bucket``.

    Returns ``(confirmed, needs_decision, pre_existing)``, each ordered by the
    globally gap-free ``f#`` sequence :func:`finalize_records` assigned (visible
    findings number ``f1..fK`` in display order, so ``f# == visible position``).
    The ``hidden`` bucket — every Invalid finding plus any pre-existing Questionable
    — is recorded in ``records.json`` only and is intentionally dropped here: it is
    never rendered in review.md or the canvas.
    """
    confirmed = [f for f in findings if _finding_bucket(f) == "confirmed"]
    needs_decision = [f for f in findings if _finding_bucket(f) == "needs-decision"]
    pre_existing = [f for f in findings if _finding_bucket(f) == "pre-existing"]
    for bucket in (confirmed, needs_decision, pre_existing):
        bucket.sort(key=_finding_number)
    return confirmed, needs_decision, pre_existing


# ── rule-quality dependency map (deterministic preview) ──────────────────────


def _rule_dependency_map(findings: list, notes: list) -> dict[str, list[str]]:
    """Map each *invalidatable* finding to the rule-quality note ids it depends on.

    Returns ``{record_id: [RQ#, ...]}`` for findings that a rule fix could
    invalidate. A finding qualifies only when **all of its sources are rules
    being fixed** (Decision 12): it has at least one rule source, **no** concern
    source (an independent justification keeps it alive), no unrecognised source,
    and **every** one of its rule sources is named by a rule-quality note (so the
    listed RQ ids fully account for why the finding exists). The note ids are
    de-duplicated and emitted in the finding's provenance order.

    This is the single source of truth for the canvas live-preview (each row gets
    a ``data-rule-deps`` list and greys only once *all* its RQ ids are checked)
    and for resolving which ``record_id``s a scheduled rule fix invalidates: given
    an applied RQ-id set ``A``, the dying findings are exactly
    ``[rid for rid, deps in map.items() if set(deps) <= A]``.

    A finding kept alive by a concern source, or carrying a rule source with no
    corresponding note (which can never be "applied" — no checkbox), is absent
    from the map, so the canvas never live-greys it.
    """
    # Canonical rule_source label -> the note id that fixes it. A note now carries
    # a LIST of rule_sources (each rule it covers), so every label maps to that
    # note's id. validate_records rejects two notes naming the same source, so this
    # map is one-to-one for any validated envelope; the setdefault tiebreak below
    # is a defensive fallback that keeps the first note should unvalidated input
    # ever reach here.
    source_to_note: dict[str, str] = {}
    for note in notes:
        if not isinstance(note, dict):
            continue
        note_id = note.get("id")
        if not _is_nonempty_str(note_id):
            continue
        sources = note.get("rule_sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if _is_nonempty_str(source):
                source_to_note.setdefault(source.strip(), note_id)

    deps: dict[str, list[str]] = {}
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        record_id = finding.get("record_id")
        if not _is_nonempty_str(record_id):
            continue
        rule_labels: list[str] = []
        keep_alive = False  # a concern or unrecognised source blocks invalidation
        for label in _raw_source_labels(finding.get("provenance")):
            kind, _, _ = _parse_source_label(label)
            if kind == "rule":
                rule_labels.append(label)
            else:
                keep_alive = True
        if keep_alive or not rule_labels:
            continue
        note_ids: list[str] = []
        covered = True
        for label in rule_labels:
            note_id = source_to_note.get(label)
            if note_id is None:
                covered = False  # an un-noted rule can never be checked/fixed
                break
            if note_id not in note_ids:
                note_ids.append(note_id)
        if covered and note_ids:
            deps[record_id] = note_ids
    return deps


# ── review.md ────────────────────────────────────────────────────────────────


def _md_finding_block(finding: dict) -> str:
    """One Confirmed/Questionable finding block in the review.md shape."""
    parts: list[str] = []
    # Heading shape: "### F2. [severity] title".
    #
    # The finding's f# id (assigned by finalize_records, gap-free in display order)
    # IS the heading's leading token, rendered uppercase. It is globally unique, so
    # the post-mortem mode selects a finding unambiguously by matching the leading
    # "### F<n>." anchor (case-insensitively) — there is no second per-bucket number
    # and no redundant "(rN)" anchor (the old "### {n}. (rN)" shape, D-23, is gone).
    #
    # The uppercase id precedes the _flatten()-ed (CR/LF-collapsed) untrusted title,
    # so a hostile title can neither forge a second "### " heading line nor spoof a
    # different finding's id anchor.
    parts.append(
        f"### {_finding_label(finding)}. "
        f"[{finding.get('severity', '')}] {_flatten(finding.get('title', ''))}"
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
    keep working. Empty Confirmed / Needs-your-decision / Pre-existing / quality
    sections are omitted (the reporter's "omit a section with no findings" rule).
    Invalid findings are never rendered — they live in ``records.json`` only.
    """
    run = data.get("run", {})
    findings = data.get("findings", [])
    notes = data.get("rule_quality_notes", []) or []
    confirmed, needs_decision, pre_existing = _partition_findings(findings)

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
                f"| ❓ Needs your decision | {len(needs_decision)} |",
                f"| 📋 Pre-existing | {len(pre_existing)} |",
            ]
        )
    )
    blocks.append("---")

    if confirmed:
        blocks.append("## Confirmed Findings")
        blocks.extend(_md_finding_block(f) for f in confirmed)
    if needs_decision:
        blocks.append("## Needs Your Decision")
        blocks.extend(_md_finding_block(f) for f in needs_decision)
    if pre_existing:
        blocks.append("## Pre-existing")
        blocks.extend(_md_finding_block(f) for f in pre_existing)
    if notes:
        blocks.append(_md_quality_block(notes))

    return "\n\n".join(blocks) + "\n"


# ── terminal summary ─────────────────────────────────────────────────────────


def render_terminal_summary(data: dict, report_path: str) -> str:
    """Render the user-facing terminal summary string.

    Shape matches the reporter agent's relayed summary: report path, pipeline
    stats, an actionable findings table (Confirmed + Needs-your-decision, with a
    "Found by" column), a non-gating Pre-existing block, and rule-quality notes.
    The "relay everything above this line" trailer is intentionally NOT emitted
    here: it was a control instruction for an LLM agent's output; render-review is
    a tool whose stdout is pure data, and the orchestrator (SKILL.md) owns the
    relay-verbatim guarantee.
    """
    run = data.get("run", {})
    findings = data.get("findings", [])
    notes = data.get("rule_quality_notes", []) or []
    confirmed, needs_decision, pre_existing = _partition_findings(findings)
    # Pre-existing is non-gating (Decision 16), so the headline "actionable" count
    # and the main table are the in-scope buckets only; pre-existing is surfaced in
    # its own block below. Each bucket is already ordered by its f# id, so
    # concatenating (not re-sorting) keeps Confirmed (lower f#) ahead of
    # Needs-your-decision; the table keys on the globally-unique F# id.
    actionable = confirmed + needs_decision

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
                f"| {_finding_label(f)} "
                f"| {icon} "
                f"| {_md_cell(f.get('severity', ''))} "
                f"| {_md_cell(_found_by_terminal(f.get('provenance')))} "
                f"| {_md_cell(_location_str(f.get('file', ''), f.get('line')))} "
                f"| {_md_cell(f.get('title', ''))} |"
            )
        blocks.append("\n".join(table))
    else:
        blocks.append("✅ No actionable findings.")

    if pre_existing:
        pre = ["📋 Pre-existing (non-gating)"]
        for f in pre_existing:
            pre.append(
                f"- [{f.get('severity', '')}] {_flatten(f.get('title', ''))} "
                f"({_location_str(f.get('file', ''), f.get('line'))})"
            )
        blocks.append("\n".join(pre))

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


# A "complex" fix is the large/costly tier of the fix_complexity ordinal. It is
# flagged visually (a tag + subtle row tint) ORTHOGONALLY to the verdict bucket —
# a costly fix can appear in Confirmed, Needs-your-decision, or Pre-existing — and
# the tag is pure presentation: it never routes, hides, or downgrades a finding.
_COSTLY_FIX_COMPLEXITY = "complex"


def _is_costly_fix(finding: dict) -> bool:
    """True when the finding's ``fix_complexity`` is the large/costly tier."""
    return finding.get("fix_complexity") == _COSTLY_FIX_COMPLEXITY


def _canvas_finding_block(
    finding: dict,
    detail_html: str | None = None,
    dimmed: bool = False,
    *,
    dim_reason: str | None = None,
    rule_deps: list[str] | None = None,
    fixed: bool = False,
) -> str:
    """One ``.finding`` accordion block for the canvas (every text field escaped).

    ``detail_html`` is the already-nh3-sanitized rich-detail fragment (or
    ``None``). When present it is embedded after the structural fields, wrapped in
    ``<div class="rich-detail">``; when absent the panel renders the escaped
    structural fields only — the fail-closed "plain escaped text" behaviour.

    ``dimmed`` bakes the ``dimmed`` class onto the block so a finding the user
    persisted as *disregarded* — or invalidated by an applied rule fix (run state,
    read back by render-review) — stays dimmed across every re-render, not just
    within the live JS session. The action-bar script seeds its own disregarded
    set from these server-rendered classes, so the persisted dim survives a cold
    canvas load too, not only an in-session morph.

    ``dim_reason`` (e.g. "invalidated — rule RQ2 fixed") renders a small pill on
    the title explaining why the row is dimmed; it is used for rule-fix
    invalidation, where the dim carries an audit reason rather than being a silent
    disregard. ``rule_deps`` is the finding's rule-quality dependency list (RQ ids
    from :func:`_rule_dependency_map`): when present it is baked into a
    ``data-rule-deps`` attribute so the canvas can live-grey the row once *all* its
    RQ checkboxes are checked, with no agent round-trip.

    A large/costly fix (``fix_complexity == "complex"``) additionally gets the
    ``costly`` row class (a subtle tint) and a ``fix-tag`` pill on the title —
    pure presentation, orthogonal to the verdict bucket.

    ``fixed`` bakes the ``fixed`` class onto the block so a finding the orchestrator
    has actually fixed (persisted in run state, read back by render-review) renders
    as *done* (green ✓ + strikethrough) on every re-render. It is orthogonal to
    ``dimmed`` (disregard / rule-fix invalidation): a row can be ``finding`` /
    ``finding dimmed`` / ``finding fixed`` / ``finding fixed dimmed``, and when both
    apply the two treatments stack with no precedence logic.
    """
    rid = finding.get("record_id", "")
    label = _finding_label(finding)
    title = finding.get("title", "")
    severity = finding.get("severity", "")
    location = _location_str(finding.get("file", ""), finding.get("line"))
    aria = f"Select finding {label}: {title}"
    costly = _is_costly_fix(finding)

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

    # The fix-tag / dim-reason are trusted constant/derived markup appended AFTER
    # the escaped title, so a hostile title can never break out of the span (it is
    # still html.escape()-d). The reason text is escaped as well.
    title_html = html.escape(str(title))
    if costly:
        title_html += ' <span class="fix-tag">⚠ Large fix</span>'
    if dim_reason:
        title_html += f' <span class="dim-reason">{html.escape(str(dim_reason))}</span>'

    class_names = ["finding"]
    if fixed:
        class_names.append("fixed")
    if dimmed:
        class_names.append("dimmed")
    if costly:
        class_names.append("costly")
    classes = " ".join(class_names)
    # data-rule-deps lists the RQ ids whose collective fix would invalidate this
    # finding (omitted when the finding can never be rule-invalidated), so the
    # live-preview JS greys the row only once every listed checkbox is checked.
    deps_attr = ""
    if rule_deps:
        deps_attr = f' data-rule-deps="{html.escape(" ".join(rule_deps), quote=True)}"'
    return (
        f'      <div class="{classes}" data-record-id="{html.escape(str(rid), quote=True)}"{deps_attr}>\n'
        f'        <input type="checkbox" class="row-cb" aria-label="{html.escape(aria, quote=True)}">\n'
        '        <details class="finding-d" name="findings">\n'
        '          <summary class="finding-summary">\n'
        '            <span class="caret" aria-hidden="true"></span>\n'
        f'            <span class="num">{html.escape(label)}</span>\n'
        f'            <span class="sev {html.escape(_sev_class(severity), quote=True)}">{html.escape(str(severity))}</span>\n'
        f'            <span class="found-by">{_found_tags_html(finding.get("provenance"))}</span>\n'
        f'            <span class="title">{title_html}</span>\n'
        '          </summary>\n'
        '          <div class="detail-content">\n'
        f"{detail_content}\n"
        '          </div>\n'
        '        </details>\n'
        '      </div>'
    )


def _canvas_quality_item(note: dict) -> str:
    """One ``.quality-item`` block for the canvas.

    When the note carries a valid ``RQ#`` id, the item gains a schedulable
    checkbox (``quality-cb`` + ``data-rq-id``) so checking it live-previews which
    findings the rule fix would invalidate (the suggested change is shown inline).
    A note without a usable id renders read-only (no checkbox).
    """
    rule = html.escape(str(note.get("rule", "")))
    observation = html.escape(str(note.get("observation", "")))
    suggestion = html.escape(str(note.get("suggestion", "")))
    body = f"<strong>{rule}</strong>: {observation} — {suggestion}"

    note_id = note.get("id")
    if not _is_nonempty_str(note_id):
        return f'    <div class="quality-item">{body}</div>'

    rq = html.escape(str(note_id), quote=True)
    label = _display_label(note_id)
    aria = html.escape(f"Schedule rule fix {label}: {note.get('rule', '')}", quote=True)
    return (
        f'    <div class="quality-item" data-rq-id="{rq}">'
        f'<input type="checkbox" class="quality-cb" data-rq-id="{rq}" aria-label="{aria}">'
        f'<span class="quality-id">{html.escape(label)}</span> {body}</div>'
    )


def render_canvas_html(
    data: dict,
    template: str,
    details: dict[str, str] | None = None,
    disregarded: set[str] | None = None,
    parent_origin: str = DEFAULT_PARENT_ORIGIN,
    *,
    invalidated: dict[str, str] | None = None,
    fixed: set[str] | None = None,
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
    the ``dimmed`` class so the dim survives across re-renders. ``invalidated`` maps
    a ``record_id`` to a dim *reason* (e.g. "invalidated — rule RQ2 fixed") for
    findings a persisted rule fix has knocked out; render-review unions it with
    ``disregarded`` and reuses the same dim mechanism (not a hard drop), so the
    invalidation keeps an audit trail. Both are passed as data (no file IO here) to
    keep this function pure and testable, mirroring ``details``.

    ``fixed`` is the set of ``record_id``s persisted as fixed run state (read back
    from ``run-state.json``); their ``.finding`` block gains the ``fixed`` class so
    the canvas renders them as done (green ✓ + strikethrough) across re-renders. It
    is orthogonal to ``disregarded``/``invalidated`` (which drive the dim), so a row
    may carry both — the treatments stack.

    The rule-quality dependency map (:func:`_rule_dependency_map`) is computed from
    the envelope and threaded onto each row's ``data-rule-deps`` so the canvas can
    live-grey the rows a *scheduled* rule fix would invalidate — no agent round-trip.
    """
    details = details or {}
    disregarded = disregarded or set()
    invalidated = invalidated or {}
    fixed = fixed or set()
    run = data.get("run", {})
    findings = data.get("findings", [])
    notes = data.get("rule_quality_notes", []) or []
    confirmed, needs_decision, pre_existing = _partition_findings(findings)
    # Pre-existing is non-gating (Decision 16), so the headline "actionable" count
    # is the in-scope buckets only; the Pre-existing section is rendered but counted
    # separately. The hidden bucket (Invalid + pre-existing Questionable) is dropped.
    actionable_count = len(confirmed) + len(needs_decision)

    # Deterministic preview map (record_id -> [RQ ids]) so each row can advertise the
    # rule fixes that would invalidate it; the canvas greys it once they are all checked.
    rule_deps = _rule_dependency_map(findings, notes)

    def block(finding: dict) -> str:
        rid = finding.get("record_id")
        reason = invalidated.get(rid)
        return _canvas_finding_block(
            finding,
            details.get(rid),
            dimmed=(rid in disregarded or reason is not None),
            dim_reason=reason,
            rule_deps=rule_deps.get(rid),
            fixed=(rid in fixed),
        )

    meta = (
        f"Scope: {html.escape(str(run.get('scope', '')))} · "
        f"{html.escape(str(run.get('date', '')))} · "
        f"{run.get('rule_count', 0)} rules + {run.get('concern_count', 0)} concerns → "
        f"{run.get('consolidated_count', 0)} unique → {actionable_count} actionable"
    )
    badges = (
        f'<span class="summary-badge badge-confirmed"><span class="count">{len(confirmed)}</span> Confirmed</span>'
        f'<span class="summary-badge badge-needs-decision"><span class="count">{len(needs_decision)}</span> Needs your decision</span>'
        f'<span class="summary-badge badge-preexisting"><span class="count">{len(pre_existing)}</span> Pre-existing</span>'
    )

    replacements = {
        "{{RUN_ID}}": html.escape(str(run.get("run_id", "")), quote=True),
        "{{PARENT_ORIGIN}}": html.escape(str(parent_origin), quote=True),
        "<!-- FR:META -->": meta,
        "<!-- FR:SUMMARY_BADGES -->": badges,
        "<!-- FR:CONFIRMED_COUNT -->": str(len(confirmed)),
        "<!-- FR:CONFIRMED_ROWS -->": "\n".join(block(f) for f in confirmed),
        "<!-- FR:NEEDS_DECISION_COUNT -->": str(len(needs_decision)),
        "<!-- FR:NEEDS_DECISION_ROWS -->": "\n".join(block(f) for f in needs_decision),
        "<!-- FR:PREEXISTING_COUNT -->": str(len(pre_existing)),
        "<!-- FR:PREEXISTING_ROWS -->": "\n".join(block(f) for f in pre_existing),
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
    # The rule_file under-rules-dir check uses the repo's configured rules dir
    # (--rules-dir override, else resolved from --repo config). getattr keeps
    # callers that build a bare Namespace working.
    rules_dir = getattr(args, "rules_dir", None) or _resolve_rules_dir(
        getattr(args, "repo", None) or "."
    )
    data, errors = load_and_validate_records(records_path, rules_dir=rules_dir)

    if errors:
        _emit_validation_errors(records_path, errors)
        sys.exit(1)

    # The reporter emits semantic fields only. Assign the display layer (finding
    # f# ids + display_bucket, note rq# ids + rule label, run tallies) and the
    # canonical ordering deterministically in Python. finalize is idempotent, so
    # re-rendering an already-enriched records.json (SKILL Step 6d) is safe.
    data = finalize_records(data)
    # Defensive self-check: on correctly finalized data this is always empty; it
    # guards against a finalize regression or a hand-tampered enriched file.
    finalized_errors = validate_finalized_records(data, rules_dir=rules_dir)
    if finalized_errors:
        _emit_validation_errors(records_path, finalized_errors)
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
    # canvas dims those findings on every re-render. Rule-fix invalidations
    # (`rule_fixes_applied`) are the sibling key: their `invalidated_record_ids` are
    # unioned in and dimmed with a reason ("invalidated — rule RQ# fixed"), reusing
    # the same dim mechanism (an audit trail, not a hard drop). The `fixed` key
    # (written by `validate-action --apply-fixed`) is the third sibling: its
    # record_ids get the `.finding.fixed` "done" mark (orthogonal to the dim, so a
    # row may carry both). Fail-open (empty) when the state file is absent/unreadable
    # or belongs to a different run_id.
    run = data.get("run", {}) if isinstance(data, dict) else {}
    expected_run_id = run.get("run_id") if isinstance(run, dict) else None
    run_state = load_run_state(run_dir, expected_run_id=expected_run_id)
    disregarded = set(run_state.get("disregarded", []))
    invalidated = _invalidated_reasons(run_state.get("rule_fixes_applied", []))
    fixed = set(run_state.get("fixed", []))

    # Render both artifacts in memory, THEN write — so any render-time failure
    # also leaves no partial output. review.md is Markdown (raw text fields, no
    # HTML escaping); the canvas is ALWAYS written (gitignored, inert when
    # Treemon is down).
    review_md = render_review_markdown(data)
    canvas_html = render_canvas_html(
        data, template, details, disregarded, parent_origin,
        invalidated=invalidated, fixed=fixed,
    )
    # Persist the enriched records.json (the display-layer is now the single
    # source of truth read by validate-action / post-comments). Written in the
    # same all-or-nothing write phase as the rendered artifacts.
    _write_text(records_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    _write_text(review_out, review_md)
    _write_text(canvas_out, canvas_html)

    # Terminal summary -> stdout for verbatim relay (UTF-8, locale-independent).
    _emit_stdout(render_terminal_summary(data, review_out))


# ---------------------------------------------------------------------------
# validate-action (Phase 6): the canvas action-bar round-trip.
#
# The canvas posts {ids[], button, text, run_id} to the orchestrator. The `ids`
# are a single, prefix-disambiguated list: findings keep their record_id (r#),
# rule-quality notes use RQ#. Before the orchestrator does anything (and only
# after a human confirms), it calls `validate-action` to validate/expand the
# payload against records.json: the posted run_id must match the rendered run
# (rejecting a forged action from injected canvas JS), every id must resolve by
# prefix (r# -> finding file/line/title/suggestion; RQ# -> rule file + suggested
# change + the record_ids its fix invalidates), and an id matching neither prefix
# (or absent from the envelope) is rejected. Python's role stays mechanical — it
# validates a schema-defined contract and resolves stable ids; it never executes
# anything.
#
# `disregard` is the one action with persisted state: `--apply-disregard` records
# the resolved finding ids in run-state.json so render-review re-applies the dim
# across re-renders. See docs/spec/canvas-review-report.md and
# docs/spec/verdict-model-redesign.md (postMessage actions + Security Model).
# ---------------------------------------------------------------------------

# The canvas action bar posts exactly these namespaced verbs (the data-action
# values in templates/review-canvas.html). validate-action is fail-closed on the
# verb: anything outside this allowlist is rejected, never echoed back as a
# resolved action. Two verbs carry a persisted side effect (run-state.json) and
# are named separately so each can be gated on its own: ``disregard`` dims the
# selected findings, and ``fix`` is the verb under which scheduled rule fixes are
# applied (``--apply-rule-fixes`` writes their invalidated record_ids).
FIX_ACTION = "focused-review.fix"
DISREGARD_ACTION = "focused-review.disregard"
VALID_ACTIONS = (FIX_ACTION, DISREGARD_ACTION, "focused-review.document")

# Per-run state the canvas re-applies across renders: the `disregarded` record_ids
# (a silent dim) and `rule_fixes_applied` — the rule fixes the agent has committed,
# whose `invalidated_record_ids` dim with a reason. Both are add-only and
# run_id-stamped. Lives beside records.json in the run directory.
RUN_STATE_FILENAME = "run-state.json"


def _run_state_path(run_dir: str | None) -> str:
    """Path to the run-state file inside *run_dir* (defaults to the cwd)."""
    return os.path.join(run_dir or ".", RUN_STATE_FILENAME)


def _sanitize_rule_fixes(raw: object) -> list[dict]:
    """Normalize the persisted ``rule_fixes_applied`` list, dropping junk entries.

    Each kept entry is ``{rule_id, rule_source, invalidated_record_ids[]}`` with a
    non-empty ``rule_id`` (the identity); ``rule_source`` defaults to ``""`` and
    only string ``record_id``s survive. A non-list, or an entry missing its
    ``rule_id``, is skipped — the dim is additive, so malformed state must never
    raise or block the always-written canvas.
    """
    fixes: list[dict] = []
    if not isinstance(raw, list):
        return fixes
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        rule_id = entry.get("rule_id")
        if not _is_nonempty_str(rule_id):
            continue
        ids_raw = entry.get("invalidated_record_ids")
        ids = [r for r in ids_raw if _is_nonempty_str(r)] if isinstance(ids_raw, list) else []
        rule_source = entry.get("rule_source")
        fixes.append(
            {
                "rule_id": rule_id,
                "rule_source": rule_source if _is_nonempty_str(rule_source) else "",
                "invalidated_record_ids": ids,
            }
        )
    return fixes


def load_run_state(run_dir: str | None, expected_run_id: str | None = None) -> dict:
    """Read ``{run_dir}/run-state.json``; fail-open to an empty state.

    The persisted run state holds three sibling decision keys: the ``disregarded``
    record_ids the canvas dims on every re-render, the ``rule_fixes_applied`` entries
    (each ``{rule_id, rule_source, invalidated_record_ids[]}``) whose invalidated rows
    are dimmed with a reason, and the ``fixed`` record_ids the canvas marks done (a
    ``.finding.fixed`` row). Any read/parse problem — or a ``run_id`` that does not
    match *expected_run_id* (stale state from a different run) — yields an empty
    state: these marks are additive canvas affordances, never a hard dependency, so a
    missing or unreadable file must never block the always-written canvas.
    """
    empty: dict = {"disregarded": [], "rule_fixes_applied": [], "fixed": []}
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
    raw_fixed = data.get("fixed")
    fixed = [r for r in raw_fixed if _is_nonempty_str(r)] if isinstance(raw_fixed, list) else []
    return {
        "disregarded": disregarded,
        "rule_fixes_applied": _sanitize_rule_fixes(data.get("rule_fixes_applied")),
        "fixed": fixed,
    }


def _write_run_state(
    run_dir: str | None,
    run_id: str,
    disregarded: list[str],
    rule_fixes_applied: list[dict],
    fixed: list[str] | None = None,
) -> dict:
    """Serialize the full run-state envelope (all sibling keys) and write it.

    All three persist helpers route through here so writing one key always
    preserves the others — applying a disregard never wipes the recorded rule fixes
    or the fixed set, and vice-versa. The shape is ``{schema_version, run_id,
    disregarded[], rule_fixes_applied[], fixed[]}``.
    """
    state = {
        "schema_version": RECORDS_SCHEMA_VERSION,
        "run_id": run_id,
        "disregarded": list(disregarded),
        "rule_fixes_applied": list(rule_fixes_applied),
        "fixed": list(fixed or []),
    }
    _write_text(_run_state_path(run_dir), json.dumps(state, indent=2) + "\n")
    return state


def persist_disregard(run_dir: str | None, run_id: str, record_ids: list[str]) -> dict:
    """Merge *record_ids* into the run-state's disregarded set and write it.

    Disregard is monotonic within a run (add-only): the existing set is preserved
    and new ids appended in first-seen order, so render-review re-applies the dim
    on every subsequent re-render. The sibling ``rule_fixes_applied`` and ``fixed``
    keys are read back and re-written untouched. Returns the persisted state dict.
    The caller is expected to have validated *record_ids* (via
    :func:`validate_action`) first.
    """
    existing = load_run_state(run_dir, expected_run_id=run_id)
    disregarded = list(existing.get("disregarded", []))
    seen = set(disregarded)
    for rid in record_ids:
        if _is_nonempty_str(rid) and rid not in seen:
            seen.add(rid)
            disregarded.append(rid)
    return _write_run_state(
        run_dir,
        run_id,
        disregarded,
        existing.get("rule_fixes_applied", []),
        existing.get("fixed", []),
    )


def persist_rule_fixes(run_dir: str | None, run_id: str, fixes: list[dict]) -> dict:
    """Merge applied rule-fix *fixes* into run-state (add-only, run_id-stamped).

    Mirrors :func:`persist_disregard`. Each fix is ``{rule_id, rule_source,
    invalidated_record_ids[]}``; entries are merged by ``rule_id`` (a re-applied
    rule unions its invalidated ids, never duplicating or dropping), new rules are
    appended in first-seen order, and the sibling ``disregarded`` and ``fixed`` sets
    are preserved. render-review then re-applies the invalidation dim on every
    subsequent re-render. The caller (the validate-action round-trip) is expected to
    have resolved the ids via :func:`_rule_dependency_map` first.
    """
    existing = load_run_state(run_dir, expected_run_id=run_id)
    applied = [dict(e) for e in existing.get("rule_fixes_applied", [])]
    by_rule = {e["rule_id"]: e for e in applied if _is_nonempty_str(e.get("rule_id"))}
    for fix in fixes:
        if not isinstance(fix, dict):
            continue
        rule_id = fix.get("rule_id")
        if not _is_nonempty_str(rule_id):
            continue
        new_ids = [r for r in (fix.get("invalidated_record_ids") or []) if _is_nonempty_str(r)]
        rule_source = fix.get("rule_source") if _is_nonempty_str(fix.get("rule_source")) else ""
        entry = by_rule.get(rule_id)
        if entry is None:
            entry = {"rule_id": rule_id, "rule_source": rule_source, "invalidated_record_ids": []}
            by_rule[rule_id] = entry
            applied.append(entry)
        elif rule_source and not entry.get("rule_source"):
            entry["rule_source"] = rule_source
        seen = set(entry["invalidated_record_ids"])
        for rid in new_ids:
            if rid not in seen:
                seen.add(rid)
                entry["invalidated_record_ids"].append(rid)
    return _write_run_state(
        run_dir,
        run_id,
        existing.get("disregarded", []),
        applied,
        existing.get("fixed", []),
    )


def persist_fixed(run_dir: str | None, run_id: str, record_ids: list[str]) -> dict:
    """Merge *record_ids* into the run-state's fixed set and write it.

    Mirrors :func:`persist_disregard`. ``fixed`` is monotonic within a run
    (add-only): the existing set is preserved and new ids appended in first-seen
    order, so render-review re-applies the ``.finding.fixed`` mark on every
    subsequent re-render. The sibling ``disregarded`` and ``rule_fixes_applied``
    keys are read back and re-written untouched (the shared :func:`_write_run_state`
    writes all four keys, so marking a finding fixed never wipes the recorded
    disregards or rule fixes). Returns the persisted state dict. The caller is
    expected to have validated *record_ids* (via :func:`validate_action`) first.
    """
    existing = load_run_state(run_dir, expected_run_id=run_id)
    fixed = list(existing.get("fixed", []))
    seen = set(fixed)
    for rid in record_ids:
        if _is_nonempty_str(rid) and rid not in seen:
            seen.add(rid)
            fixed.append(rid)
    return _write_run_state(
        run_dir,
        run_id,
        existing.get("disregarded", []),
        existing.get("rule_fixes_applied", []),
        fixed,
    )


def _invalidated_reasons(rule_fixes_applied: object) -> dict[str, str]:
    """Map each invalidated ``record_id`` to a human dim reason from applied fixes.

    Inverts the persisted ``rule_fixes_applied`` list into ``{record_id: reason}``
    where the reason names the rule(s) that knocked the finding out — e.g.
    "invalidated — rule RQ2 fixed", or "invalidated — rules RQ2, RQ3 fixed" when a
    multi-rule finding lost all its rules. The rule ids are collected in first-seen
    order so the reason is deterministic across renders.
    """
    by_record: dict[str, list[str]] = {}
    for entry in _sanitize_rule_fixes(rule_fixes_applied):
        rule_id = entry["rule_id"]
        for rid in entry["invalidated_record_ids"]:
            rule_ids = by_record.setdefault(rid, [])
            if rule_id not in rule_ids:
                rule_ids.append(rule_id)
    reasons: dict[str, str] = {}
    for rid, rule_ids in by_record.items():
        if len(rule_ids) == 1:
            reasons[rid] = f"invalidated — rule {rule_ids[0]} fixed"
        else:
            reasons[rid] = f"invalidated — rules {', '.join(rule_ids)} fixed"
    return reasons


def _accumulated_rule_fixes(
    data: dict,
    run_dir: str | None,
    run_id: str,
    resolved_rules: list[dict],
) -> list[dict]:
    """Re-derive every applied rule's invalidated ids against the ACCUMULATED set.

    Finding C-11 / Decision 12: a finding whose only sources are rules dies once
    *all* of those rules are fixed — but rules can be fixed across **separate** apply
    actions. ``rule_fixes_applied`` is an add-only accumulator, so invalidation must
    be recomputed from the dependency map against the UNION of the rules already
    persisted in run-state and the rules in the current batch, not the posted batch
    alone. Otherwise a multi-rule finding whose rules are applied in different
    actions is never invalidated — each call only ever sees its own batch, never a
    superset of the finding's dependency set — so it stays permanently visible.

    Returns a :func:`persist_rule_fixes`-shaped list with one entry per applied rule
    (previously-persisted ones first, then this batch's, first-seen). A dying record
    is attributed to *every* applied rule it depends on, so the persisted state — the
    source of truth the re-render dims from — accounts for all the rules that fixed
    it (and :func:`_invalidated_reasons` names them all in the dim reason).
    """
    findings = data.get("findings")
    findings = findings if isinstance(findings, list) else []
    notes = data.get("rule_quality_notes")
    notes = notes if isinstance(notes, list) else []

    # Canonical rule_source for every rule applied so far: the already-persisted
    # entries plus the rules resolved from the current batch (the batch value
    # refreshes/fills the source). Insertion order — persisted first, then this
    # batch — gives the rebuilt entries a deterministic first-seen order.
    existing = load_run_state(run_dir, expected_run_id=run_id).get("rule_fixes_applied", [])
    rule_source: dict[str, str] = {}
    for entry in existing:
        rid = entry.get("rule_id")
        if _is_nonempty_str(rid):
            rule_source.setdefault(rid, entry.get("rule_source") or "")
    for rule in resolved_rules:
        rid = rule.get("rule_id")
        if _is_nonempty_str(rid):
            rule_source[rid] = rule.get("rule_source") or rule_source.get(rid, "")

    # Dying = findings whose full rule-dependency set is within the accumulated
    # applied set (Decision 12). _rule_dependency_map already excludes findings kept
    # alive by a concern/unrecognised source or carrying an un-noted rule.
    applied_ids = set(rule_source)
    dep_map = _rule_dependency_map(findings, notes)
    dying = {rid: deps for rid, deps in dep_map.items() if set(deps) <= applied_ids}

    return [
        {
            "rule_id": rule_id,
            "rule_source": source,
            "invalidated_record_ids": [
                rid for rid, deps in dying.items() if rule_id in deps
            ],
        }
        for rule_id, source in rule_source.items()
    ]


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


def _resolve_action_rule(note: dict, invalidated_record_ids: list[str]) -> dict:
    """Resolve a rule-quality note to the fields needed to apply its rule fix.

    The spec requires the rule file path, the suggested change, and the
    ``record_id``s the fix invalidates; ``rule`` / ``rule_source`` / ``observation``
    give the orchestrator (and the human) enough context to describe the change.
    ``rule_file`` is the schema-validated safe path under the rules directory the
    agent edits; ``invalidated_record_ids`` are the findings this rule fix knocks
    out *given the full posted RQ set* (Decision 12 — see :func:`validate_action`).
    These resolved ids are what the action reports back; the *persisted* invalidation
    is re-derived against the accumulated applied set by :func:`_accumulated_rule_fixes`
    (so a finding whose rules span separate applies still dims) — see Finding C-11.
    """
    return {
        "rule_id": note.get("id"),
        "rule": note.get("rule", ""),
        "rule_source": note.get("rule_source", ""),
        "rule_file": note.get("rule_file", ""),
        "observation": note.get("observation", ""),
        "suggestion": note.get("suggestion", ""),
        "invalidated_record_ids": invalidated_record_ids,
    }


def validate_action(
    data: object,
    run_id: object,
    ids: list[str],
    *,
    action: str | None = None,
    instructions: str = "",
    rules_dir: str | None = None,
) -> tuple[dict | None, list[dict]]:
    """Validate/expand a posted canvas action against a records.json envelope.

    Returns ``(expanded, errors)``. ``errors`` is a list of structured per-id error
    dicts (same shape as :func:`validate_records`, scope ``"action"``); when it is
    empty, ``expanded`` is the resolved action. ``ids`` is the unified,
    prefix-disambiguated id list the canvas posts — findings keep their
    ``record_id`` (``r#``) and rule-quality notes use ``RQ#``. Each id is resolved
    **by prefix**: an ``r#`` to a finding (file/line/title/suggestion) in
    ``expanded["findings"]``, an ``RQ#`` to a rule (rule_file/suggested change/the
    ``record_id``s its fix invalidates) in ``expanded["rules"]``. An id matching
    *neither* prefix, or a well-formed id absent from the envelope, is rejected —
    the trust boundary stays in Python because the canvas content is untrusted.

    The ``action`` verb (when present) must be one of :data:`VALID_ACTIONS`; a
    mismatched/forged ``run_id`` is rejected. Never raises; never executes anything.

    ``rules_dir`` is the configured review-rules directory; each resolved rule's
    ``rule_file`` is re-validated against it (path safety + ``rule_source``
    consistency) because the action round-trip loads records.json *without* schema
    validation, so this is the only place those checks run at action time (Findings
    C-12 / C-13). When ``None`` it defaults to :data:`DEFAULT_RULES_DIR` so a caller
    that omits it still gets the secure-by-default ``review/`` prefix check.
    """
    errors: list[dict] = []

    # Action verb allowlist (fail-closed). A forged/arbitrary verb is rejected
    # here rather than echoed back as a "resolved" action. ``None`` means
    # "resolve only" (no verb posted) and is permitted — the CLI ``--action``
    # defaults to None and the pure-resolve callers rely on it.
    if action is not None and action not in VALID_ACTIONS:
        errors.append(
            _records_error(
                "action", None, "action", "action",
                f"unknown action {action!r}: must be one of "
                f"{', '.join(VALID_ACTIONS)}",
            )
        )

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

    # Index findings by stable record_id and rule-quality notes by RQ id so each
    # posted id resolves by prefix (or is rejected as unknown).
    findings_list = data.get("findings")
    findings_list = findings_list if isinstance(findings_list, list) else []
    by_record_id: dict[str, dict] = {}
    for finding in findings_list:
        if isinstance(finding, dict):
            fid = finding.get("record_id")
            if _is_nonempty_str(fid):
                by_record_id.setdefault(fid, finding)

    notes_list = data.get("rule_quality_notes")
    notes_list = notes_list if isinstance(notes_list, list) else []
    by_note_id: dict[str, dict] = {}
    for note in notes_list:
        if isinstance(note, dict):
            nid = note.get("id")
            if _is_nonempty_str(nid):
                by_note_id.setdefault(nid, note)

    resolved_findings: list[dict] = []
    matched_notes: list[dict] = []
    if not ids:
        errors.append(
            _records_error(
                "action", None, "ids", "ids",
                "no ids provided; a canvas action must target at least one finding "
                "(r#) or rule-quality note (RQ#)",
            )
        )
    for i, raw in enumerate(ids):
        if not _is_nonempty_str(raw):
            errors.append(
                _records_error(
                    "action", i, f"ids[{i}]", "id",
                    "id must be a non-empty string",
                    record_id=raw if isinstance(raw, str) else None,
                )
            )
            continue
        token = raw.strip()
        if _FINDING_ID_RE.match(token):
            finding = by_record_id.get(token)
            if finding is None:
                errors.append(
                    _records_error(
                        "action", i, f"ids[{i}]", "record_id",
                        f"unknown record_id {token!r}: not present in records.json",
                        record_id=token,
                    )
                )
            else:
                resolved_findings.append(_resolve_action_finding(finding))
        elif _RULE_QUALITY_NOTE_ID_RE.match(token):
            note = by_note_id.get(token)
            if note is None:
                errors.append(
                    _records_error(
                        "action", i, f"ids[{i}]", "rule_id",
                        f"unknown rule-quality note id {token!r}: not present in records.json",
                        record_id=token,
                    )
                )
            else:
                matched_notes.append(note)
        else:
            errors.append(
                _records_error(
                    "action", i, f"ids[{i}]", "id",
                    f"unrecognized id {token!r}: must be a finding id (r#) or a "
                    f"rule-quality note id (RQ#)",
                    record_id=token,
                )
            )

    # Defense-in-depth (Findings C-12 / C-13): the action round-trip loads
    # records.json via _load_records_only, which deliberately SKIPS schema
    # validation — so the rule_file path-safety check and the rule_source
    # consistency check that render-review applied are NOT in force here, at the
    # very point the agent consumes rule_file to *edit* it. Re-apply both against
    # the configured rules_dir (defaulting to the secure-by-default review/ prefix
    # when omitted, mirroring validate_records) so validate_action is a
    # self-sufficient trust boundary — closing the TOCTOU window and the
    # never-rendered-records.json path — instead of trusting an upstream render.
    effective_rules_dir = DEFAULT_RULES_DIR if rules_dir is None else rules_dir
    for note_index, note in enumerate(matched_notes):
        note_id = note.get("id")
        for message in _collect_rule_file_errors(
            note.get("rule_file", ""), note.get("rule_source"), effective_rules_dir
        ):
            errors.append(
                _records_error(
                    "action", note_index, f"rules[{note_index}].rule_file",
                    "rule_file", message, record_id=note_id,
                )
            )

    if errors:
        return None, errors

    # Resolve each matched rule-quality note to its rule file + suggested change +
    # the record_ids its fix invalidates. Per Decision 12 a finding dies only when
    # *all* of its rule sources are in the applied set, so the invalidation is
    # computed against the *full* set of posted RQ ids and then attributed back to
    # each rule that knocked the finding out — a multi-rule finding lists under
    # every rule it depends on, which is exactly what persist_rule_fixes expects.
    resolved_rules: list[dict] = []
    if matched_notes:
        applied = {n.get("id") for n in matched_notes}
        dep_map = _rule_dependency_map(findings_list, notes_list)
        dying = {rid: deps for rid, deps in dep_map.items() if set(deps) <= applied}
        for note in matched_notes:
            note_id = note.get("id")
            invalidated = [rid for rid, deps in dying.items() if note_id in deps]
            resolved_rules.append(_resolve_action_rule(note, invalidated))

    expanded = {
        "valid": True,
        "schema_version": RECORDS_SCHEMA_VERSION,
        "action": action,
        "run_id": run_id,
        "instructions": instructions or "",
        "record_count": len(resolved_findings),
        "rule_count": len(resolved_rules),
        "findings": resolved_findings,
        "rules": resolved_rules,
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


def _split_ids(raw: str | None) -> list[str]:
    """Parse the comma-separated ``--ids`` value into a de-duped list.

    The posted ids are per-run tokens (findings ``r1``, ``r2``, …; rule-quality
    notes ``RQ1``, ``RQ2``, …) so a comma is a safe separator. Order is preserved
    (first-seen) and blanks/dupes are dropped. The id *type* is disambiguated later
    by prefix in :func:`validate_action`.
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

    On success prints the resolved action JSON to stdout (exit 0) — ``findings[]``
    (resolved ``r#`` ids) and ``rules[]`` (resolved ``RQ#`` ids). On a forged
    run_id, an unknown/unrecognised id, an unknown action verb, or a
    missing/unparseable records.json, prints the structured errors to stderr and
    exits 1 — so the orchestrator rejects the action instead of executing it.

    Three flags persist run-state after a successful validation, each bound to its
    verb and gated behind the human-confirmation step in SKILL.md:
    ``--apply-disregard`` (``focused-review.disregard`` only) dims the resolved
    **finding** ids; ``--apply-rule-fixes`` (``focused-review.fix`` only) writes the
    resolved **rules'** invalidated record_ids so the re-render dims them with a
    reason; ``--apply-fixed`` (``focused-review.fix`` only) marks the resolved
    **finding** ids done so the re-render bakes the ``.finding.fixed`` class.
    ``focused-review.fix`` thus carries two independent side effects
    (``--apply-rule-fixes`` and ``--apply-fixed``) that may both fire from a single
    call and persist through the shared writer without clobbering each other. Each
    flag refuses to run for any other verb.
    """
    path: str = args.records
    posted_run_id = args.run_id
    action = args.action
    instructions = args.instructions or ""
    ids = _split_ids(args.ids)

    # --apply-disregard is bound to the disregard verb only: it is the one side
    # effect that persists state, so refuse the flag for any other (or absent)
    # action rather than silently writing run-state for a fix/document call.
    # Fail-closed and fail-fast — checked before any records IO.
    if getattr(args, "apply_disregard", False) and action != DISREGARD_ACTION:
        _emit_action_error(
            path, posted_run_id, action,
            [_records_error(
                "action", None, "apply_disregard", "action",
                f"--apply-disregard requires --action {DISREGARD_ACTION!r}, "
                f"got {action!r}; refusing to persist disregard state for a "
                f"non-disregard action",
            )],
        )
        sys.exit(1)

    # --apply-rule-fixes is the symmetric gate for rule-fix invalidation: it is
    # bound to the fix verb (rule fixes are applied under "Fix"), so refuse it for
    # any other (or absent) action rather than writing rule-fix run-state for a
    # disregard/document call. Fail-closed and fail-fast, before any records IO.
    if getattr(args, "apply_rule_fixes", False) and action != FIX_ACTION:
        _emit_action_error(
            path, posted_run_id, action,
            [_records_error(
                "action", None, "apply_rule_fixes", "action",
                f"--apply-rule-fixes requires --action {FIX_ACTION!r}, "
                f"got {action!r}; refusing to persist rule-fix state for a "
                f"non-fix action",
            )],
        )
        sys.exit(1)

    # --apply-fixed is the second fix-bound flag: it marks the resolved FINDING
    # ids done (a .finding.fixed "done" mark), distinct from --apply-rule-fixes,
    # which dims rule-invalidated rows. Like its siblings it is the gate for a
    # persisted side effect, so refuse it for any other (or absent) action rather
    # than writing fixed run-state for a disregard/document call. Fail-closed and
    # fail-fast, before any records IO.
    if getattr(args, "apply_fixed", False) and action != FIX_ACTION:
        _emit_action_error(
            path, posted_run_id, action,
            [_records_error(
                "action", None, "apply_fixed", "action",
                f"--apply-fixed requires --action {FIX_ACTION!r}, "
                f"got {action!r}; refusing to persist fixed state for a "
                f"non-fix action",
            )],
        )
        sys.exit(1)

    data, load_error = _load_records_only(path)
    if load_error is not None:
        _emit_action_error(path, posted_run_id, action, [load_error])
        sys.exit(1)

    # Resolve the review-rules directory the same way render-review/validate-records
    # do (--rules-dir override, else the repo's configured value). validate_action
    # re-validates each resolved rule_file against it, so the action path enforces
    # the same rule_file trust boundary as the render path even though records.json
    # is loaded here without schema validation. getattr keeps programmatic callers
    # that build a bare Namespace working.
    rules_dir = getattr(args, "rules_dir", None) or _resolve_rules_dir(
        getattr(args, "repo", None) or "."
    )

    resolved, errors = validate_action(
        data, posted_run_id, ids, action=action, instructions=instructions,
        rules_dir=rules_dir,
    )
    if errors:
        _emit_action_error(path, posted_run_id, action, errors)
        sys.exit(1)

    assert resolved is not None  # errors empty => resolved populated
    resolved["records_path"] = str(path)

    # Disregard is one of the two actions that persist state. Only after a
    # successful validation (so a forged run_id / unknown id can never write
    # state). The ``action == DISREGARD_ACTION`` guard is co-located with the side
    # effect so it holds even for a caller that bypasses the argparse/early gate
    # above. Only the resolved *finding* ids are disregarded — any RQ# in the mix
    # is a rule fix, not a record to dim, so it is never written to the disregarded set.
    if getattr(args, "apply_disregard", False) and action == DISREGARD_ACTION:
        run_dir = args.run_dir or os.path.dirname(path) or "."
        disregard_ids = [f["record_id"] for f in resolved["findings"]]
        state = persist_disregard(run_dir, posted_run_id, disregard_ids)
        resolved["disregarded"] = state.get("disregarded", [])
        resolved["run_state_path"] = _run_state_path(run_dir)

    # Rule fixes are the other persisted side effect: after the agent has edited
    # the rule files (the human-confirmed manual step), this writes the invalidated
    # record_ids into run-state so the re-render dims them with an audit reason.
    # Invalidation is recomputed by ``_accumulated_rule_fixes`` against the UNION of
    # the rules already persisted and this batch — not the posted batch alone — so a
    # multi-rule finding still dies when its rules are fixed across SEPARATE apply
    # actions (Finding C-11 / Decision 12); the add-only accumulator alone never
    # would. ``persist_rule_fixes`` is add-only and preserves the sibling
    # ``disregarded`` set.
    if getattr(args, "apply_rule_fixes", False) and action == FIX_ACTION:
        run_dir = args.run_dir or os.path.dirname(path) or "."
        fixes = _accumulated_rule_fixes(data, run_dir, posted_run_id, resolved["rules"])
        state = persist_rule_fixes(run_dir, posted_run_id, fixes)
        resolved["rule_fixes_applied"] = state.get("rule_fixes_applied", [])
        resolved["run_state_path"] = _run_state_path(run_dir)

    # --apply-fixed is the fix verb's second persisted side effect: after the agent
    # has applied the user-confirmed code fixes, this marks the resolved FINDING
    # ids done so the re-render bakes the ``.finding.fixed`` class. Persisted like
    # --apply-disregard — the resolved *finding* ids ([f["record_id"] …]), NOT the
    # rules / _accumulated_rule_fixes — because "fixed" and rule-fix invalidation
    # are orthogonal states. Only after a successful validation (so a forged run_id
    # / unknown id never writes), and the ``action == FIX_ACTION`` guard is
    # co-located with the side effect so it holds even for a caller that bypasses
    # the early gate above. ``persist_fixed`` is add-only and routes through the
    # shared four-key ``_write_run_state``, preserving the sibling ``disregarded``
    # and ``rule_fixes_applied`` keys — so --apply-fixed and --apply-rule-fixes may
    # both fire from one fix call without clobbering each other.
    if getattr(args, "apply_fixed", False) and action == FIX_ACTION:
        run_dir = args.run_dir or os.path.dirname(path) or "."
        fixed_ids = [f["record_id"] for f in resolved["findings"]]
        state = persist_fixed(run_dir, posted_run_id, fixed_ids)
        resolved["fixed"] = state.get("fixed", [])
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
    validate_parser.add_argument(
        "--repo",
        default=".",
        help="Repository root, used to resolve the configured rules_dir (default: .)",
    )
    validate_parser.add_argument(
        "--rules-dir",
        default=None,
        help="Override the review-rules directory used to validate note rule_file paths",
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
        "--rules-dir",
        default=None,
        help="Override the review-rules directory used to validate note rule_file paths",
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
        "--repo",
        default=".",
        help="Repository root, used to resolve the configured rules_dir (default: .)",
    )
    action_parser.add_argument(
        "--rules-dir",
        default=None,
        help="Override the review-rules directory used to re-validate note rule_file paths",
    )
    action_parser.add_argument(
        "--run-id",
        required=True,
        help="The run_id from the posted action (must match records.json's run.run_id)",
    )
    action_parser.add_argument(
        "--ids",
        required=True,
        help="Comma-separated stable ids the action targets: finding ids (r#) "
             "and/or rule-quality note ids (RQ#), e.g. r1,r3,RQ2",
    )
    action_parser.add_argument(
        "--action",
        default=None,
        choices=VALID_ACTIONS,
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
        help="After validation, persist the resolved finding ids as disregarded "
             "run state (canvas dims them across re-renders). Use only after human "
             "confirmation.",
    )
    action_parser.add_argument(
        "--apply-rule-fixes",
        action="store_true",
        help="After validation, persist the resolved rules' invalidated record_ids "
             "as rule-fix run state (canvas dims them with a reason across "
             "re-renders). Bound to --action focused-review.fix. Use only after the "
             "rule files have been edited and the human has confirmed.",
    )
    action_parser.add_argument(
        "--apply-fixed",
        action="store_true",
        help="After validation, persist the resolved finding ids as fixed run "
             "state (canvas marks them done — a green check + strikethrough — "
             "across re-renders). Bound to --action focused-review.fix; may be "
             "combined with --apply-rule-fixes in one call. Use only after the "
             "code fixes have been applied and the human has confirmed.",
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
