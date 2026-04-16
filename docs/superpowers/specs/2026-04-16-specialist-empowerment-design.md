# Specialist Empowerment (Batch B)

**Date:** 2026-04-16
**Status:** Approved (auto mode), pending implementation plan
**Related review:** Gap #1 — "Specialist can't follow up; it gets one-shot inputs."

---

## Background

After Batch A, the log agent retrieves richer logs over a better time window. But the specialist still consumes them as a frozen markdown string. It can only:
- Read repo code (filesystem or Gitea)
- Parse the pre-baked `log_summary` + `onchain_findings` blobs from orchestrator

It cannot:
- Follow up with targeted Loki queries once a hypothesis forms
- Verify on-chain state the on-chain agent didn't happen to check
- Cross-reference a log line against a specific service it hasn't seen yet

Result: reports that say "check X" instead of "I checked X and found Y."

## Goal

Give the chain specialist access to the log agent's Loki tools and the chain's on-chain tools, alongside its existing repo tools, so it can iterate during analysis. Keep the first-pass `log_agent` / on-chain agent untouched — they still produce the initial summary the specialist anchors on.

## Non-goals

- Removing the log agent or on-chain agent first-pass (they remain).
- Cross-chain tool access (gap #10; separate batch).
- Report-shape changes (Batch C).
- Per-tool cost budgeting — trust the model with a moderately higher turn cap.

---

## Design

### Change 1 — `log_agent.run()` returns structured window + solver_id

Currently `log_agent.run()` returns `{summary, key_evidence, raw_lines, usage}`. The window it computed is only embedded in the user message it sent to its own model — downstream callers have no access. Extend the return dict with:

```python
{
    "summary": ...,
    "key_evidence": ...,
    "raw_lines": ...,
    "usage": ...,
    "window_start": "2026-04-10T11:55:00+00:00",   # NEW
    "window_end":   "2026-04-10T14:30:00+00:00",   # NEW
    "solver_id":    "s-123",                       # NEW (may be "")
}
```

Minimal change: compute these values once (already happens), capture them into the return dict.

### Change 2 — Orchestrator threads window + solver_id + onchain_agent into specialist

In `agents/orchestrator.py:run()`, after the log_agent and on-chain agent runs, call `specialist.analyze(...)` with the extra context:

```python
specialist_result = specialist.analyze(
    alert=alert,
    log_summary=log_result["summary"],
    onchain_findings=onchain_result,
    log_window_start=log_result.get("window_start"),
    log_window_end=log_result.get("window_end"),
    solver_id=log_result.get("solver_id", ""),
    onchain_agent=onchain_agent,  # may be None if unsupported chain
)
```

### Change 3 — Specialist builds combined tool set + dispatcher

In `agents/specialists/base.py` `analyze()`:

1. **Signature extended** to accept `log_window_start`, `log_window_end`, `solver_id`, `onchain_agent` (all optional — backwards compatible).

2. **Combined tool defs** built by concatenating:
   - Existing repo tools (filesystem or Gitea) — already chain-specific.
   - `LOKI_TOOL_DEFINITIONS` from `tools.loki` — only when both window values are present (otherwise queries would misfire).
   - `onchain_agent.tool_definitions` — only when `onchain_agent` is provided.

3. **Single dispatcher function** routes tool calls by name:

```python
LOKI_TOOL_NAMES = {"query_loki", "search_by_order_id", "search_by_service"}

def dispatch(tool_name, tool_input):
    if tool_name in LOKI_TOOL_NAMES:
        return execute_loki_tool(tool_name, tool_input)
    if onchain_agent and tool_name in _onchain_names:
        return onchain_agent.execute_tool(tool_name, tool_input)
    return execute_repo_tool(chain, tool_name, tool_input)  # or gitea
```

Tool-name collisions: none. Repo (`read_file`, `grep_repo`, `list_directory`), Loki (`query_*`, `search_*`), on-chain (`get_transaction`, `get_native_balance`, `get_htlc_order_state`, etc. — chain-specific) are all distinct.

4. **User message augmented** to tell the specialist about the new tools:

```
You have three tool categories:

1. Repo tools (read_file, grep_repo, list_directory) — inspect source code.
2. Log tools (search_by_order_id, search_by_service, query_loki) — targeted follow-up log queries.
   When calling log tools, always pass start_iso="{window_start}" and end_iso="{window_end}".
   {if solver_id} For executor and solver-* services, pass solver_id="{solver_id}".
3. On-chain tools ({list}) — verify live chain state directly (balances, tx status, HTLC state).

Use log and on-chain tools to verify hypotheses the first-pass summary raised.
Do NOT re-do bulk retrieval — those summaries are already in your context.
```

5. **Turn cap bump**: 25 → 35 for local repo path, 15 → 20 for Gitea path, to account for the new tool categories. No model change.

### Change 4 — Graceful degradation

If `log_window_start` / `log_window_end` are `None` (e.g., legacy callers or fallback), Loki tools are *not* included in the combined set. If `onchain_agent` is `None`, on-chain tools are omitted. Specialist still works with just repo tools. No behaviour regression for any caller.

---

## File change summary

| File | Change |
|---|---|
| `agents/log_agent.py` | Return `window_start`, `window_end`, `solver_id` in the result dict. Declare them as locals up front so they are in scope at return. |
| `agents/orchestrator.py` | Forward window/solver_id/onchain_agent to `specialist.analyze()`. |
| `agents/specialists/base.py` | Extend `analyze()` signature. Build combined tool defs. Implement dispatcher. Update user-message guidance. Bump turn caps. |
| `tests/specialist/__init__.py` | New empty package marker. |
| `tests/specialist/test_tool_dispatch.py` | Unit test: combined tool defs contain all expected categories; dispatcher routes each tool name to the right executor. Fully mocked — no live provider / Loki / RPC. |

## Testing

- `log_agent.run()` return dict contains `window_start`, `window_end`, `solver_id` (non-crash test with stubbed provider).
- Specialist tool defs include repo + loki + onchain when all inputs provided.
- Specialist tool defs include only repo when loki/onchain inputs missing.
- Dispatcher routes `search_by_order_id` → `execute_loki_tool`, `get_native_balance` → `onchain_agent.execute_tool`, `read_file` → `execute_repo_tool`.
- Orchestrator regression: existing test that invokes `run()` still passes (if one exists; otherwise add a light smoke test).

## Rollout

Single PR on `feat/specialist-empowerment`, squash-merge to main. Backwards compatible: `specialist.analyze(alert, log_summary, onchain_findings)` without the new kwargs still works.

## Risks

- **Cost:** specialist now runs on Opus and can issue Loki/RPC calls. Worst case at 35 turns is higher than today's 25, but only when the model chooses to iterate. Mitigation: observe token usage in cost field; tune turn cap if it drifts.
- **Tool mis-use:** model may forget to thread `start_iso`/`end_iso` on Loki calls. Mitigation: user message gives explicit copy-paste values; tool failure returns `[No logs found]` — self-correcting.
- **Dispatcher collisions:** none today, but if a future on-chain tool adopts a repo-style name, the ordering above resolves to Loki → on-chain → repo. Add a unit test that would fail on collision.
