# Configurable Rules Directory

## Goals

1. A `focused-review.json` config file controls which directory rules are read from/written to.
2. Passing `--rules-dir X` on the Python CLI overrides the config file.
3. No config file falls back to `review/` (backward compatible).
4. Windows path separators are normalized.
5. `/focused-review configure` provides an interactive flow to create/update the config file.

## Config File

### Format

```json
{ "rules_dir": "custom-rules/" }
```

### Scan Locations (priority order)

1. `.claude/focused-review.json` — project shared (version-controlled)
2. `focused-review.json` — repo root (platform-agnostic)
3. `.github/focused-review.json` — GitHub convention
4. `~/.claude/focused-review.json` — user-wide (Claude Code)
5. `~/.copilot/focused-review.json` — user-wide (Copilot CLI)

First match wins. If none found → default `review/`.

## Expected Behavior

### Resolution Priority

The rules directory resolves in this order:
1. Explicit `--rules-dir` CLI flag (Python script only)
2. `focused-review.json` config file (first match from scan locations)
3. `review/` (default)

Relative paths are relative to repo root. Path separators are normalized for Windows compatibility.

### Review Mode

SKILL.md resolves the rules directory once at the start via a Python one-liner that scans config file locations. The resolved value is passed to the Python script via `--rules-dir`.

### Refresh Mode

SKILL.md resolves the rules directory once at the start. Rules are created/updated/deleted in the resolved directory. On first run (no existing rules), the skill tells the user:

> "Rules will be stored in `{rules_dir}`. Run `/focused-review configure` to change the rules directory."

### Python Script

The `--rules-dir` argument's default calls a `_resolve_rules_dir()` function that scans the same config file locations as SKILL.md. Explicit `--rules-dir` on the command line still takes precedence.

### Configure Mode

`/focused-review configure` runs an interactive flow:

1. **Detect platform**: `COPILOT_CLI` env var present → Copilot CLI, otherwise → Claude Code
2. **Ask for rules directory**: Show current resolved value, ask for new value (Enter = keep)
3. **Ask where to save**: Present location options based on platform:
   - Claude Code: `.claude/focused-review.json`, `focused-review.json`, `.github/focused-review.json`, `~/.claude/focused-review.json`
   - Copilot CLI: `focused-review.json`, `.github/focused-review.json`, `~/.copilot/focused-review.json`
4. **Write the file**: Read existing JSON or start from `{}`, set `rules_dir`, write with indent=2. Create parent dir if needed.
5. **Confirm**: Tell user what was written. If project-shared location, remind to commit.

## Technical Approach

### Python Changes (`focused-review.py`)

- Add `_resolve_rules_dir()` function that scans config file locations
- Change `--rules-dir` default from env var to `_resolve_rules_dir()`
- Keep Windows path normalization

### SKILL.md Changes

- Update argument-hint to include `configure`
- Add `configure` to mode dispatch
- Replace env var resolution one-liner with config file scanner one-liner
- Add Configure Mode section between Mode Selection and Review Mode
- Update first-run message to reference `/focused-review configure`

### Test Changes

- Rewrite `test_rules_dir_resolution.py` for config file instead of env var

### Documentation Updates

- `docs/spec/focused-review.md`: add `configure` to arguments, update config references
- `README.md`: update configuration section for config file
- `CLAUDE.md`: update architecture notes

## Decisions

- **Config file over env var**: works identically on Claude Code and Copilot CLI, no platform-specific settings mechanism needed, dedicated file is discoverable
- **Multiple scan locations**: project-scoped (3 options for team preference) + user-scoped (platform-aware)
- **Default stays `review/`**: backward compatible, no migration needed
- **SKILL.md resolves once**: avoids repeated config file reads throughout the prompt
- **Platform detection via `COPILOT_CLI` env var**: set by Copilot CLI runtime, absent in Claude Code
