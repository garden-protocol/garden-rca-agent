# Log Intelligence Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the log intelligence subsystem so the RCA agent sees the right logs (solver-engine, solver-comms, orderbook), over the right time window (order creation to deadline), with an honest error-level filter.

**Architecture:** Three surgical changes in `tools/loki.py` and `agents/log_agent.py`. Two new label-map dicts (`_SOLVER_SHARED_SERVICES`, `_PRIMARY_SHARED_SERVICES`) give `search_by_service` a routing table for non-chain-scoped services. The time window in the log agent is re-anchored to the order deadline instead of a fixed +1h. The `level_filter` substring match is replaced with a case-insensitive regex covering JSON / logfmt / bracketed levels. No schema or downstream-pipeline changes.

**Tech Stack:** Python 3.12, pytest, httpx, pydantic. Tests live under `tests/loki/` and are run with `.venv/bin/python -m pytest`.

**Spec:** `docs/superpowers/specs/2026-04-16-log-intelligence-upgrade-design.md`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `tools/loki.py` | Modify | Add shared-service maps, extend `search_by_service` routing, add new enum values to `LOKI_TOOL_DEFINITIONS`, replace `level_filter` substring with regex |
| `agents/log_agent.py` | Modify | Replace time-window math with deadline-anchored window; extend `SYSTEM_PROMPT` with routing hints; update user-message text |
| `tests/loki/test_loki_routing.py` | Create | Unit tests for `search_by_service` routing, level-filter regex — all fully mocked, no live Loki |
| `tests/log_agent/__init__.py` | Create | Empty package marker for log-agent test dir |
| `tests/log_agent/test_time_window.py` | Create | Unit tests for the time-window computation in `log_agent.run` — mocked provider |

No changes to `agents/orchestrator.py`; `deadline` is already written into `alert.metadata` by `_build_alert_from_order`.

---

## Task 1 — Scaffolding: unit test file for routing (fail-first)

**Files:**
- Create: `tests/loki/test_loki_routing.py`

- [ ] **Step 1: Create the test file with one failing placeholder test that imports the maps we haven't added yet**

```python
# tests/loki/test_loki_routing.py
"""
Unit tests for tools/loki.py routing logic.

These tests MOCK the underlying HTTP layer (`_query`) so they run offline
and do not require a configured Loki instance. They assert:
  - Correct Loki instance (primary vs solver) is selected per service
  - Correct LogQL label selector is built
  - solver_id filter is applied only where appropriate
  - level_filter regex matches real level tokens but not arbitrary substrings
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tools import loki as loki_mod
from tools.loki import (
    _PRIMARY_SHARED_SERVICES,
    _SOLVER_SHARED_SERVICES,
    search_by_service,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _call_and_capture(service, chain="evm", network="mainnet", **kwargs):
    """Call search_by_service with _query stubbed; return the (url, logql) captured."""
    captured = {}

    def fake_query(base_url, headers, logql, start, end, limit):
        captured["base_url"] = base_url
        captured["logql"] = logql
        captured["limit"] = limit
        return []

    with patch.object(loki_mod, "_query", side_effect=fake_query):
        search_by_service(service, chain, network, **kwargs)

    return captured


# ── Sentinel test: maps must exist ──────────────────────────────────────────

def test_shared_service_maps_exist():
    """Smoke test: the two new maps must be importable with expected keys."""
    assert "solver-engine" in _SOLVER_SHARED_SERVICES
    assert "solver-comms" in _SOLVER_SHARED_SERVICES
    assert "orderbook" in _PRIMARY_SHARED_SERVICES
```

- [ ] **Step 2: Run the test — expect ImportError because maps do not exist yet**

Run: `.venv/bin/python -m pytest tests/loki/test_loki_routing.py -v`
Expected: `ImportError: cannot import name '_PRIMARY_SHARED_SERVICES' from 'tools.loki'`

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/loki/test_loki_routing.py
git commit -m "test: failing unit test scaffolding for loki routing"
```

---

## Task 2 — Add the shared-service maps

**Files:**
- Modify: `tools/loki.py` (after `_SOLVER_SERVICE_MAP` definition, around line 152)

- [ ] **Step 1: Add both maps immediately after `_SOLVER_SERVICE_MAP`**

```python
# Services on solver Loki that are NOT chain-scoped. Filtered by solver_id
# when one is provided. `chain` and `network` args are accepted for API
# symmetry but ignored for these services.
_SOLVER_SHARED_SERVICES: dict[str, str] = {
    "solver-engine": "solver-engine",
    "solver-comms":  "solver-comms",
}

# Services on primary Loki that are NOT chain-scoped. `chain`, `network`,
# and `solver_id` args are ignored for these services.
_PRIMARY_SHARED_SERVICES: dict[str, str] = {
    "orderbook": "/orderbook-mainnet",  # also contains quote service logs
}
```

- [ ] **Step 2: Run the scaffolding test to confirm import resolves**

Run: `.venv/bin/python -m pytest tests/loki/test_loki_routing.py::test_shared_service_maps_exist -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tools/loki.py
git commit -m "feat(loki): add shared-service maps for solver-engine, solver-comms, orderbook"
```

---

## Task 3 — Route solver-engine / solver-comms to solver Loki

**Files:**
- Modify: `tools/loki.py` `search_by_service` body (~line 235-270)
- Modify: `tests/loki/test_loki_routing.py`

- [ ] **Step 1: Add failing tests for solver Loki routing**

Append to `tests/loki/test_loki_routing.py`:

```python
# ── Solver-shared service routing ────────────────────────────────────────────

def test_solver_engine_routes_to_solver_loki_with_solver_id():
    """solver-engine + solver_id → solver Loki, LogQL filters by both labels."""
    with patch.object(loki_mod, "_solver_url", return_value="http://solver.loki"):
        with patch.object(loki_mod, "_primary_url", return_value="http://primary.loki"):
            captured = _call_and_capture(
                "solver-engine",
                chain="evm",
                network="mainnet",
                solver_id="s-123",
            )
    assert captured["base_url"] == "http://solver.loki"
    assert 'service_name="solver-engine"' in captured["logql"]
    assert 'solver_id="s-123"' in captured["logql"]


def test_solver_engine_routes_to_solver_loki_without_solver_id():
    """solver-engine with no solver_id → still routes to solver Loki."""
    with patch.object(loki_mod, "_solver_url", return_value="http://solver.loki"):
        with patch.object(loki_mod, "_primary_url", return_value="http://primary.loki"):
            captured = _call_and_capture(
                "solver-engine", chain="evm", network="mainnet",
            )
    assert captured["base_url"] == "http://solver.loki"
    assert 'service_name="solver-engine"' in captured["logql"]
    assert "solver_id" not in captured["logql"]


def test_solver_comms_routes_to_solver_loki():
    """solver-comms behaves the same as solver-engine."""
    with patch.object(loki_mod, "_solver_url", return_value="http://solver.loki"):
        with patch.object(loki_mod, "_primary_url", return_value="http://primary.loki"):
            captured = _call_and_capture(
                "solver-comms",
                chain="evm",
                network="mainnet",
                solver_id="s-999",
            )
    assert captured["base_url"] == "http://solver.loki"
    assert 'service_name="solver-comms"' in captured["logql"]
    assert 'solver_id="s-999"' in captured["logql"]
```

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/python -m pytest tests/loki/test_loki_routing.py -v -k solver_engine or solver_comms`
Expected: FAIL — current `search_by_service` has no branch for these services; falls through to primary Loki path and queries `{job="MAINNET_LOGS"} |= \`solver-engine\``.

- [ ] **Step 3: Add the solver-shared routing branch at the top of `search_by_service`**

In `tools/loki.py`, modify `search_by_service()`. Locate the block starting `if service == "executor":` (~line 243). Immediately **before** it, insert:

```python
    # ── Shared solver-Loki services (solver-engine, solver-comms) ────────
    if service in _SOLVER_SHARED_SERVICES:
        svc_name = _SOLVER_SHARED_SERVICES[service]
        labels: list[str] = [f'service_name="{svc_name}"']
        if solver_id:
            labels.append(f'solver_id="{solver_id}"')
        logql = "{" + ", ".join(labels) + "}"
        if level_filter:
            logql += _level_filter_logql(level_filter)
        return _query(_solver_url(), _solver_headers(), logql, start, end, limit=300)
```

**Note:** `_level_filter_logql` is introduced in Task 5. For now, keep the old substring form inline and replace it in Task 5:

```python
        if level_filter:
            logql += f' |= `{level_filter}`'
```

- [ ] **Step 4: Run the solver-routing tests — expect PASS**

Run: `.venv/bin/python -m pytest tests/loki/test_loki_routing.py -v -k "solver_engine or solver_comms"`
Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add tools/loki.py tests/loki/test_loki_routing.py
git commit -m "feat(loki): route solver-engine and solver-comms queries to solver Loki"
```

---

## Task 4 — Route orderbook to primary Loki

**Files:**
- Modify: `tools/loki.py` `search_by_service`
- Modify: `tests/loki/test_loki_routing.py`

- [ ] **Step 1: Add failing test for orderbook routing**

Append to `tests/loki/test_loki_routing.py`:

```python
# ── Primary-shared service routing ───────────────────────────────────────────

def test_orderbook_routes_to_primary_loki():
    """orderbook → primary Loki, uses configured service_name, ignores solver_id."""
    with patch.object(loki_mod, "_solver_url", return_value="http://solver.loki"):
        with patch.object(loki_mod, "_primary_url", return_value="http://primary.loki"):
            captured = _call_and_capture(
                "orderbook",
                chain="evm",
                network="mainnet",
                solver_id="s-123",  # should be ignored
            )
    assert captured["base_url"] == "http://primary.loki"
    assert 'service_name="/orderbook-mainnet"' in captured["logql"]
    assert "solver_id" not in captured["logql"]
```

- [ ] **Step 2: Run the failing test**

Run: `.venv/bin/python -m pytest tests/loki/test_loki_routing.py::test_orderbook_routes_to_primary_loki -v`
Expected: FAIL — orderbook currently falls through to the primary default `{job="MAINNET_LOGS"}`.

- [ ] **Step 3: Add the primary-shared routing branch**

In `tools/loki.py` `search_by_service`, immediately **after** the `_SOLVER_SHARED_SERVICES` branch from Task 3 and **before** `if service == "executor":`, insert:

```python
    # ── Shared primary-Loki services (orderbook, contains quote logs) ─────
    if service in _PRIMARY_SHARED_SERVICES:
        svc_name = _PRIMARY_SHARED_SERVICES[service]
        logql = f'{{service_name="{svc_name}"}}'
        if level_filter:
            logql += f' |= `{level_filter}`'  # replaced in Task 5
        return _query(_primary_url(), _primary_headers(), logql, start, end, limit=300)
```

- [ ] **Step 4: Run — expect PASS**

Run: `.venv/bin/python -m pytest tests/loki/test_loki_routing.py::test_orderbook_routes_to_primary_loki -v`
Expected: PASS.

- [ ] **Step 5: Run all existing loki tests to confirm no regression**

Run: `.venv/bin/python -m pytest tests/loki/test_loki_routing.py -v`
Expected: All tests pass (5 tests: 1 scaffolding + 3 solver-shared + 1 primary-shared).

- [ ] **Step 6: Commit**

```bash
git add tools/loki.py tests/loki/test_loki_routing.py
git commit -m "feat(loki): route orderbook queries to primary Loki"
```

---

## Task 5 — Replace `level_filter` substring with regex

**Files:**
- Modify: `tools/loki.py`
- Modify: `tests/loki/test_loki_routing.py`

- [ ] **Step 1: Add failing tests for the level-filter semantics**

Append to `tests/loki/test_loki_routing.py`:

```python
# ── Level filter regex ───────────────────────────────────────────────────────

from tools.loki import _level_filter_logql  # introduced in this task


def test_level_filter_produces_regex_filter():
    """level_filter should emit |~ (regex) LogQL, not |= (substring)."""
    frag = _level_filter_logql("error")
    assert frag.strip().startswith("|~"), f"expected regex filter, got: {frag!r}"
    assert "error" in frag


def test_level_filter_matches_json_logs():
    """Simulate the regex fragment against sample JSON log lines."""
    import re
    frag = _level_filter_logql("error")
    pattern = frag.split("`")[1]  # extract the regex between backticks
    compiled = re.compile(pattern)
    assert compiled.search('{"level":"error","msg":"oops"}')
    assert compiled.search('{"msg":"oops","level":"ERROR"}')  # case-insensitive


def test_level_filter_matches_logfmt():
    import re
    frag = _level_filter_logql("warn")
    pattern = frag.split("`")[1]
    compiled = re.compile(pattern)
    assert compiled.search("ts=2026-04-10 level=warn msg=backoff")
    assert compiled.search("level = warn msg=...")


def test_level_filter_matches_bracketed_level():
    import re
    frag = _level_filter_logql("error")
    pattern = frag.split("`")[1]
    compiled = re.compile(pattern)
    assert compiled.search("2026-04-10 [ERROR] connection refused")
    assert compiled.search("ERROR: something failed")


def test_level_filter_does_not_match_substring_error():
    """The whole point: substring 'error' inside words must NOT match."""
    import re
    frag = _level_filter_logql("error")
    pattern = frag.split("`")[1]
    compiled = re.compile(pattern)
    assert not compiled.search("no error occurred today")
    assert not compiled.search("error_count=0 completed ok")
    assert not compiled.search("had an error-free run")


def test_search_by_service_uses_regex_filter_for_level():
    """Integration: search_by_service with level_filter emits |~ in LogQL."""
    with patch.object(loki_mod, "_solver_url", return_value="http://solver.loki"):
        with patch.object(loki_mod, "_primary_url", return_value="http://primary.loki"):
            captured = _call_and_capture(
                "watcher",
                chain="evm",
                network="mainnet",
                level_filter="error",
            )
    assert "|~" in captured["logql"]
    assert "|= `error`" not in captured["logql"]
```

- [ ] **Step 2: Run the failing tests**

Run: `.venv/bin/python -m pytest tests/loki/test_loki_routing.py -v -k level_filter`
Expected: ImportError on `_level_filter_logql`.

- [ ] **Step 3: Add the `_level_filter_logql` helper in `tools/loki.py`**

Place it near the top of the file, under the existing `_to_ns` helper (~line 65):

```python
def _level_filter_logql(level: str) -> str:
    """
    Build a LogQL filter fragment that matches real level tokens across
    common log formats (JSON, logfmt, bracketed, bare prefix).

    Matches:
      "level":"error"         (JSON)
      level=error / level = error   (logfmt)
      [ERROR], ERROR:               (plain-text)
    Does NOT match substrings like "no error" or "error_count=0".
    Case-insensitive.
    """
    # (?i) — case insensitive
    # Alternative 1: "?level"?[:=]"? level \b   — JSON / logfmt
    # Alternative 2: bare level token followed by ':', ']', or whitespace
    regex = (
        f'(?i)(?:"?level"?\\s*[:=]\\s*"?{level}\\b'
        f'|\\b{level}\\b(?=[:\\]\\s]))'
    )
    return f' |~ `{regex}`'
```

- [ ] **Step 4: Update all three call sites inside `search_by_service` to use the helper**

Replace every occurrence of:

```python
        if level_filter:
            logql += f' |= `{level_filter}`'
```

with:

```python
        if level_filter:
            logql += _level_filter_logql(level_filter)
```

There are three such occurrences: in the `_SOLVER_SHARED_SERVICES` branch (from Task 3), the `_PRIMARY_SHARED_SERVICES` branch (from Task 4), and the existing `executor` / primary fall-through paths at roughly lines 255-270.

- [ ] **Step 5: Run the level-filter tests**

Run: `.venv/bin/python -m pytest tests/loki/test_loki_routing.py -v -k level_filter`
Expected: 6 tests pass (5 direct helper tests + 1 integration through `search_by_service`).

- [ ] **Step 6: Run the full routing test file to confirm no regression**

Run: `.venv/bin/python -m pytest tests/loki/test_loki_routing.py -v`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add tools/loki.py tests/loki/test_loki_routing.py
git commit -m "feat(loki): replace level_filter substring match with proper regex"
```

---

## Task 6 — Extend `LOKI_TOOL_DEFINITIONS` service enum and descriptions

**Files:**
- Modify: `tools/loki.py` (`LOKI_TOOL_DEFINITIONS`, ~line 345-396)

- [ ] **Step 1: Update the `service` enum for `search_by_service`**

Locate the `search_by_service` tool definition's `service` property (~line 354) and replace it:

```python
                "service": {
                    "type": "string",
                    "enum": [
                        "executor", "watcher", "relayer",
                        "solver-engine", "solver-comms", "orderbook",
                    ],
                    "description": (
                        "Service to query logs for. "
                        "Chain-scoped: executor, watcher, relayer (require matching chain/network). "
                        "Shared (chain arg ignored): solver-engine + solver-comms on solver Loki "
                        "(filtered by solver_id when provided); orderbook on primary Loki "
                        "(contains quote service logs)."
                    ),
                },
```

- [ ] **Step 2: Update the `level_filter` description (same tool)**

Locate the `level_filter` property (~line 383) and replace:

```python
                "level_filter": {
                    "type": "string",
                    "description": (
                        "Log level to filter on: 'error', 'warn', 'info', 'debug'. "
                        "Matches JSON ('level':'error'), logfmt (level=error), and bracketed ([ERROR]) "
                        "tokens. Case-insensitive. For freeform keyword filtering use `query_loki` "
                        "with a full LogQL query instead."
                    ),
                    "default": "",
                },
```

- [ ] **Step 3: Run all loki tests to confirm no regression**

Run: `.venv/bin/python -m pytest tests/loki/ -v -k "not integration and not dest_executor"`
Expected: all unit tests pass. Integration tests are skipped if Loki is not configured.

- [ ] **Step 4: Commit**

```bash
git add tools/loki.py
git commit -m "feat(loki): document new services and level_filter semantics in tool definitions"
```

---

## Task 7 — Deadline-anchored time window in `log_agent.py`

**Files:**
- Create: `tests/log_agent/__init__.py`
- Create: `tests/log_agent/test_time_window.py`
- Modify: `agents/log_agent.py`

- [ ] **Step 1: Create the package marker**

Create `tests/log_agent/__init__.py` (empty file).

- [ ] **Step 2: Write failing unit tests for the time window**

Create `tests/log_agent/test_time_window.py`:

```python
"""
Unit tests for the time-window computation in agents/log_agent.run().

Tests run without hitting the provider or Loki — we stub them both.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from models.alert import Alert
import agents.log_agent as log_agent


def _make_alert(created_at: datetime, deadline_unix: int | None) -> Alert:
    metadata = {"order_created_at": created_at.isoformat()}
    if deadline_unix is not None:
        metadata["deadline"] = deadline_unix
    return Alert(
        order_id="test-order",
        alert_type="missed_init",
        chain="evm",
        service="executor",
        network="mainnet",
        message="test",
        timestamp=created_at,
        metadata=metadata,
    )


def _run_capturing_user_message(alert: Alert) -> str:
    """Stub the provider; return the user message text passed to it."""
    captured = {}

    fake_response = MagicMock()
    fake_response.usage = MagicMock(
        input_tokens=0, output_tokens=0,
        cache_read_tokens=0, cache_creation_tokens=0,
    )
    fake_response.stop_reason = "end_turn"
    fake_response.tool_calls = []
    fake_response.text = "stubbed"

    fake_provider = MagicMock()
    fake_provider.create_message.return_value = fake_response

    def capture_call(*args, **kwargs):
        # Grab the user message (last message at call time)
        messages = kwargs.get("messages") or (args[-1] if args else [])
        for m in messages:
            if m.get("role") == "user":
                captured["user_message"] = m["content"]
        return fake_response

    fake_provider.create_message.side_effect = capture_call

    with patch.object(log_agent, "get_provider", return_value=fake_provider):
        log_agent.run(alert)

    return captured["user_message"]


def test_window_starts_5min_before_created_at():
    created = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
    deadline = int((created + timedelta(hours=2)).timestamp())
    msg = _run_capturing_user_message(_make_alert(created, deadline))
    # Start should be 5 minutes before created_at
    assert "2026-04-10T11:55:00" in msg


def test_window_ends_30min_after_deadline_when_deadline_in_past():
    """If deadline is in the past, window_end = deadline + 30min (still capped by now)."""
    created = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
    # Deadline 2 hours after creation
    deadline_dt = created + timedelta(hours=2)
    deadline = int(deadline_dt.timestamp())

    fake_now = deadline_dt + timedelta(hours=1)  # well past deadline

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    with patch.object(log_agent, "datetime", FakeDateTime):
        msg = _run_capturing_user_message(_make_alert(created, deadline))

    # Window end is deadline + 30 min = 14:30:00
    assert "2026-04-10T14:30:00" in msg


def test_window_capped_at_now_when_deadline_is_future():
    """If deadline is in the future, window_end = now (not deadline + 30min)."""
    created = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
    future_deadline = int((created + timedelta(hours=10)).timestamp())

    fake_now = created + timedelta(hours=1)

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    with patch.object(log_agent, "datetime", FakeDateTime):
        msg = _run_capturing_user_message(_make_alert(created, future_deadline))

    assert "2026-04-10T13:00:00" in msg  # fake_now
    assert "2026-04-10T22" not in msg   # future deadline not used


def test_window_falls_back_to_4h_when_no_deadline():
    """No deadline in metadata → window_end = created_at + 4h (capped at now)."""
    created = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
    fake_now = created + timedelta(hours=8)  # far in future so cap doesn't apply

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    with patch.object(log_agent, "datetime", FakeDateTime):
        msg = _run_capturing_user_message(_make_alert(created, None))

    assert "2026-04-10T16:00:00" in msg  # created + 4h
```

- [ ] **Step 3: Run the failing tests**

Run: `.venv/bin/python -m pytest tests/log_agent/test_time_window.py -v`
Expected: all four tests fail — current behaviour uses `created_at + 1 hour`, so end times are `13:00:00` for all cases.

- [ ] **Step 4: Replace the window computation in `agents/log_agent.py`**

Locate the block at lines ~79-91 (from `order_created_at_str = ...` through `window_end = min(...).isoformat()`) and replace it with:

```python
    # Compute the log query time window:
    #   start = order_created_at - 5 minutes (catches pre-order orderbook validation)
    #   end   = min(deadline + 30 minutes, now)  — falls back to +4h if no deadline
    order_created_at_str = (alert.metadata or {}).get("order_created_at")
    if order_created_at_str:
        order_created_at = datetime.fromisoformat(
            order_created_at_str.replace("Z", "+00:00")
        )
    else:
        order_created_at = alert.timestamp

    now = datetime.now(timezone.utc)
    window_start = (order_created_at - timedelta(minutes=5)).isoformat()

    deadline_unix = (alert.metadata or {}).get("deadline")
    if deadline_unix:
        window_end_dt = datetime.fromtimestamp(
            deadline_unix, tz=timezone.utc
        ) + timedelta(minutes=30)
    else:
        window_end_dt = order_created_at + timedelta(hours=4)
    window_end = min(window_end_dt, now).isoformat()
```

Also update the user-message text (~line 114-117) so the description matches reality:

```python
        f"**IMPORTANT — Time window and solver_id for all queries:**\n"
        f"Use start_iso=\"{window_start}\" and end_iso=\"{window_end}\" "
        f"(order lifetime window: created_at -5min to min(deadline+30min, now)) "
        f"for ALL log queries. "
        f"Do NOT use minutes_back — always pass explicit start_iso/end_iso.\n"
```

- [ ] **Step 5: Run the time-window tests**

Run: `.venv/bin/python -m pytest tests/log_agent/test_time_window.py -v`
Expected: all four tests pass.

- [ ] **Step 6: Commit**

```bash
git add tests/log_agent/__init__.py tests/log_agent/test_time_window.py agents/log_agent.py
git commit -m "feat(log_agent): anchor query window to order deadline, widen by 5min/30min"
```

---

## Task 8 — Extend `log_agent` SYSTEM_PROMPT with routing hints

**Files:**
- Modify: `agents/log_agent.py` `SYSTEM_PROMPT` (line 13-66)

- [ ] **Step 1: Add the routing guide section to SYSTEM_PROMPT**

In `agents/log_agent.py`, locate the `SYSTEM_PROMPT` string. **Before** the `## Output Format` section, insert a new section:

```python
## Service Routing Guide

When calling search_by_service, choose the service based on what you want to find:

- `executor` — chain executor logs (evm-executor, solana-executor, etc.).
  Required: `chain`, `network`. Use when chasing an action after the solver-engine
  has mapped it to Initiate/Redeem/Refund.

- `watcher` — chain watcher logs (evm-watcher, solana-watcher, etc.).
  Required: `chain`, `network`. Use to see DB state transitions, event parsing
  failures, confirmation lag.

- `relayer` — chain relayer logs (evm-relay, solana-relayer, etc.).
  Required: `chain`, `network`. Use for user-facing initiate/redeem flow issues.

- `solver-engine` — SHARED, solver-scoped. Use for NoOp decisions, order
  mapping (Initiate / Redeem / Refund / NoOp), order lock/unlock events,
  status-watcher behaviour. Pass solver_id. `chain`/`network` are ignored.

- `solver-comms` — SHARED, solver-scoped. Use for liquidity snapshots,
  committed-funds lag, aggregator sync failures. Pass solver_id. `chain`/`network`
  are ignored.

- `orderbook` — SHARED, non-solver-scoped. Use for order creation / status
  transitions; contains quote service logs as well. `chain`/`network`/solver_id
  are ignored.

Rule of thumb for `missed_init`: start with solver-engine (NoOp? lock stuck?),
then orderbook (status valid?), then executor (action received and submitted?).
```

- [ ] **Step 2: Sanity check — confirm the file still parses**

Run: `.venv/bin/python -c "import agents.log_agent; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Run the full log_agent test suite**

Run: `.venv/bin/python -m pytest tests/log_agent/ -v`
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add agents/log_agent.py
git commit -m "feat(log_agent): add service routing guide to SYSTEM_PROMPT"
```

---

## Task 9 — Full-suite regression check

**Files:** none (verification only)

- [ ] **Step 1: Run every test in the project**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all unit tests pass. Loki integration tests and on-chain integration tests may skip depending on `.env`; that is fine. No failures.

- [ ] **Step 2: Confirm the server still starts**

Run: `.venv/bin/python -c "import main"` (or `.venv/bin/python -m py_compile main.py`)
Expected: no ImportError, no SyntaxError.

- [ ] **Step 3: No commit needed unless a regression was discovered**

If a regression appeared, fix it, add a regression test in the appropriate test file, and commit with message `fix: <what>`.

---

## Self-Review

**Spec coverage:**
- Spec §Change 1 (service routing) → Tasks 2, 3, 4, 6, 8 ✓
- Spec §Change 2 (time window) → Task 7 ✓
- Spec §Change 3 (level filter) → Task 5 ✓
- Spec §Testing (all six cases listed) → covered by tests in Tasks 3, 4, 5 ✓
- Spec §File change summary → matches the File Map above ✓

**Placeholder scan:** No TBDs, no "add error handling", every step either runs a command, writes tests, or shows exact code. Task 3 flags that the inline `|= \`{level_filter}\`` is a deliberate placeholder replaced in Task 5 — that is explicit, not vague.

**Type consistency:** `_level_filter_logql(level: str) -> str` defined once (Task 5) and imported/called consistently. `_SOLVER_SHARED_SERVICES` and `_PRIMARY_SHARED_SERVICES` keys match the enum values added in Task 6. `search_by_service` signature unchanged throughout.
