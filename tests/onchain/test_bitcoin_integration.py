"""
Integration tests for BitcoinOnChainAgent against a live Bitcoin mainnet node.

Run with:
  BITCOIN_RPC_URL=http://alphamainnode.dealpulley.com \
  BITCOIN_RPC_USER=catal0g_rpc \
  BITCOIN_RPC_PASS=3f6b60a37d86f497e64cefef920311972f61160eb4abd9d7fd2b2aa24842b872 \
  .venv/bin/python -m pytest tests/onchain/test_bitcoin_integration.py -v -s

Skipped automatically if BITCOIN_RPC_URL is not set in the environment.
"""
import os
import pytest

from agents.onchain.bitcoin import BitcoinOnChainAgent

# Known mainnet HTLC address that has already been spent (no UTXOs)
HTLC_ADDRESS = "bc1pwq2adudvsfckcvydcv8lu5k0pg76wd4h977zqxdfr2dm700kclfqsvpj7u"

pytestmark = pytest.mark.skipif(
    not os.getenv("BITCOIN_RPC_URL"),
    reason="BITCOIN_RPC_URL not set — skipping integration tests",
)

@pytest.fixture(scope="module")
def agent():
    return BitcoinOnChainAgent()

@pytest.fixture(scope="module")
def mempool_txid(agent):
    """Grab a real txid from the mempool to use in tx/mempool tests."""
    from agents.onchain.bitcoin import _rpc
    pool = _rpc("getrawmempool", [False])
    assert isinstance(pool, list) and len(pool) > 0, "Mempool is empty — cannot run tx tests"
    return pool[0]


# ── get_block_count ────────────────────────────────────────────────────────────

def test_get_block_count_returns_mainnet_height(agent):
    result = agent.execute_tool("get_block_count", {})
    print("\n", result)
    assert "Current block height:" in result
    height = int(result.split(":")[-1].strip())
    assert height > 900_000, f"Expected mainnet height > 900k, got {height}"


# ── get_fee_rate ──────────────────────────────────────────────────────────────

def test_get_fee_rate_returns_sat_per_vbyte(agent):
    result = agent.execute_tool("get_fee_rate", {})
    print("\n", result)
    assert "sat/vbyte" in result
    assert "fast(1-block)" in result
    assert "normal(6-block)" in result
    assert "slow(144-block)" in result


# ── get_mempool_info ──────────────────────────────────────────────────────────

def test_get_mempool_info_returns_stats(agent):
    result = agent.execute_tool("get_mempool_info", {})
    print("\n", result)
    assert "size" in result
    assert "bytes" in result


# ── get_transaction (mempool tx) ──────────────────────────────────────────────

def test_get_transaction_mempool_tx(agent, mempool_txid):
    result = agent.execute_tool("get_transaction", {"txid": mempool_txid})
    print("\n", result[:300])
    assert mempool_txid in result
    # Unconfirmed tx — confirmations field absent or 0
    assert "txid" in result


def test_get_transaction_unknown_txid_returns_error(agent):
    result = agent.execute_tool("get_transaction", {"txid": "aa" * 32})
    print("\n", result)
    assert "error" in result.lower()


# ── check_mempool ─────────────────────────────────────────────────────────────

def test_check_mempool_tx_is_present(agent, mempool_txid):
    result = agent.execute_tool("check_mempool", {"txid": mempool_txid})
    print("\n", result)
    assert "Transaction IS in mempool" in result
    assert "fees" in result


def test_check_mempool_unknown_tx_not_found(agent):
    result = agent.execute_tool("check_mempool", {"txid": "bb" * 32})
    print("\n", result)
    assert "Transaction NOT in mempool" in result


# ── get_address_balance (HTLC address — already spent) ───────────────────────

@pytest.mark.timeout(130)
def test_get_address_balance_htlc_spent_is_zero(agent):
    """The known HTLC address has been redeemed — balance should be 0."""
    result = agent.execute_tool("get_address_balance", {"address": HTLC_ADDRESS})
    print("\n", result)
    assert "Balance:" in result
    assert "0 satoshis" in result
    assert "confirmed UTXOs: 0" in result


# ── get_address_utxos (HTLC address — already spent) ─────────────────────────

@pytest.mark.timeout(130)
def test_get_address_utxos_htlc_spent_signals_redeemed(agent):
    """No UTXOs at this HTLC address → agent should say HTLC was spent."""
    result = agent.execute_tool("get_address_utxos", {"address": HTLC_ADDRESS})
    print("\n", result)
    assert "No UTXOs found" in result
    assert "HTLC has been spent" in result
