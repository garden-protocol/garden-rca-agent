# Specialist Empowerment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Give the chain specialist iterative access to Loki + on-chain tools (in addition to repo tools) so it can follow up on hypotheses instead of consuming one-shot frozen summaries.

**Architecture:** Extend `log_agent.run()` return value with structured window/solver_id. Orchestrator threads those plus the on-chain agent into `specialist.analyze()`. Specialist concatenates repo + Loki + on-chain tool defs and dispatches each tool call to the right executor by name. Turn cap bumped to account for the new tool categories. Backwards compatible — new kwargs are all optional.

**Tech Stack:** Python 3.12, pytest, mock-based unit tests. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-04-16-specialist-empowerment-design.md`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `agents/log_agent.py` | Modify | Capture window_start/window_end/solver_id as locals, include in return dict |
| `agents/orchestrator.py` | Modify | Pass new kwargs into `specialist.analyze()` |
| `agents/specialists/base.py` | Modify | Extend `analyze()` signature, build combined tool defs, implement dispatcher, update user-message guidance, bump turn cap |
| `tests/specialist/__init__.py` | Create | Package marker |
| `tests/specialist/test_tool_dispatch.py` | Create | Offline unit tests for tool-defs assembly and dispatcher routing |

---

## Task 1 — Log agent exposes window + solver_id

**Files:**
- Modify: `agents/log_agent.py`

- [ ] **Step 1: Add a failing test**

Append to `tests/log_agent/test_time_window.py`:

```python
def test_run_returns_window_and_solver_id():
    """log_agent.run() return dict must expose window_start, window_end, solver_id."""
    created = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
    deadline = int((created + timedelta(hours=2)).timestamp())
    alert = _make_alert(created, deadline)
    alert = alert.model_copy(update={"metadata": {**alert.metadata, "solver_id": "s-xyz"}})

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

    with patch.object(log_agent, "get_provider", return_value=fake_provider):
        result = log_agent.run(alert)

    assert result["window_start"] == "2026-04-10T11:55:00+00:00"
    # window_end capped at fake-now in this harness, which is real now → just existence check
    assert isinstance(result["window_end"], str) and result["window_end"]
    assert result["solver_id"] == "s-xyz"
```

Run: `.venv/bin/python -m pytest tests/log_agent/test_time_window.py::test_run_returns_window_and_solver_id -v`
Expected: FAIL — `window_start` key missing from result.

- [ ] **Step 2: Modify `agents/log_agent.py` return dict**

Find the `return { ... }` block at the end of `log_agent.run()`. Add three fields:

```python
    return {
        "summary": summary,
        "key_evidence": key_evidence,
        "raw_lines": all_log_lines[:500],
        "window_start": window_start,
        "window_end": window_end,
        "solver_id": solver_id,
        "usage": { ... },
    }
```

(The three variables `window_start`, `window_end`, `solver_id` already exist earlier in the function — just include them in the dict.)

- [ ] **Step 3: Run test**

Run: `.venv/bin/python -m pytest tests/log_agent/test_time_window.py -v`
Expected: all 5 tests pass.

- [ ] **Step 4: Commit**

```bash
git add agents/log_agent.py tests/log_agent/test_time_window.py
git commit -m "feat(log_agent): expose window_start, window_end, solver_id in return dict"
```

---

## Task 2 — Specialist accepts new kwargs and includes tool categories conditionally

**Files:**
- Modify: `agents/specialists/base.py`
- Create: `tests/specialist/__init__.py`
- Create: `tests/specialist/test_tool_dispatch.py`

- [ ] **Step 1: Create the package marker**

Create `tests/specialist/__init__.py` (empty).

- [ ] **Step 2: Write failing tests for tool-def assembly**

Create `tests/specialist/test_tool_dispatch.py`:

```python
"""
Offline unit tests for BaseSpecialist tool assembly + dispatching.

Tests stub the provider, do not touch filesystem repos, Loki, or RPCs.
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from models.alert import Alert
from datetime import datetime, timezone

from agents.specialists.evm import EVMSpecialist
from agents.onchain.evm import EVMOnChainAgent


def _alert() -> Alert:
    return Alert(
        order_id="test",
        alert_type="missed_init",
        chain="evm",
        service="executor",
        network="mainnet",
        message="test",
        timestamp=datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc),
        metadata={},
    )


def _stub_response(text="{}"):
    r = MagicMock()
    r.usage = MagicMock(
        input_tokens=0, output_tokens=0,
        cache_read_tokens=0, cache_creation_tokens=0,
    )
    r.stop_reason = "end_turn"
    r.tool_calls = []
    r.text = text
    return r


def _capture_tool_defs(spec, **kwargs):
    """Run analyze() with stubbed provider and return the 'tools' param captured."""
    captured = {}
    fake_provider = MagicMock()

    def capture(**kw):
        captured["tools"] = kw.get("tools")
        return _stub_response()

    fake_provider.create_message.side_effect = capture
    fake_provider.build_assistant_message.return_value = {"role": "assistant", "content": ""}
    fake_provider.build_tool_results_message.return_value = {"role": "user", "content": ""}

    with patch("agents.specialists.base.get_provider", return_value=fake_provider):
        # Force the "no repo on disk, no gitea configured" path so we isolate new tool logic
        with patch("agents.specialists.base.gitea_configured", return_value=False):
            with patch("os.path.isdir", return_value=False):
                spec.analyze(
                    alert=_alert(),
                    log_summary="summary",
                    onchain_findings={"findings": "ok"},
                    **kwargs,
                )
    return captured.get("tools") or []


def _names(tool_defs):
    return {t["name"] for t in tool_defs}


def test_specialist_no_loki_no_onchain_when_inputs_missing():
    """Without window or onchain agent, specialist gets no extra tools (just analyses from knowledge)."""
    tools = _capture_tool_defs(EVMSpecialist())
    # No repos on disk, no gitea, no window, no onchain → no tools
    assert tools == [] or tools is None


def test_specialist_includes_loki_when_window_provided():
    """Pass window → LOKI tools added."""
    tools = _capture_tool_defs(
        EVMSpecialist(),
        log_window_start="2026-04-10T11:55:00+00:00",
        log_window_end="2026-04-10T14:30:00+00:00",
        solver_id="s-1",
    )
    names = _names(tools)
    assert {"query_loki", "search_by_order_id", "search_by_service"} <= names


def test_specialist_includes_onchain_when_agent_provided():
    """Pass onchain_agent → that agent's tool defs added."""
    agent = EVMOnChainAgent()
    tools = _capture_tool_defs(
        EVMSpecialist(),
        onchain_agent=agent,
    )
    names = _names(tools)
    # EVM on-chain tools include these
    assert {"get_native_balance", "get_transaction", "get_htlc_order_state"} <= names


def test_specialist_includes_all_three_categories():
    """Window + onchain agent + repos → all three categories (repos stubbed off here)."""
    agent = EVMOnChainAgent()
    tools = _capture_tool_defs(
        EVMSpecialist(),
        log_window_start="2026-04-10T11:55:00+00:00",
        log_window_end="2026-04-10T14:30:00+00:00",
        solver_id="s-1",
        onchain_agent=agent,
    )
    names = _names(tools)
    assert "search_by_order_id" in names
    assert "get_native_balance" in names


def test_specialist_no_name_collision():
    """Combined tool defs must have unique names."""
    agent = EVMOnChainAgent()
    tools = _capture_tool_defs(
        EVMSpecialist(),
        log_window_start="2026-04-10T11:55:00+00:00",
        log_window_end="2026-04-10T14:30:00+00:00",
        onchain_agent=agent,
    )
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names)), f"tool name collision: {names}"
```

- [ ] **Step 3: Run failing tests**

Run: `.venv/bin/python -m pytest tests/specialist/ -v`
Expected: FAIL — `analyze()` does not accept the new kwargs.

- [ ] **Step 4: Extend `analyze()` signature and tool assembly**

In `agents/specialists/base.py`, modify the `analyze()` method:

**A.** Add the new kwargs to the signature:

```python
def analyze(
    self,
    alert: Alert,
    log_summary: str,
    onchain_findings: dict | None = None,
    log_window_start: str | None = None,
    log_window_end: str | None = None,
    solver_id: str = "",
    onchain_agent: "BaseOnChainAgent | None" = None,
) -> dict:
```

**B.** After the existing `if repos_on_disk: ... elif gitea_configured(): ... else: ...` block that sets `tool_defs` and `tool_executor`, extend both:

Replace the current block (~lines 206-224):

```python
        # Determine tool source: local filesystem > Gitea API > knowledge-only
        from config import settings as _cfg
        import os as _os
        repos_on_disk = any(
            _os.path.isdir(p) for p in _cfg.repo_paths(chain).values()
        )

        if repos_on_disk:
            repo_tool_defs = build_repo_tool_definitions(chain)
            repo_executor = lambda name, inp: execute_repo_tool(chain, name, inp)
            max_turns = 35  # bumped from 25 for extra tool categories
        elif gitea_configured():
            repo_tool_defs = build_gitea_tool_definitions(chain)
            repo_executor = lambda name, inp: execute_gitea_tool(chain, name, inp)
            max_turns = 20  # bumped from 15
        else:
            repo_tool_defs = []
            repo_executor = None
            max_turns = 0

        # Loki tools: included only when window is provided
        loki_enabled = bool(log_window_start and log_window_end)
        loki_tool_defs = list(LOKI_TOOL_DEFINITIONS) if loki_enabled else []

        # On-chain tools: included only when an agent is provided
        onchain_tool_defs = list(onchain_agent.tool_definitions) if onchain_agent else []
        onchain_tool_names = {t["name"] for t in onchain_tool_defs}

        tool_defs = (repo_tool_defs or []) + loki_tool_defs + onchain_tool_defs
        if not tool_defs:
            tool_defs = None

        LOKI_TOOL_NAMES = {"query_loki", "search_by_order_id", "search_by_service"}

        def tool_executor(name, inp):
            if name in LOKI_TOOL_NAMES:
                return execute_loki_tool(name, inp)
            if name in onchain_tool_names and onchain_agent is not None:
                return onchain_agent.execute_tool(name, inp)
            if repo_executor is not None:
                return repo_executor(name, inp)
            return f"[no executor available for tool: {name}]"

        # If no turn budget was set (knowledge-only path), but we do have loki/onchain
        # tools, allow some turns for them.
        if max_turns == 0 and tool_defs:
            max_turns = 15
```

**C.** Add required imports at the top of the file (near existing imports):

```python
from tools.loki import LOKI_TOOL_DEFINITIONS, execute_loki_tool
```

And keep the existing imports.

- [ ] **Step 5: Run the test file**

Run: `.venv/bin/python -m pytest tests/specialist/ -v`
Expected: all 5 tests pass.

- [ ] **Step 6: Commit**

```bash
git add agents/specialists/base.py tests/specialist/__init__.py tests/specialist/test_tool_dispatch.py
git commit -m "feat(specialist): accept log window + onchain agent and build combined tool set"
```

---

## Task 3 — Orchestrator threads window + onchain_agent into specialist call

**Files:**
- Modify: `agents/orchestrator.py`

- [ ] **Step 1: Update the specialist call site in `run()`**

Locate in `agents/orchestrator.py` the block that calls `specialist.analyze(...)`. Replace with:

```python
    try:
        specialist_result = specialist.analyze(
            alert=alert,
            log_summary=log_result["summary"],
            onchain_findings=onchain_result,
            log_window_start=log_result.get("window_start"),
            log_window_end=log_result.get("window_end"),
            solver_id=log_result.get("solver_id", ""),
            onchain_agent=onchain_agent,
        )
    except Exception as exc:
        specialist_result["root_cause"] = f"[Specialist failed: {exc}]"
        specialist_result["raw_analysis"] = (
            f"## Log Summary\n\n{log_result['summary']}\n\n"
            f"## Specialist Error\n\n{exc}"
        )
```

- [ ] **Step 2: Sanity compile-check**

Run: `.venv/bin/python -c "import agents.orchestrator; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add agents/orchestrator.py
git commit -m "feat(orchestrator): thread log window + onchain agent into specialist"
```

---

## Task 4 — Specialist user-message guidance for new tools

**Files:**
- Modify: `agents/specialists/base.py` — the `user_message` string construction around the "Your Role" and "Investigation Protocol" sections

- [ ] **Step 1: Insert a "Tools Available" section before "Investigation Protocol"**

In `agents/specialists/base.py` `analyze()`, find the f-string `user_message = (...)` that builds the user message. Add a new section describing the tools, parameterised on whether each is available:

After the "Your Role: You Are the Investigator" section and before "Investigation Protocol", splice in:

```python
        tool_hint_lines = []
        tool_hint_lines.append(
            "1. **Repo tools** (`read_file`, `grep_repo`, `list_directory`) — inspect source code."
        )
        if log_window_start and log_window_end:
            solver_line = (
                f" For `executor` / `solver-engine` / `solver-comms` services, pass solver_id=\"{solver_id}\"."
                if solver_id else ""
            )
            tool_hint_lines.append(
                "2. **Log tools** (`search_by_order_id`, `search_by_service`, `query_loki`) — targeted "
                "follow-up Loki queries. "
                f"Always pass start_iso=\"{log_window_start}\" and end_iso=\"{log_window_end}\" on these calls."
                f"{solver_line} "
                "Do NOT re-run bulk retrieval — the first-pass summary is already in your context."
            )
        if onchain_agent is not None:
            onchain_tool_list = ", ".join(f"`{t['name']}`" for t in onchain_agent.tool_definitions)
            tool_hint_lines.append(
                f"3. **On-chain tools** ({onchain_tool_list}) — verify live chain state directly "
                "(balances, tx status, HTLC state). Use when a hypothesis depends on on-chain fact "
                "not already confirmed in the first-pass on-chain findings."
            )
        tool_hint_block = (
            "## Tools Available\n\n" + "\n\n".join(tool_hint_lines) + "\n\n"
            if tool_hint_lines else ""
        )
```

Then insert `{tool_hint_block}` into the `user_message` f-string immediately before `## Investigation Protocol`:

```python
        user_message = (
            f"## Alert\n\n{alert_block}\n\n"
            f"## Log Intelligence Report\n\n{log_summary}"
            f"{onchain_block}\n\n"
            f"---\n\n"
            f"## Your Role: You Are the Investigator\n\n"
            f"You have full access to source code (via tools), log analysis (above), "
            f"on-chain findings (above), and deep knowledge of the codebase (in your system prompt). "
            f"YOUR job is to investigate, trace code paths, and explain what happened. "
            f"**Never tell the operator to inspect code, check logs, or verify on-chain state — "
            f"that is YOUR job and you have already done it (or can do it now with your tools).**\n\n"
            f"{tool_hint_block}"
            f"## Investigation Protocol\n\n"
            ...  # rest unchanged
        )
```

- [ ] **Step 2: Sanity compile**

Run: `.venv/bin/python -c "import agents.specialists.base; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Run specialist tests to make sure existing tests still pass**

Run: `.venv/bin/python -m pytest tests/specialist/ -v`
Expected: 5/5 pass.

- [ ] **Step 4: Commit**

```bash
git add agents/specialists/base.py
git commit -m "feat(specialist): add tools-available section to user message"
```

---

## Task 5 — Full-suite regression check

- [ ] **Step 1: Run every unit test**

Run: `.venv/bin/python -m pytest tests/ -v --ignore=tests/loki/test_loki_integration.py --ignore=tests/loki/test_dest_executor_logs.py --ignore=tests/onchain`
Expected: all unit tests pass. (Integration tests that hit live services are excluded.)

- [ ] **Step 2: Compile every file we changed**

Run: `.venv/bin/python -m py_compile agents/log_agent.py agents/orchestrator.py agents/specialists/base.py`
Expected: no errors.

- [ ] **Step 3: No commit unless regression**

---

## Self-Review

**Spec coverage:**
- Spec §Change 1 (log_agent return) → Task 1 ✓
- Spec §Change 2 (orchestrator threading) → Task 3 ✓
- Spec §Change 3 (specialist combined tools + dispatcher) → Tasks 2, 4 ✓
- Spec §Change 4 (graceful degradation) → tested in Task 2 (no kwargs → no Loki/onchain tools) ✓

**Placeholder scan:** No TBDs. Every step has exact code or exact command.

**Type consistency:** `log_window_start`/`log_window_end`/`solver_id` spelled consistently. `LOKI_TOOL_NAMES` defined once, used once. `execute_loki_tool` / `execute_repo_tool` / `onchain_agent.execute_tool` signatures all take `(name, input_dict)` and return `str` — consistent.
