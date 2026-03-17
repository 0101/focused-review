# Marketplace Publishing

## Goals

Enable discovery and installation of the focused-review plugin through both Copilot CLI and Claude Code marketplace systems, using a single self-hosted dual-target marketplace hosted in this repo.

## Expected Behavior

- Copilot CLI users can add the marketplace and install the plugin:
  ```
  copilot plugin marketplace add 0101/focused-review
  copilot plugin install focused-review@focused-review
  ```
- Claude Code users can add the marketplace and install the plugin:
  ```
  /plugin marketplace add 0101/focused-review
  /plugin install focused-review@focused-review
  ```
- The skill loads and functions identically regardless of which tool installed it.
- A single `marketplace.json` in `.claude-plugin/` serves both tools (Copilot CLI cross-reads `.claude-plugin/`).

## Technical Approach

### Dual Plugin Manifest

- Keep existing `plugin.json` at repo root for Copilot CLI compatibility.
- Create `.claude-plugin/plugin.json` for Claude Code. Both manifests share `name` and `version` values.
- Skills and agents are auto-discovered from `skills/` and `agents/` directories by both tools; no explicit listing needed in `.claude-plugin/plugin.json`.

### Self-Hosted Marketplace

- Create `.claude-plugin/marketplace.json` with the plugin catalog.
- Both Copilot CLI and Claude Code read this file natively -- no separate `.github/plugin/marketplace.json` required.
- `source: "."` points to the repo root as the plugin source.

### README Installation Instructions

- Replace existing Installation section in README.md with marketplace-based instructions for both Copilot CLI and Claude Code.
- Remove old symlink-based Claude Code installation instructions.

### Release Tagging

- Tag `v0.1.0` for version pinning via git refs.

## Decisions

- **Self-hosted over official marketplace**: Full control, no approval process. Official marketplace submission deferred to a future iteration.
- **Single marketplace.json in `.claude-plugin/`**: Copilot CLI cross-reads this directory, eliminating duplication.
- **Keep root `plugin.json` as-is**: Backward compatible; explicit `skills`/`agents` arrays are harmless.
