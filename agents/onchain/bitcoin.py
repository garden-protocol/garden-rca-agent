"""Bitcoin on-chain query agent using Bitcoin RPC."""
import time
import httpx
from config import settings
from agents.onchain.base import BaseOnChainAgent


def _rpc(method: str, params: list, timeout: int = 15) -> dict:
    """Make a single Bitcoin RPC call."""
    payload = {"jsonrpc": "1.0", "id": "rca", "method": method, "params": params}
    try:
        resp = httpx.post(
            settings.bitcoin_rpc_url,
            json=payload,
            auth=(settings.bitcoin_rpc_user, settings.bitcoin_rpc_pass),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            return {"error": data["error"]}
        return data.get("result", {})
    except Exception as e:
        return {"error": str(e)}


def _rpc_batch(calls: list[tuple[str, list]], timeout: int = 15) -> list[dict]:
    """Send multiple RPC calls in one HTTP request (JSON-RPC batch).
    Returns results in the same order as calls."""
    payload = [
        {"jsonrpc": "1.0", "id": i, "method": method, "params": params}
        for i, (method, params) in enumerate(calls)
    ]
    try:
        resp = httpx.post(
            settings.bitcoin_rpc_url,
            json=payload,
            auth=(settings.bitcoin_rpc_user, settings.bitcoin_rpc_pass),
            timeout=timeout,
        )
        resp.raise_for_status()
        # Sort by id to guarantee order matches input
        results = sorted(resp.json(), key=lambda r: r["id"])
        return [r.get("result") if not r.get("error") else {"error": r["error"]} for r in results]
    except Exception as e:
        return [{"error": str(e)}] * len(calls)


# scantxoutset is slow (~90s on mainnet) — cache per address for 5 minutes
# so get_address_balance and get_address_utxos share one RPC call if called
# in the same agentic loop turn.
_SCAN_CACHE: dict[str, tuple[float, dict]] = {}
_SCAN_TTL = 300  # seconds


def _scantxoutset(address: str) -> dict:
    now = time.monotonic()
    if address in _SCAN_CACHE:
        ts, cached = _SCAN_CACHE[address]
        if now - ts < _SCAN_TTL:
            return cached
    result = _rpc("scantxoutset", ["start", [f"addr({address})"]], timeout=120)
    if "error" not in result:
        _SCAN_CACHE[address] = (now, result)
    return result


class BitcoinOnChainAgent(BaseOnChainAgent):

    @property
    def chain(self) -> str:
        return "bitcoin"

    @property
    def system_prompt(self) -> str:
        return """\
You are a Bitcoin on-chain query agent for the Garden bridge system.

Tool usage guide:
- BALANCE check → use get_address_balance. Start response with BALANCE_INSUFFICIENT or BALANCE_OK.
- HTLC redeemed check → use get_address_utxos on the HTLC address. No UTXOs = HTLC was spent \
(HTLC_REDEEMED). UTXOs present = still locked (HTLC_PENDING). Optionally call get_transaction \
on the spending tx to confirm spend type (witness with 4 elements = redeem, 3 elements = refund).
- Transaction status → use get_transaction (check confirmations). If 0 confirmations, \
call check_mempool to see if it is still queued and check its fee rate.
- Fee congestion → use get_fee_rate and get_mempool_info.
- Timelock / CSV eligibility → use get_block_count for current block height.

Always start with the required keyword (BALANCE_INSUFFICIENT, BALANCE_OK, HTLC_REDEEMED, \
HTLC_PENDING) when the question asks for one. Be precise and factual.\
"""

    @property
    def tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "get_address_balance",
                "description": (
                    "Get the total confirmed BTC balance of a Bitcoin address in satoshis. "
                    "Uses scantxoutset to aggregate all UTXOs. "
                    "USE THIS for relayer/executor balance checks (BALANCE_INSUFFICIENT / BALANCE_OK)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "Bitcoin address (P2PKH, P2SH, P2WPKH, P2TR, etc.)"},
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_address_utxos",
                "description": (
                    "Get all unspent transaction outputs (UTXOs) for a Bitcoin address via scantxoutset. "
                    "USE THIS to check if a P2TR HTLC address has been spent: "
                    "if unspents is empty → HTLC was spent (redeemed or refunded); "
                    "if unspents are present → HTLC is still locked and pending."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "Bitcoin address to scan for UTXOs"},
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_transaction",
                "description": (
                    "Get full details of a Bitcoin transaction by txid via getrawtransaction. "
                    "Returns confirmations, inputs, outputs, and witness data. "
                    "Witness with 4 elements = redeem spend (element[1] is the preimage/secret). "
                    "Witness with 3 elements = refund spend (CSV timelock path). "
                    "USE THIS to check tx confirmation status and to identify HTLC spend type."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "txid": {"type": "string", "description": "Bitcoin transaction ID (64 hex chars)"},
                    },
                    "required": ["txid"],
                },
            },
            {
                "name": "check_mempool",
                "description": (
                    "Check if a specific transaction is currently in the mempool via getmempoolentry. "
                    "Returns fee rate (sat/vbyte), ancestor fees, and position info if present. "
                    "Returns NOT in mempool if the tx was dropped or never broadcast. "
                    "USE THIS after get_transaction shows 0 confirmations to diagnose stuck txs."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "txid": {"type": "string", "description": "Bitcoin transaction ID to look up in mempool"},
                    },
                    "required": ["txid"],
                },
            },
            {
                "name": "get_fee_rate",
                "description": (
                    "Get current Bitcoin network fee rates in sat/vbyte via estimatesmartfee "
                    "for 3 confirmation targets: fast (1-block), normal (6-block), slow (144-block). "
                    "USE THIS to assess network congestion and whether a tx fee was too low."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "get_mempool_info",
                "description": (
                    "Get general mempool statistics via getmempoolinfo: tx count, total size in bytes, "
                    "minimum fee rate to be accepted. "
                    "USE THIS alongside get_fee_rate to assess overall network congestion."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "get_block_count",
                "description": (
                    "Get the current Bitcoin block height via getblockcount. "
                    "USE THIS to check whether a CSV timelock has elapsed "
                    "(compare current height to the initiation block + timelock blocks) "
                    "or to assess how many confirmations a tx has relative to current tip."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "get_address_balance":
            # Shared cache with get_address_utxos — avoids double 90s scan
            result = _scantxoutset(tool_input["address"])
            if "error" in result:
                return f"[RPC error] {result['error']}"
            total_btc = result.get("total_amount", 0)
            total_sats = round(total_btc * 1e8)
            utxo_count = len(result.get("unspents", []))
            return f"Balance: {total_sats} satoshis ({total_btc} BTC), confirmed UTXOs: {utxo_count}"

        elif tool_name == "get_address_utxos":
            # Shared cache with get_address_balance — avoids double 90s scan
            result = _scantxoutset(tool_input["address"])
            if "error" in result:
                return f"[RPC error] {result['error']}"
            unspents = result.get("unspents", [])
            total_sats = round(result.get("total_amount", 0) * 1e8)
            if not unspents:
                return "No UTXOs found at address — HTLC has been spent."
            # Return only txid+vout+amount per UTXO, not the full scriptPubKey blob
            slim = [{"txid": u["txid"], "vout": u["vout"], "amount_sats": round(u["amount"] * 1e8)} for u in unspents]
            return f"{len(slim)} UTXO(s) found, total {total_sats} sats. UTXOs: {slim}"

        elif tool_name == "get_transaction":
            result = _rpc("getrawtransaction", [tool_input["txid"], True])
            if "error" in result:
                return f"[RPC error] {result['error']}"
            # Trim to fields Haiku needs — strip hex, full scripts, and raw input scripts
            slim = {
                "txid": result.get("txid"),
                "confirmations": result.get("confirmations", 0),
                "size": result.get("size"),
                "vsize": result.get("vsize"),
                "fee": result.get("fee"),
                "vin": [
                    {"txid": v.get("txid"), "vout": v.get("vout"),
                     "witness": v.get("txinwitness", [])}
                    for v in result.get("vin", [])
                ],
                "vout": [
                    {"value_sats": round(v.get("value", 0) * 1e8),
                     "address": v.get("scriptPubKey", {}).get("address")}
                    for v in result.get("vout", [])
                ],
                "blockhash": result.get("blockhash"),
                "blocktime": result.get("blocktime"),
            }
            return str(slim)

        elif tool_name == "check_mempool":
            result = _rpc("getmempoolentry", [tool_input["txid"]])
            if "error" in result:
                return f"Transaction NOT in mempool: {result['error']}"
            # Keep only the fields relevant for diagnosis
            slim = {
                "vsize": result.get("vsize"),
                "fee_sat": round(result.get("fees", {}).get("base", 0) * 1e8),
                "fee_rate_sat_vbyte": round(
                    result.get("fees", {}).get("base", 0) * 1e8 / result["vsize"], 2
                ) if result.get("vsize") else "?",
                "time": result.get("time"),
                "bip125_replaceable": result.get("bip125-replaceable"),
                "ancestor_count": result.get("ancestorcount"),
                "ancestor_size": result.get("ancestorsize"),
            }
            return f"Transaction IS in mempool: {slim}"

        elif tool_name == "get_fee_rate":
            # Batch all 3 estimatesmartfee calls into one HTTP request
            results = _rpc_batch([
                ("estimatesmartfee", [1]),
                ("estimatesmartfee", [6]),
                ("estimatesmartfee", [144]),
            ])
            def to_sat(r):
                if isinstance(r, dict) and "feerate" in r:
                    return round(r["feerate"] * 1e8 / 1000, 2)
                return "unavailable"
            fast, normal, slow = results[0], results[1], results[2]
            return (
                f"Fee rates (sat/vbyte): "
                f"fast(1-block)={to_sat(fast)}, "
                f"normal(6-block)={to_sat(normal)}, "
                f"slow(144-block)={to_sat(slow)}"
            )

        elif tool_name == "get_mempool_info":
            result = _rpc("getmempoolinfo", [])
            if "error" in result:
                return f"[RPC error] {result['error']}"
            slim = {
                "tx_count": result.get("size"),
                "total_bytes": result.get("bytes"),
                "min_fee_rate_sat_vbyte": round(result.get("mempoolminfee", 0) * 1e8 / 1000, 2),
                "total_fee_btc": result.get("total_fee"),
                "full_rbf": result.get("fullrbf"),
            }
            return str(slim)

        elif tool_name == "get_block_count":
            result = _rpc("getblockcount", [])
            if isinstance(result, int):
                return f"Current block height: {result}"
            return str(result)

        return f"[Unknown tool: {tool_name}]"
