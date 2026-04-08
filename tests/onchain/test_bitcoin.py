"""
Tests for BitcoinOnChainAgent.execute_tool()

All tests mock agents.onchain.bitcoin._rpc to avoid needing a live Bitcoin node.
Each test covers one tool + the key edge cases within that tool.
"""
import pytest
from unittest.mock import patch

from agents.onchain.bitcoin import BitcoinOnChainAgent

PATCH_TARGET = "agents.onchain.bitcoin._rpc"

agent = BitcoinOnChainAgent()


# ── get_address_balance ────────────────────────────────────────────────────────

class TestGetAddressBalance:

    def test_returns_satoshis_and_utxo_count(self):
        mock_result = {
            "total_amount": 0.001,       # 100_000 sats
            "unspents": [{"txid": "abc", "vout": 0}, {"txid": "def", "vout": 1}],
            "searched_items": 5,
        }
        with patch(PATCH_TARGET, return_value=mock_result):
            result = agent.execute_tool("get_address_balance", {"address": "bc1qtest"})

        assert "100000 satoshis" in result
        assert "0.001 BTC" in result
        assert "confirmed UTXOs: 2" in result

    def test_zero_balance(self):
        mock_result = {"total_amount": 0.0, "unspents": [], "searched_items": 3}
        with patch(PATCH_TARGET, return_value=mock_result):
            result = agent.execute_tool("get_address_balance", {"address": "bc1qempty"})

        assert "0 satoshis" in result
        assert "confirmed UTXOs: 0" in result

    def test_rpc_error_returned(self):
        with patch(PATCH_TARGET, return_value={"error": {"code": -5, "message": "Invalid address"}}):
            result = agent.execute_tool("get_address_balance", {"address": "badaddr"})

        assert "[RPC error]" in result
        assert "Invalid address" in result


# ── get_address_utxos ─────────────────────────────────────────────────────────

class TestGetAddressUtxos:

    def test_utxos_present_htlc_pending(self):
        mock_result = {
            "unspents": [{"txid": "aabbcc", "vout": 0, "amount": 0.0005}],
            "total_amount": 0.0005,
            "searched_items": 2,
        }
        with patch(PATCH_TARGET, return_value=mock_result):
            result = agent.execute_tool("get_address_utxos", {"address": "bc1qhtlc"})

        assert "1 UTXO(s) found" in result
        assert "50000 sats" in result

    def test_no_utxos_htlc_spent(self):
        mock_result = {"unspents": [], "total_amount": 0.0, "searched_items": 4}
        with patch(PATCH_TARGET, return_value=mock_result):
            result = agent.execute_tool("get_address_utxos", {"address": "bc1qhtlcspent"})

        assert "No UTXOs found" in result
        assert "HTLC has been spent" in result

    def test_rpc_error(self):
        with patch(PATCH_TARGET, return_value={"error": {"code": -1, "message": "scan failed"}}):
            result = agent.execute_tool("get_address_utxos", {"address": "bc1qerr"})

        assert "[RPC error]" in result


# ── get_transaction ───────────────────────────────────────────────────────────

class TestGetTransaction:

    def test_confirmed_transaction(self):
        mock_tx = {
            "txid": "deadbeef" * 8,
            "confirmations": 6,
            "vin": [{"txid": "prev", "vout": 0}],
            "vout": [{"value": 0.001, "n": 0}],
        }
        with patch(PATCH_TARGET, return_value=mock_tx):
            result = agent.execute_tool("get_transaction", {"txid": "deadbeef" * 8})

        assert "confirmations" in result
        assert "deadbeef" in result

    def test_unconfirmed_transaction(self):
        mock_tx = {"txid": "abc123", "confirmations": 0, "vin": [], "vout": []}
        with patch(PATCH_TARGET, return_value=mock_tx):
            result = agent.execute_tool("get_transaction", {"txid": "abc123"})

        assert "confirmations" in result

    def test_rpc_error_unknown_txid(self):
        with patch(PATCH_TARGET, return_value={"error": {"code": -5, "message": "No such mempool or blockchain transaction"}}):
            result = agent.execute_tool("get_transaction", {"txid": "notfound"})

        # The raw RPC dict is returned as str — error key present
        assert "error" in result


# ── check_mempool ─────────────────────────────────────────────────────────────

class TestCheckMempool:

    def test_tx_in_mempool(self):
        mock_entry = {
            "fees": {"base": 0.0001, "modified": 0.0001},
            "vsize": 250,
            "time": 1700000000,
            "bip125-replaceable": True,
            "ancestorcount": 1,
            "ancestorsize": 250,
        }
        with patch(PATCH_TARGET, return_value=mock_entry):
            result = agent.execute_tool("check_mempool", {"txid": "abc"})

        assert "Transaction IS in mempool" in result
        assert "fee_sat" in result
        assert "10000" in result  # 0.0001 BTC = 10000 sats
        assert "fee_rate_sat_vbyte" in result

    def test_tx_not_in_mempool(self):
        with patch(PATCH_TARGET, return_value={"error": {"code": -5, "message": "Transaction not in mempool"}}):
            result = agent.execute_tool("check_mempool", {"txid": "gone"})

        assert "Transaction NOT in mempool" in result


# ── get_fee_rate ──────────────────────────────────────────────────────────────

class TestGetFeeRate:

    def test_all_targets_available(self):
        # get_fee_rate now uses _rpc_batch — patch that instead
        mock_results = [
            {"feerate": 0.001, "blocks": 1},
            {"feerate": 0.0005, "blocks": 6},
            {"feerate": 0.0002, "blocks": 144},
        ]
        with patch("agents.onchain.bitcoin._rpc_batch", return_value=mock_results):
            result = agent.execute_tool("get_fee_rate", {})

        assert "fast(1-block)" in result
        assert "normal(6-block)" in result
        assert "slow(144-block)" in result
        assert "sat/vbyte" in result

    def test_fee_unavailable(self):
        mock_results = [
            {"errors": ["Insufficient data"]},
            {"errors": ["Insufficient data"]},
            {"errors": ["Insufficient data"]},
        ]
        with patch("agents.onchain.bitcoin._rpc_batch", return_value=mock_results):
            result = agent.execute_tool("get_fee_rate", {})

        assert "unavailable" in result

    def test_conversion_accuracy(self):
        # 0.001 BTC/kB = 0.001 * 1e8 / 1000 = 100.0 sat/vbyte
        mock_results = [{"feerate": 0.001}, {"feerate": 0.001}, {"feerate": 0.001}]
        with patch("agents.onchain.bitcoin._rpc_batch", return_value=mock_results):
            result = agent.execute_tool("get_fee_rate", {})
        assert "100.0" in result


# ── get_mempool_info ──────────────────────────────────────────────────────────

class TestGetMempoolInfo:

    def test_returns_stats(self):
        mock_info = {
            "size": 12345,
            "bytes": 5000000,
            "mempoolminfee": 0.00001,
        }
        with patch(PATCH_TARGET, return_value=mock_info):
            result = agent.execute_tool("get_mempool_info", {})

        assert "12345" in result
        assert "5000000" in result

    def test_rpc_error(self):
        with patch(PATCH_TARGET, return_value={"error": "node unavailable"}):
            result = agent.execute_tool("get_mempool_info", {})

        assert "error" in result


# ── get_block_count ───────────────────────────────────────────────────────────

class TestGetBlockCount:

    def test_returns_block_height(self):
        with patch(PATCH_TARGET, return_value=840000):
            result = agent.execute_tool("get_block_count", {})

        assert "840000" in result
        assert "Current block height" in result

    def test_rpc_error_falls_back_to_str(self):
        with patch(PATCH_TARGET, return_value={"error": "RPC unavailable"}):
            result = agent.execute_tool("get_block_count", {})

        assert "error" in result


# ── unknown tool ──────────────────────────────────────────────────────────────

def test_unknown_tool_returns_error():
    result = agent.execute_tool("nonexistent_tool", {})
    assert "[Unknown tool: nonexistent_tool]" == result


# ── tool_definitions sanity check ────────────────────────────────────────────

def test_all_tools_have_required_fields():
    for tool in agent.tool_definitions:
        assert "name" in tool
        assert "description" in tool
        assert "input_schema" in tool
        assert len(tool["description"]) > 20, f"Tool '{tool['name']}' description too short"


def test_system_prompt_contains_guidance():
    prompt = agent.system_prompt
    for keyword in ("BALANCE_INSUFFICIENT", "BALANCE_OK", "HTLC_REDEEMED", "HTLC_PENDING"):
        assert keyword in prompt, f"system_prompt missing keyword: {keyword}"
