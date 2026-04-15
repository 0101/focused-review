---
model: inherit
applies-to: "**/*.py"
source: "CLAUDE.md"
---
# Windows Path Compatibility

## Rule
All path handling must work on Windows. Use `pathlib.Path` or `os.path` instead of string manipulation with hardcoded separators.

## Why
The primary development environment is Windows. Hardcoded forward-slash path construction, Unix-only path assumptions, or shell commands that assume `/` as separator will break on Windows.

## Requirements
- Use `pathlib.Path` or `os.path.join` for path construction instead of string concatenation or f-strings with `/`
- Do not assume `/` as path separator in path operations (string splitting, joining, prefix checking)
- Do not use Unix-only commands or path conventions (e.g., `~` expansion without `Path.expanduser()`)
- `PurePosixPath` is acceptable only when constructing paths for non-filesystem use (URLs, git refs)
- Forward slashes in glob patterns are fine — Python's `glob` and `pathlib.glob` handle them cross-platform

## Wrong
```python
# Hardcoded forward slash in path construction
rule_path = rules_dir + "/" + rule_name + ".md"
output_dir = f".agents/focused-review/{scope}"
relative = full_path.replace(repo_root + "/", "")
```

## Correct
```python
# pathlib handles separators correctly on all platforms
rule_path = Path(rules_dir) / f"{rule_name}.md"
output_dir = Path(".agents") / "focused-review" / scope
relative = full_path.relative_to(repo_root)
```
