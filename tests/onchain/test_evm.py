"""
Unit tests for EVMOnChainAgent.execute_tool()
Mocks web3 and Garden API calls — no live node required.
"""
import json
import pytest
from unittest.mock import patch, MagicMock

from agents.onchain.evm import EVMOnChainAgent

agent = EVMOnChainAgent()

# Patch targets
GET_W3  = "agents.onchain.evm._get_w3"
LOOKUP  = "agents.onchain.evm.lookup_asset"
GET_ABI = "agents.onchain.evm.get_abi"

ADDR = "0x" + "ab" * 20   # valid 20-byte EVM address


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    """Ensure evm_rpc_url is always set so execute_tool doesn't short-circuit."""
    from config import settings
    monkeypatch.setattr(settings, "evm_rpc_url", "https://erpc.garden.finance/rpc")


def _mock_w3():
    return MagicMock()


# ── get_native_balance ────────────────────────────────────────────────────────

class TestGetNativeBalance:

    def test_returns_wei_and_eth(self):
        w3 = _mock_w3()
        w3.eth.get_balance.return_value = 10_000_000_000_000_000
        w3.from_wei.return_value = 0.01
        with patch(GET_W3, return_value=w3):
            result = agent.execute_tool("get_native_balance", {"address": ADDR, "chain": "ethereum"})
        assert "10000000000000000 wei" in result
        assert "ETH" in result

    def test_zero_balance(self):
        w3 = _mock_w3()
        w3.eth.get_balance.return_value = 0
        w3.from_wei.return_value = 0.0
        with patch(GET_W3, return_value=w3):
            result = agent.execute_tool("get_native_balance", {"address": ADDR, "chain": "citrea"})
        assert "0 wei" in result

    def test_no_rpc_url_returns_error(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "evm_rpc_url", "")
        result = agent.execute_tool("get_native_balance", {"address": ADDR})
        assert "not configured" in result


# ── get_token_balance ─────────────────────────────────────────────────────────

class TestGetTokenBalance:

    def _mock_asset(self, is_native=False):
        return {
            "token_address": None if is_native else "0x" + "cc" * 20,
            "htlc_address":  "0x" + "dd" * 20,
            "decimals":      6,
            "is_native":     is_native,
            "chain_id":      "evm:1",
            "htlc_schema":   "evm:htlc_erc20",
        }

    def test_erc20_balance(self):
        w3 = _mock_w3()
        w3.eth.contract.return_value.functions.balanceOf.return_value.call.return_value = 5_000_000  # 5 USDT
        with patch(GET_W3, return_value=w3), \
             patch(LOOKUP, return_value=self._mock_asset()), \
             patch(GET_ABI, return_value=[]):
            result = agent.execute_tool("get_token_balance", {
                "wallet_address": ADDR,
                "asset_id": "ethereum:usdt",
                "chain": "ethereum",
            })
        assert "5000000 raw" in result
        assert "5.000000 tokens" in result
        assert "ethereum:usdt" in result

    def test_native_asset_falls_back_to_eth_balance(self):
        w3 = _mock_w3()
        w3.eth.get_balance.return_value = 1_000_000_000_000_000_000
        w3.from_wei.return_value = 1.0
        with patch(GET_W3, return_value=w3), patch(LOOKUP, return_value=self._mock_asset(is_native=True)):
            result = agent.execute_tool("get_token_balance", {
                "wallet_address": ADDR,
                "asset_id": "citrea:cbtc",
                "chain": "citrea",
            })
        assert "Native asset" in result
        assert "wei" in result

    def test_unknown_asset_id(self):
        with patch(GET_W3, return_value=_mock_w3()), patch(LOOKUP, return_value=None):
            result = agent.execute_tool("get_token_balance", {
                "wallet_address": ADDR,
                "asset_id": "unknown:xyz",
                "chain": "ethereum",
            })
        assert "Asset not found" in result

    def test_garden_api_failure_falls_back_to_inline_abi(self):
        w3 = _mock_w3()
        w3.eth.contract.return_value.functions.balanceOf.return_value.call.return_value = 1_000_000
        with patch(GET_W3, return_value=w3), \
             patch(LOOKUP, return_value=self._mock_asset()), \
             patch(GET_ABI, side_effect=Exception("API down")):
            result = agent.execute_tool("get_token_balance", {
                "wallet_address": ADDR,
                "asset_id": "ethereum:usdt",
                "chain": "ethereum",
            })
        assert "1000000 raw" in result


# ── get_transaction ───────────────────────────────────────────────────────────

class TestGetTransaction:

    def test_mined_transaction(self):
        w3 = _mock_w3()
        w3.eth.get_transaction.return_value = {
            "hash": bytes.fromhex("abcd" * 16), "from": "0xS", "to": "0xR",
            "nonce": 5, "value": 0, "gas": 21000,
            "gasPrice": 10**9, "maxFeePerGas": None, "blockNumber": 19000000,
        }
        with patch(GET_W3, return_value=w3):
            result = agent.execute_tool("get_transaction", {
                "tx_hash": "0x" + "ab" * 32, "chain": "ethereum",
            })
        data = json.loads(result)
        assert data["blockNumber"] == 19000000
        assert data["nonce"] == 5

    def test_pending_tx_has_null_block(self):
        w3 = _mock_w3()
        w3.eth.get_transaction.return_value = {
            "hash": b"\xab" * 32, "from": "0xA", "to": "0xB",
            "nonce": 3, "value": 0, "gas": 21000,
            "gasPrice": 10**9, "maxFeePerGas": None, "blockNumber": None,
        }
        with patch(GET_W3, return_value=w3):
            result = agent.execute_tool("get_transaction", {
                "tx_hash": "0x" + "ab" * 32, "chain": "arbitrum",
            })
        assert json.loads(result)["blockNumber"] is None

    def test_rpc_exception(self):
        w3 = _mock_w3()
        w3.eth.get_transaction.side_effect = Exception("tx not found")
        with patch(GET_W3, return_value=w3):
            result = agent.execute_tool("get_transaction", {
                "tx_hash": "0x00", "chain": "ethereum",
            })
        assert "[RPC error" in result


# ── get_transaction_receipt ───────────────────────────────────────────────────

class TestGetTransactionReceipt:

    def test_success_receipt(self):
        w3 = _mock_w3()
        w3.eth.get_transaction_receipt.return_value = {
            "status": 1, "blockNumber": 19000001,
            "gasUsed": 50000, "effectiveGasPrice": 10**9,
            "logs": [MagicMock(), MagicMock()], "contractAddress": None,
        }
        with patch(GET_W3, return_value=w3):
            result = agent.execute_tool("get_transaction_receipt", {
                "tx_hash": "0xabc", "chain": "ethereum",
            })
        data = json.loads(result)
        assert data["status"] == 1
        assert data["logs_count"] == 2

    def test_reverted_receipt(self):
        w3 = _mock_w3()
        w3.eth.get_transaction_receipt.return_value = {
            "status": 0, "blockNumber": 19000002,
            "gasUsed": 50000, "effectiveGasPrice": 10**9,
            "logs": [], "contractAddress": None,
        }
        with patch(GET_W3, return_value=w3):
            result = agent.execute_tool("get_transaction_receipt", {
                "tx_hash": "0xabc", "chain": "base",
            })
        assert json.loads(result)["status"] == 0

    def test_not_yet_mined(self):
        w3 = _mock_w3()
        w3.eth.get_transaction_receipt.return_value = None
        with patch(GET_W3, return_value=w3):
            result = agent.execute_tool("get_transaction_receipt", {
                "tx_hash": "0xabc", "chain": "ethereum",
            })
        assert "not yet mined" in result


# ── get_htlc_order_state ──────────────────────────────────────────────────────

class TestGetHtlcOrderState:

    # Tuple: (initiator, redeemer, initiatedAt, timelock, amount, fulfilledAt)
    def _order(self, initiated_at=1700000000, fulfilled_at=0):
        return ("0xA", "0xB", initiated_at, 3600, 1000000, fulfilled_at)

    def test_pending_order(self):
        w3 = _mock_w3()
        w3.eth.contract.return_value.functions.orders.return_value.call.return_value = \
            self._order(initiated_at=1700000000, fulfilled_at=0)
        with patch(GET_W3, return_value=w3), patch(GET_ABI, return_value=[]):
            result = agent.execute_tool("get_htlc_order_state", {
                "contract_address": "0x" + "aa" * 20,
                "order_id": "0x" + "bb" * 32,
                "chain": "ethereum",
            })
        assert "pending" in result
        assert "fulfilledAt=0" in result

    def test_redeemed_order(self):
        w3 = _mock_w3()
        w3.eth.contract.return_value.functions.orders.return_value.call.return_value = \
            self._order(fulfilled_at=1700001000)
        with patch(GET_W3, return_value=w3), patch(GET_ABI, return_value=[]):
            result = agent.execute_tool("get_htlc_order_state", {
                "contract_address": "0x" + "aa" * 20,
                "order_id": "0x" + "bb" * 32,
                "chain": "arbitrum",
            })
        assert "redeemed" in result
        assert "fulfilledAt=1700001000" in result

    def test_not_initiated_order(self):
        w3 = _mock_w3()
        w3.eth.contract.return_value.functions.orders.return_value.call.return_value = \
            self._order(initiated_at=0, fulfilled_at=0)
        with patch(GET_W3, return_value=w3), patch(GET_ABI, return_value=[]):
            result = agent.execute_tool("get_htlc_order_state", {
                "contract_address": "0x" + "aa" * 20,
                "order_id": "0x" + "bb" * 32,
                "chain": "citrea",
            })
        assert "not_initiated" in result

    def test_falls_back_to_inline_abi_on_api_failure(self):
        w3 = _mock_w3()
        w3.eth.contract.return_value.functions.orders.return_value.call.return_value = \
            self._order()
        with patch(GET_W3, return_value=w3), patch(GET_ABI, side_effect=Exception("API down")):
            result = agent.execute_tool("get_htlc_order_state", {
                "contract_address": "0x" + "aa" * 20,
                "order_id": "0x" + "bb" * 32,
                "chain": "ethereum",
            })
        assert "pending" in result  # didn't crash


# ── get_logs ──────────────────────────────────────────────────────────────────

class TestGetLogs:

    def test_logs_found(self):
        w3 = _mock_w3()
        w3.eth.get_logs.return_value = [{
            "blockNumber": 19000000,
            "transactionHash": bytes.fromhex("ab" * 32),
            "topics": [bytes.fromhex("cd" * 32)],
            "data": "0x" + "ef" * 32,
        }]
        with patch(GET_W3, return_value=w3):
            result = agent.execute_tool("get_logs", {
                "contract_address": "0x" + "aa" * 20,
                "from_block": 19000000,
                "chain": "ethereum",
            })
        logs = json.loads(result)
        assert len(logs) == 1
        assert logs[0]["blockNumber"] == 19000000

    def test_no_logs_returns_message(self):
        w3 = _mock_w3()
        w3.eth.get_logs.return_value = []
        with patch(GET_W3, return_value=w3):
            result = agent.execute_tool("get_logs", {
                "contract_address": "0x" + "aa" * 20,
                "from_block": 0,
                "chain": "base",
            })
        assert "No logs found" in result

    def test_caps_at_20_entries(self):
        w3 = _mock_w3()
        w3.eth.get_logs.return_value = [
            {"blockNumber": i, "transactionHash": bytes(32), "topics": [], "data": "0x"}
            for i in range(50)
        ]
        with patch(GET_W3, return_value=w3):
            result = agent.execute_tool("get_logs", {
                "contract_address": "0x" + "aa" * 20,
                "from_block": 0,
                "chain": "ethereum",
            })
        assert len(json.loads(result)) == 20


# ── get_nonce ─────────────────────────────────────────────────────────────────

class TestGetNonce:

    def test_no_queued_txs(self):
        w3 = _mock_w3()
        w3.eth.get_transaction_count.side_effect = lambda addr, state: 10
        with patch(GET_W3, return_value=w3):
            result = agent.execute_tool("get_nonce", {"address": ADDR, "chain": "ethereum"})
        assert "Confirmed nonce: 10" in result
        assert "Queued txs in pool: 0" in result

    def test_gap_detected(self):
        w3 = _mock_w3()
        w3.eth.get_transaction_count.side_effect = lambda addr, state: 10 if state == "latest" else 13
        with patch(GET_W3, return_value=w3):
            result = agent.execute_tool("get_nonce", {"address": ADDR, "chain": "arbitrum"})
        assert "Queued txs in pool: 3" in result


# ── get_pending_txs ───────────────────────────────────────────────────────────

class TestGetPendingTxs:

    def test_pending_txs_found(self):
        w3 = _mock_w3()
        w3.manager.request_blocking.return_value = {
            "pending": {ADDR.lower(): {"5": {"hash": "0xdeadbeef", "gasPrice": "1e9", "nonce": "5"}}}
        }
        with patch(GET_W3, return_value=w3):
            result = agent.execute_tool("get_pending_txs", {"address": ADDR, "chain": "ethereum"})
        assert "0xdeadbeef" in result

    def test_no_pending_txs(self):
        w3 = _mock_w3()
        w3.manager.request_blocking.return_value = {"pending": {}}
        with patch(GET_W3, return_value=w3):
            result = agent.execute_tool("get_pending_txs", {"address": ADDR, "chain": "ethereum"})
        assert "No pending transactions" in result


# ── get_gas_price ─────────────────────────────────────────────────────────────

class TestGetGasPrice:

    def test_returns_gwei(self):
        w3 = _mock_w3()
        w3.eth.gas_price = 20 * 10**9
        w3.eth.get_block.return_value = {"baseFeePerGas": 15 * 10**9}
        w3.from_wei.side_effect = lambda val, unit: f"{val / 10**9:.1f}"
        with patch(GET_W3, return_value=w3):
            result = agent.execute_tool("get_gas_price", {"chain": "ethereum"})
        assert "Gwei" in result


# ── sanity ────────────────────────────────────────────────────────────────────

def test_unknown_tool():
    with patch(GET_W3, return_value=_mock_w3()):
        assert "[Unknown tool" in agent.execute_tool("does_not_exist", {"chain": "ethereum"})


def test_all_tools_have_required_fields():
    for tool in agent.tool_definitions:
        assert "name" in tool and "description" in tool and "input_schema" in tool
        assert len(tool["description"]) > 20


def test_system_prompt_contains_guidance():
    for kw in ("BALANCE_INSUFFICIENT", "BALANCE_OK", "HTLC_REDEEMED", "HTLC_PENDING"):
        assert kw in agent.system_prompt


def test_chain_param_in_all_tools():
    """Every tool definition should accept a 'chain' parameter."""
    for tool in agent.tool_definitions:
        props = tool["input_schema"].get("properties", {})
        assert "chain" in props, f"Tool '{tool['name']}' missing 'chain' parameter"
