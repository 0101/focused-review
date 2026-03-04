---
autofix: false
model: inherit
source: "built-in"
---
# Bug Spotter

## Rule
Find bugs, logic errors, and correctness issues in new or changed code. Nothing else.

## Why
Dedicated bug-finding without distraction from style, naming, or documentation concerns. A focused reviewer examining only correctness catches issues that broader reviews miss under cognitive load.

## Requirements
- Look for logic errors: wrong comparisons, off-by-one, boundary conditions (`>` vs `>=`, `<` vs `<=`)
- Look for state management bugs: variables not updated in all branches, missing else clauses, incomplete switch cases
- Look for null/resource bugs: use-after-dispose, null dereference on error paths, missing cleanup
- Look for concurrency bugs: race conditions, shared state without synchronization, non-atomic check-then-act
- Look for arithmetic bugs: integer overflow, division by zero, sign errors, lossy casts
- Reason about what the code is trying to do, then check whether it actually does that
- ONLY report actual bugs or very likely bugs — not style, not naming, not "could be cleaner"
- Do NOT report speculative or theoretical concerns. A bug report must identify a concrete scenario where the code produces a wrong result, crashes, or corrupts state. "Another process might modify the file between check and use" is not a bug unless the code's own contract requires atomicity. "What if the array is empty" is not a bug if the caller guarantees non-empty input. "This could overflow" is not a bug unless the value range actually reaches overflow in practice.
- If unsure whether something is a bug, explain the concern and the conditions under which it would fail — but only report it if those conditions are plausible given the surrounding code and API contract

## Wrong (real bug)
```
// Off-by-one: skips first IPv6 address when index is 0
if (nextIPv6AddressIndex > 0 && nextIPv4AddressIndex >= 0)
    parallelConnect = true;
```

## Correct
```
// Includes index 0
if (nextIPv6AddressIndex >= 0 && nextIPv4AddressIndex >= 0)
    parallelConnect = true;
```

## Wrong (false positive — do NOT report concerns like this)
```
// Reviewer flags: "The file could be deleted by another process between
// File.Exists and File.ReadAllText, causing a FileNotFoundException."
// This is speculative — the method's contract does not guarantee atomicity
// against external modifications, and the caller handles exceptions upstream.
if (File.Exists(path))
    return File.ReadAllText(path);
```
