# Configurable Rules Directory

## Goals

1. Setting `FOCUSED_REVIEW_RULES_DIR=custom-rules/` causes both review and refresh modes to read/write rules from `custom-rules/` instead of `review/`.
2. Passing `--rules-dir X` on the CLI overrides the env var.
3. Omitting both falls back to `review/` (backward compatible).
4. Windows path separators are normalized.

## Expected Behavior

### Resolution Priority

The rules directory resolves in this order:
1. Explicit `--rules-dir` CLI flag (Python script only)
2. `FOCUSED_REVIEW_RULES_DIR` environment variable
3. `review/` (default)

Both relative paths (relative to repo root) and absolute paths are accepted. Path separators are normalized for Windows compatibility.

### Review Mode

SKILL.md resolves the rules directory once at the start of Review Mode and passes the resolved value to the Python script via `--rules-dir`. All user-facing messages reference the resolved directory, not the hardcoded default.

### Refresh Mode

SKILL.md resolves the rules directory once at the start of Refresh Mode. Rules are created/updated/deleted in the resolved directory. On first run (no existing rules), the skill tells the user:

> "Rules will be stored in `{rules_dir}`. To use a different directory, set `FOCUSED_REVIEW_RULES_DIR` in your Claude Code settings (`env` section) or shell environment."

### Python Script

The `--rules-dir` argument's default changes from the hardcoded `"review/"` to: `os.environ.get("FOCUSED_REVIEW_RULES_DIR", "review/")`. Explicit `--rules-dir` on the command line still takes precedence.

## Technical Approach

### Python Changes (`focused-review.py`)

- Change `--rules-dir` default from `"review/"` to `os.environ.get("FOCUSED_REVIEW_RULES_DIR", "review/")`
- Normalize path separators for Windows

### SKILL.md Changes

- Add a rules-dir resolution block near the top (after script path resolution), using a Python one-liner or inline logic to read the env var
- Replace all ~15 hardcoded `review/` references with the resolved value
- Add first-run messaging in Refresh Mode

### Documentation Updates

- README.md: document `FOCUSED_REVIEW_RULES_DIR` in configuration section
- `docs/spec/focused-review.md`: update rules directory references to note configurability
- `CLAUDE.md`: update architecture notes

## Decisions

- **Env var over config file**: env var is settable via Claude Code `settings.json` `env` section, follows existing `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` pattern, no extra files
- **Default stays `review/`**: backward compatible, no migration needed
- **SKILL.md resolves once**: avoids repeated env var checks throughout the prompt
