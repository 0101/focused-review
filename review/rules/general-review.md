---
autofix: false
model: inherit
source: "built-in"
---
# General Review

## Rule
Catch issues a careful reviewer would notice that don't fit into bug, security, or architecture categories: incomplete changes, misleading names, semantic inconsistencies, and documentation drift.

## Why
Specialized reviewers focus narrowly. This rule covers the gaps — the things an experienced developer notices on a fresh read of the diff that no single-purpose check targets.

## Requirements
- Flag incomplete changes: new enum values without updated switch statements, new parameters not passed by all callers, renames applied in one place but not related places, features added without tests/docs/config
- Flag misleading names or comments: variable/function names that contradict what the code does, comments describing behavior the code no longer implements, copy-pasted docstrings describing different parameters
- Flag semantic inconsistencies: method says it returns "count" but returns an index, error messages describing the wrong operation, log levels that don't match severity
- Flag documentation drift: public API changes without doc updates, README instructions that no longer work, configuration examples referencing removed options
- Use `grep` and `view` to check callers, verify switch coverage, and confirm that names match behavior — do not guess
- Do NOT flag style preferences (brace placement, blank lines, import ordering)
- Do NOT flag TODOs or FIXMEs that were already present before the diff
- Do NOT suggest rewrites larger than the diff itself
- Do NOT duplicate bug, security, or architecture concerns — if the issue is a logic error, null deref, injection, or coupling problem, leave it to those reviewers

## Wrong
```
// Adds new enum value but forgets to update the switch
enum Status { Active, Inactive, Suspended }  // Suspended is new

// Elsewhere, unchanged:
switch (status) {
    case Active: ...
    case Inactive: ...
    // Missing: case Suspended
}
```

## Correct
```
enum Status { Active, Inactive, Suspended }

switch (status) {
    case Active: ...
    case Inactive: ...
    case Suspended: ...  // All values handled
}
```
