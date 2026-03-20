---
autofix: false
model: inherit
applies-to: "**/*.py"
source: "CLAUDE.md"
---
# No Python Parsing of LLM Output

## Rule
Python code must not parse or interpret LLM-generated content (review reports, findings, assessed.md). Use skill/agent orchestration for anything that requires understanding report content.

## Why
The LLM that produced content can read it natively — routing semantic interpretation through Python adds fragile parsing logic that breaks when output format changes. Python should handle only structured/mechanical tasks (git, file I/O, config, CLI tool invocation).

## Requirements
- Python code must not read and interpret the semantic content of LLM-generated files (findings, reports, assessed.md, review output)
- Python code must not extract structured data from LLM prose (e.g., regex on markdown findings to count severity levels)
- File I/O is fine— Python can read/write/copy LLM output files as opaque blobs; it just must not interpret their meaning
- Python can parse structured formats it controls (JSON config, dispatch.json, CLI arguments) — the restriction is on LLM-generated content

## Wrong
```python
# Python reads and interprets LLM-generated findings
findings = Path(".agents/focused-review/findings.md").read_text()
critical_count = len(re.findall(r"\*\*Severity:\*\* Critical", findings))
if critical_count > 0:
    print(f"Found {critical_count} critical issues")
```

## Correct
```python
# Python handles file I/O only — copies findings for the orchestrator skill to interpret
src = Path(".agents/focused-review/findings.md")
dst = Path(".agents/focused-review/report/findings.md")
shutil.copy(src, dst)
```
