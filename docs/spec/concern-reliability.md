# Concern Agent Reliability

## Goals

1. Concern agents (bugs, security, architecture) produce findings even when reviewing large repos that previously caused 100% timeout failures
2. Partial results survive timeouts — no more all-or-nothing output
3. Incomplete reviews can be continued across multiple invocations without re-doing prior work
4. The orchestrator tracks per-(concern, model) completion status and only re-invokes what's needed
5. Python runner stays dumb — no parsing of agent output, no iteration logic

## Expected Behavior

### Agent Working Protocol

Each concern agent receives a `## Working Protocol` section in its prompt that instructs it to:

1. **Start a background timer** — `python -c "import time; time.sleep(600)"` as async non-detached shell task. The system notification on completion serves as the "time's up" signal.
2. **Plan work** — group related changed files, write plan to scratchpad (`scratchpad/{concern}--{model}--plan.md`)
3. **Review incrementally** — one file group at a time, writing each finding to the report file immediately
4. **React to timer** — when notification arrives, finish current finding, mark report as incomplete with remaining files, exit
5. **Finish normally** — if all files reviewed before timer, mark report as complete

### Report Status Sentinel

Reports use an explicit, unambiguous status marker at the top of the file:

- Complete: `Review Status: This review is complete.`
- Incomplete: `Review Status: This review is incomplete, please invoke the agent again to continue reviewing.`

### Continuation Protocol

The **same prompt** is used for all invocations. The agent detects it's a continuation by checking if its report and plan files already exist. If they do, it reads them and continues reviewing only the unchecked file groups from its plan.

### Orchestrator Continuation Loop

After the initial `run-concerns` completes (Phase 1), the orchestrator:

1. Uses `list-findings` to get file existence/size metadata (opaque — Python does not read content)
2. Reads each existing finding file itself and checks the first-line sentinel
3. Identifies which (concern, model) pairs are incomplete
4. Uses `build-continuation` to write a filtered dispatch with only those entries
5. Runs `python run-concerns --repo . --dispatch {continue_dispatch}`
6. Repeats up to 3 continuation rounds; stops early if all complete or timed out

### Timeout Architecture

| Parameter | Value | Purpose |
|-----------|-------|---------|
| Soft timeout | 600s (10 min) | Agent starts own timer; notification triggers graceful wrap-up |
| Hard timeout | 900s (15 min) | Python subprocess kill; safety net for stuck agents |
| Max continuation rounds | 3 | Orchestrator re-invokes up to 3 times for incomplete concerns |
| Retries | 0 | No blind retries — continuation replaces retry |

## Technical Approach

### Python Changes (`focused-review.py`)

1. **Simplify `_run_single_concern`** — remove retry loop. Single subprocess launch with hard timeout. Return process-level status only (exited/timed_out + file existence). No output parsing. Update timeout defaults: `CONCERN_HARD_TIMEOUT=900`, remove `CONCERN_RETRIES`. Also add scratchpad directory creation to `prepare-review` (`.agents/focused-review/scratchpad/`).
2. **Add `--dispatch {path}` arg** to `run-concerns` — reads alternate dispatch file for continuation rounds instead of default `concern-dispatch.json`.
3. **Add `list-findings` subcommand** — returns opaque file metadata (existence, size, path) per dispatch entry. Does NOT read file content.
4. **Add `build-continuation` subcommand** — accepts `--incomplete` pairs list, filters dispatch to matching entries, writes output file.

### Prompt Changes (`_generate_concern_prompts`)

Add `## Working Protocol` section to generated prompts with:
- Timer start instruction (soft timeout value injected)
- Plan + incremental report writing methodology
- Continuation detection (check if report/plan files exist)
- Status sentinel format
- File paths for report and plan (injected per concern × model)

### Concern Body Updates

Add `## Working Approach` section to each default concern (bugs.md, security.md, architecture.md) guiding diff-focused, incremental exploration strategy.

### SKILL.md Changes

Add continuation loop between Phase 1 and Phase 2:
- Run `list-findings` after `run-concerns` completes (gets opaque metadata)
- Read each finding file and check first-line sentinel for incomplete status
- Use `build-continuation` to write filtered dispatch for incomplete pairs
- Re-invoke `run-concerns` with continuation dispatch
- Repeat up to 3 rounds; stop early if all complete
- Proceed to Phase 2 when done

## Decisions

- **Agent owns its timer** — starts background sleep as first action; system notification is the signal. No Python threading.
- **Agent owns its work organization** — decides file groupings and review order. No programmatic chunking of concern scope.
- **Python stays dumb** — no parsing of reports, no progress detection, no iteration logic. Just subprocess management and opaque file metadata (existence, size).
- **Orchestrator drives continuation** — reads finding files, checks sentinel, writes filtered dispatch via `build-continuation`, re-invokes.
- **Same prompt for all iterations** — agent discovers continuation state by checking if its files exist.
- **No stuck detection** — agents either finish (complete sentinel), run out of time (incomplete sentinel or hard timeout), or error. Max 3 continuation rounds is the only safety cap.

## Key Files

- `skills/focused-review/scripts/focused-review.py` — Python runner (simplify `_run_single_concern`, add `--dispatch` arg)
- `skills/focused-review/scripts/focused-review.py:_generate_concern_prompts` — prompt template (add Working Protocol section)
- `skills/focused-review/defaults/concerns/bugs.md` — bugs concern body (add Working Approach)
- `skills/focused-review/defaults/concerns/security.md` — security concern body (add Working Approach)
- `skills/focused-review/defaults/concerns/architecture.md` — architecture concern body (add Working Approach)
- `skills/focused-review/SKILL.md` — orchestrator (add continuation loop between Phase 1 and Phase 2)
