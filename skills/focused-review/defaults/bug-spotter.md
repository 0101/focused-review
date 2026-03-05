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
- Reason about what the code is trying to do, then check whether it actually does that
- Look for: wrong comparisons, off-by-one (`>` vs `>=`), wrong variable in a condition, dead code paths, early returns that block needed logic, race conditions, use-after-dispose, null dereference on error paths
- When you find a concrete bug — a scenario where the code produces wrong results, crashes, or corrupts state — report it. Do not stay silent on real bugs.
- Skip style, naming, documentation, and speculative concerns about external factors (e.g. "another process might delete the file"). Focus on bugs provable from the code itself.

## Wrong
```
// Off-by-one: skips index 0
if (nextIPv6AddressIndex > 0 && nextIPv4AddressIndex >= 0)
    parallelConnect = true;
```

```
// Dead code: parallelConnect is checked before indices are initialized,
// so the parallel path is unreachable
bool parallelConnect = idx6 >= 0 && idx4 >= 0;  // idx6 is still -1 here
if (parallelConnect) {
    idx6 = GetNextAddress(...);  // never reached
}
```

```
// Early return prevents comparing strategies: returns immediately
// without evaluating whether an alternative approach would be better
if (PrefixMatchWorks(data)) {
    Mode = PrefixMatch;
    return;  // blocks FixedDistanceSets from being considered
}
```

## Correct
```
// Includes index 0
if (nextIPv6AddressIndex >= 0 && nextIPv4AddressIndex >= 0)
    parallelConnect = true;
```

```
// Initialize indices before checking
idx6 = GetNextAddress(...);
idx4 = GetNextAddress(...);
bool parallelConnect = idx6 >= 0 && idx4 >= 0;
```

```
// Evaluate both strategies before choosing
var prefixResult = EvaluatePrefix(data);
var fixedResult = EvaluateFixedDistance(data);
Mode = prefixResult.IsBetter(fixedResult) ? PrefixMatch : FixedDistance;
```
