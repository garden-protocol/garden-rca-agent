# Log Intelligence Upgrade (Batch A)

**Date:** 2026-04-16
**Status:** Approved, pending implementation plan
**Related review:** In-session RCA actionability review identifying 10 gaps; this spec addresses gaps #2, #4, #9.

---

## Background

The Log Intelligence Agent (`agents/log_agent.py` + `tools/loki.py`) is the first step of every RCA run. Three defects in its current behaviour silently degrade every downstream report:

1. **No routing for solver-layer services.** `search_by_service` only knows about chain-specific `executor`, `watcher`, `relayer`. It cannot query `solver-engine` (where NoOp decisions are made), `solver-comms` (liquidity snapshots), or `orderbook` (order lifecycle). Since most `missed_init` root causes live in solver-engine logs, the agent is blind to the most common failure class.

2. **Log query window is truncated.** `log_agent.py:89-91` hard-codes `[order_created_at, order_created_at + 1 hour]`. Refund-related failures fire near the timelock deadline, often 2-6 hours after creation. Those events fall outside the window and are never seen.

3. **`level_filter` is a substring match, not a level filter.** `tools/loki.py:268-270` appends `|= \`{level_filter}\``, so `level_filter="error"` matches `"no error"`, `error_count=0`, etc. The agent receives noise when it asks for errors.

## Goal

Give the log agent access to the logs it needs, over the right time window, with an honest error-level filter — without altering the downstream report shape or specialist logic.

## Non-goals

- Giving the specialist its own Loki tools (batch B).
- Adding `timeline` / `next_action` / `links` fields to `RCAReport` (batch C).
- Loki query caching or rate limiting.
- Changing the Loki auth model or adding new Loki instances.

---

## Design

### Change 1 — Service routing expansion

Add two new maps to `tools/loki.py` for services that exist but are **not scoped to a single chain**:

```python
# Services on solver Loki that are NOT chain-scoped. Filtered by solver_id.
_SOLVER_SHARED_SERVICES: dict[str, str] = {
    "solver-engine": "solver-engine",
    "solver-comms":  "solver-comms",
}

# Services on primary Loki that are NOT chain-scoped.
_PRIMARY_SHARED_SERVICES: dict[str, str] = {
    "orderbook": "/orderbook-mainnet",   # also contains quote service logs
}
```

Update `search_by_service()` with the following routing precedence:

1. If `service in _SOLVER_SHARED_SERVICES` → query solver Loki, label selector `{service_name="<container>"}` plus `solver_id="..."` when provided. `chain` and `network` args are accepted but ignored.
2. Else if `service in _PRIMARY_SHARED_SERVICES` → query primary Loki, label selector `{service_name="<container>"}`. `chain`, `network`, and `solver_id` are ignored.
3. Else if `service == "executor"` → existing solver Loki path (unchanged).
4. Else → existing primary Loki `(service, chain)` path (unchanged).

Update the `service` enum in `LOKI_TOOL_DEFINITIONS` for the `search_by_service` tool to include `"solver-engine"`, `"solver-comms"`, `"orderbook"` in addition to the current `["executor", "watcher", "relayer"]`. Keep `chain` and `network` as required params for backwards compatibility; document that they're ignored for shared services.

Extend the `SYSTEM_PROMPT` in `agents/log_agent.py` with a short routing guide:

> **Service routing hints:**
> - `solver-engine` → use for NoOp decisions, order mapping (Initiate / Redeem / Refund / NoOp), order lock/unlock events, solver-engine status watcher events.
> - `solver-comms` → use for liquidity snapshots, committed-funds lag, aggregator sync failures.
> - `orderbook` → use for order creation/status transitions, quote-service events (quote logs live in orderbook).

### Change 2 — Time window widening

Replace the window computation in `agents/log_agent.py:80-91`:

```python
order_created_at_str = (alert.metadata or {}).get("order_created_at")
if order_created_at_str:
    order_created_at = datetime.fromisoformat(order_created_at_str.replace("Z", "+00:00"))
else:
    order_created_at = alert.timestamp

now = datetime.now(timezone.utc)
window_start = (order_created_at - timedelta(minutes=5)).isoformat()

deadline_unix = (alert.metadata or {}).get("deadline")
if deadline_unix:
    window_end_dt = datetime.fromtimestamp(deadline_unix, tz=timezone.utc) + timedelta(minutes=30)
else:
    window_end_dt = order_created_at + timedelta(hours=4)
window_end = min(window_end_dt, now).isoformat()
```

Rationale:
- `-5 min` start catches orderbook pre-order validation and early solver-engine cycles.
- `deadline + 30 min` captures timelock-fire events, refund submissions, and watcher lag around the deadline.
- `+4h` fallback for orders without a deadline (rare; should not occur in practice but avoids breaking).
- Capped at `now` so queries don't request future ranges from Loki.

No cap on window length — Loki handles multi-hour ranges fine, and this dataset is bounded by order lifetime.

`_build_alert_from_order` in `agents/orchestrator.py:541` already writes `deadline` into `alert.metadata`, so no orchestrator change is needed.

Update the user-message text in `log_agent.py:111-127` to describe the window as "order lifetime window" instead of "order_created_at to created_at + 1 hour".

### Change 3 — LogQL level filter fix

Replace the substring match in `tools/loki.py:268-270` (and the executor path at `:256`):

```python
if level_filter:
    logql += (
        f' |~ `(?i)(?:"?level"?\\s*[:=]\\s*"?{level_filter}\\b'
        f'|\\b{level_filter}\\b(?=[:\\]\\s]))`'
    )
```

This regex matches:
- JSON logs: `"level":"error"`
- Logfmt: `level=error`, `level = error`
- Bracketed levels: `[ERROR]`, `ERROR:`
- Case-insensitive: matches `error`, `Error`, `ERROR`.
- Does **not** match `"no error"` (no preceding `level` token, no bracket/colon delimiter).

Update the `level_filter` description in `LOKI_TOOL_DEFINITIONS` for `search_by_service`:

> Log level to filter on — one of `error`, `warn`, `info`, `debug`. Matches JSON, logfmt, and bracketed level tokens. For freeform keyword filtering, use `query_loki` with a full LogQL query instead.

The parameter name stays `level_filter` (renaming would break any saved prompts / sample calls).

---

## File change summary

| File | Change |
|---|---|
| `tools/loki.py` | Add `_SOLVER_SHARED_SERVICES` and `_PRIMARY_SHARED_SERVICES` maps. Extend routing in `search_by_service`. Add new service values to `search_by_service` enum in `LOKI_TOOL_DEFINITIONS`. Fix `level_filter` regex (apply to both executor path and primary path in `search_by_service`). |
| `agents/log_agent.py` | Replace time-window computation. Update user-message text. Add service-routing guide to `SYSTEM_PROMPT`. |
| `agents/orchestrator.py` | No change. |
| `tests/loki/` | Add cases per Testing section below. |

## Testing

Unit tests (in `tests/loki/`):

1. `search_by_service(service="solver-engine", chain="evm", network="mainnet", solver_id="s-123")` → LogQL `{service_name="solver-engine", solver_id="s-123"}`, routed to solver Loki URL.
2. `search_by_service(service="solver-engine", chain="evm", network="mainnet")` (no solver_id) → LogQL `{service_name="solver-engine"}`, routed to solver Loki.
3. `search_by_service(service="orderbook", chain="evm", network="mainnet")` → LogQL `{service_name="/orderbook-mainnet"}`, routed to primary Loki.
4. `search_by_service(service="executor", chain="solana", solver_id="s-123")` (regression) → unchanged behaviour.
5. `search_by_service(service="watcher", chain="evm")` (regression) → unchanged behaviour.
6. Level-filter regex:
   - Matches: `"level":"error"`, `level=error`, `[ERROR]`, `ERROR: foo`.
   - Does not match: `no error occurred`, `error_count=0`, `had error-free run`.

Integration test after merge: run `/investigate` on a known stuck/refunded order and confirm the log-summary section now contains solver-engine / orderbook lines where applicable.

## Rollout

Single PR, merged to `main`. No env var changes, no feature flag. Backwards compatible: existing callers of `search_by_service` with `(executor|watcher|relayer, chain, network)` are unaffected.

## Risks

- **Wrong label value for `/orderbook-mainnet`.** Mitigation: the constant sits in one place; if the label is different (no leading slash, different suffix), one-line fix.
- **LogQL regex false-positives.** Mitigation: covered in unit tests above. If a specific service uses an unusual log format, escalate to a service-specific filter in a follow-up.
- **Time window blowup.** A 24-hour window still returns at most `limit=300` lines per query. Performance is unchanged from today.
