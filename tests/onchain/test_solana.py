"""
Unit tests for SolanaOnChainAgent.execute_tool()
Mocks _rpc — no live Solana node required.
"""
import json
import pytest
from unittest.mock import patch

from agents.onchain.solana import SolanaOnChainAgent

agent = SolanaOnChainAgent()
RPC = "agents.onchain.solana._rpc"

PUBKEY = "11111111111111111111111111111112"   # system program — valid base58 address


# ── get_account ───────────────────────────────────────────────────────────────

class TestGetAccount:

    def test_existing_account_with_lamports(self):
        mock_result = {"value": {"lamports": 5_000_000_000, "owner": "11111111111111111111111111111112", "data": ["", "base64"], "executable": False}}
        with patch(RPC, return_value=mock_result):
            result = agent.execute_tool("get_account", {"address": PUBKEY})
        data = json.loads(result)
        assert data["value"]["lamports"] == 5_000_000_000

    def test_null_account_means_htlc_settled(self):
        with patch(RPC, return_value={"value": None}):
            result = agent.execute_tool("get_account", {"address": PUBKEY})
        data = json.loads(result)
        assert data["value"] is None

    def test_rpc_error(self):
        with patch(RPC, return_value={"error": "account not found"}):
            result = agent.execute_tool("get_account", {"address": PUBKEY})
        assert "error" in result


# ── get_transaction ───────────────────────────────────────────────────────────

class TestGetTransaction:

    def test_successful_transaction(self):
        mock_tx = {
            "slot": 250000000,
            "blockTime": 1700000000,
            "meta": {
                "err": None,
                "fee": 5000,
                "logMessages": ["Program log: Redeem", "Program log: success"],
            },
        }
        with patch(RPC, return_value=mock_tx):
            result = agent.execute_tool("get_transaction", {"signature": "abc123"})
        data = json.loads(result)
        assert data["err"] is None
        assert data["slot"] == 250000000
        assert "Program log: Redeem" in data["logMessages"]

    def test_failed_transaction_has_err(self):
        mock_tx = {
            "slot": 250000001,
            "blockTime": 1700000001,
            "meta": {
                "err": {"InstructionError": [0, "Custom: 1"]},
                "fee": 5000,
                "logMessages": ["Program log: Error"],
            },
        }
        with patch(RPC, return_value=mock_tx):
            result = agent.execute_tool("get_transaction", {"signature": "failed123"})
        data = json.loads(result)
        assert data["err"] is not None

    def test_not_found_returns_message(self):
        with patch(RPC, return_value=None):
            result = agent.execute_tool("get_transaction", {"signature": "notfound"})
        assert "not found" in result

    def test_log_messages_capped_at_10(self):
        mock_tx = {
            "slot": 1,
            "blockTime": 1,
            "meta": {
                "err": None,
                "fee": 0,
                "logMessages": [f"log {i}" for i in range(25)],
            },
        }
        with patch(RPC, return_value=mock_tx):
            result = agent.execute_tool("get_transaction", {"signature": "sig"})
        data = json.loads(result)
        assert len(data["logMessages"]) == 10


# ── get_recent_txs ────────────────────────────────────────────────────────────

class TestGetRecentTxs:

    def test_returns_formatted_lines(self):
        mock_sigs = [
            {"signature": "sig1abc", "slot": 250000000, "err": None},
            {"signature": "sig2def", "slot": 249999999, "err": {"Custom": 1}},
        ]
        with patch(RPC, return_value=mock_sigs):
            result = agent.execute_tool("get_recent_txs", {"address": PUBKEY, "limit": 2})
        assert "sig1abc" in result
        assert "sig2def" in result
        assert "err=None" in result
        assert "err=" in result

    def test_no_txs(self):
        with patch(RPC, return_value=[]):
            result = agent.execute_tool("get_recent_txs", {"address": PUBKEY})
        assert "No recent transactions" in result

    def test_default_limit_used(self):
        """Verify limit defaults to 10 when not provided."""
        captured = []
        def fake_rpc(method, params, **kwargs):
            captured.append(params)
            return []
        with patch(RPC, side_effect=fake_rpc):
            agent.execute_tool("get_recent_txs", {"address": PUBKEY})
        assert captured[0][1]["limit"] == 10

    def test_custom_limit(self):
        captured = []
        def fake_rpc(method, params, **kwargs):
            captured.append(params)
            return []
        with patch(RPC, side_effect=fake_rpc):
            agent.execute_tool("get_recent_txs", {"address": PUBKEY, "limit": 20})
        assert captured[0][1]["limit"] == 20


# ── get_slot ──────────────────────────────────────────────────────────────────

class TestGetSlot:

    def test_returns_slot(self):
        with patch(RPC, return_value=250000000):
            result = agent.execute_tool("get_slot", {})
        assert "250000000" in result
        assert "Current slot" in result


# ── get_recent_blockhash ──────────────────────────────────────────────────────

class TestGetRecentBlockhash:

    def test_returns_blockhash_and_height(self):
        def fake_rpc(method, params, **kwargs):
            if method == "getLatestBlockhash":
                return {"value": {"blockhash": "ABC123blockhash", "lastValidBlockHeight": 250000150}}
            if method == "getSlot":
                return 250000000
            return {}

        with patch(RPC, side_effect=fake_rpc):
            result = agent.execute_tool("get_recent_blockhash", {})
        assert "ABC123blockhash" in result
        assert "250000150" in result
        assert "250000000" in result

    def test_rpc_error(self):
        with patch(RPC, return_value={"error": "node unavailable"}):
            result = agent.execute_tool("get_recent_blockhash", {})
        assert "[RPC error" in result


# ── unknown tool ──────────────────────────────────────────────────────────────

def test_unknown_tool():
    result = agent.execute_tool("nonexistent_tool", {})
    assert "[Unknown tool: nonexistent_tool]" == result


# ── tool_definitions sanity ───────────────────────────────────────────────────

def test_all_tools_have_required_fields():
    for tool in agent.tool_definitions:
        assert "name" in tool and "description" in tool and "input_schema" in tool
        assert len(tool["description"]) > 20, f"Tool '{tool['name']}' description too short"


def test_system_prompt_contains_guidance():
    for kw in ("BALANCE_INSUFFICIENT", "BALANCE_OK", "HTLC_REDEEMED", "HTLC_PENDING"):
        assert kw in agent.system_prompt, f"system_prompt missing keyword: {kw}"
