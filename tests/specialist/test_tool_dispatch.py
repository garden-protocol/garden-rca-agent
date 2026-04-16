"""
Offline unit tests for BaseSpecialist tool assembly + dispatching.

Tests stub the provider, do not touch filesystem repos, Loki, or RPCs.
"""
import os
import sys
from unittest.mock import patch, MagicMock

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
    """Without window or onchain agent, specialist gets no extra tools."""
    tools = _capture_tool_defs(EVMSpecialist())
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
    assert {"get_native_balance", "get_transaction", "get_htlc_order_state"} <= names


def test_specialist_includes_all_three_categories():
    """Window + onchain agent → loki + onchain tools (repo stubbed off)."""
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


def test_tool_hint_block_omits_repo_line_in_knowledge_only_path(monkeypatch):
    """
    In the knowledge-only path (no repos, no Gitea, no window, no onchain), the
    user message must NOT claim the specialist has repo tools.
    """
    captured = {}
    fake_provider = MagicMock()

    def capture(**kw):
        for m in kw.get("messages") or []:
            if m.get("role") == "user":
                captured["user_message"] = m["content"]
        return _stub_response()

    fake_provider.create_message.side_effect = capture
    fake_provider.build_assistant_message.return_value = {"role": "assistant", "content": ""}
    fake_provider.build_tool_results_message.return_value = {"role": "user", "content": ""}

    with patch("agents.specialists.base.get_provider", return_value=fake_provider):
        with patch("agents.specialists.base.gitea_configured", return_value=False):
            with patch("os.path.isdir", return_value=False):
                EVMSpecialist().analyze(
                    alert=_alert(),
                    log_summary="summary",
                    onchain_findings={"findings": "ok"},
                )

    user_msg = captured.get("user_message", "")
    assert "read_file" not in user_msg, (
        "repo tool names must not appear in user message when no repo tools are available"
    )
    # Tools Available section should either be absent or only list available categories.
    # When nothing is available, there should be no Tools Available block at all.
    assert "## Tools Available" not in user_msg


def test_tool_hint_block_numbering_has_no_gaps():
    """
    With loki off, onchain on, and repo on, the rendered hint must read 1. ... 2. ...
    not 1. ... 3. ... (which is what the hardcoded numbering produced).
    """
    captured = {}
    fake_provider = MagicMock()

    def capture(**kw):
        for m in kw.get("messages") or []:
            if m.get("role") == "user":
                captured["user_message"] = m["content"]
        return _stub_response()

    fake_provider.create_message.side_effect = capture
    fake_provider.build_assistant_message.return_value = {"role": "assistant", "content": ""}
    fake_provider.build_tool_results_message.return_value = {"role": "user", "content": ""}

    agent = EVMOnChainAgent()
    with patch("agents.specialists.base.get_provider", return_value=fake_provider):
        with patch("agents.specialists.base.gitea_configured", return_value=False):
            # Force repo on via isdir=True and intercept build_repo_tool_definitions
            with patch("os.path.isdir", return_value=True):
                EVMSpecialist().analyze(
                    alert=_alert(),
                    log_summary="summary",
                    onchain_findings={"findings": "ok"},
                    onchain_agent=agent,  # loki off, onchain on
                )

    user_msg = captured.get("user_message", "")
    # Extract just the Tools Available block to avoid false positives from other numbered lists
    assert "## Tools Available" in user_msg
    tools_section_start = user_msg.index("## Tools Available")
    # Find the next ## heading after Tools Available
    rest = user_msg[tools_section_start + len("## Tools Available"):]
    next_section = rest.index("##") if "##" in rest else len(rest)
    tools_block = rest[:next_section]
    # Must start at 1 and have no "3." — only two categories (repo + onchain) are enabled
    assert "1." in tools_block
    assert "2." in tools_block
    assert "3." not in tools_block
