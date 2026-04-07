"""Bitcoin on-chain query agent using Bitcoin RPC."""
import httpx
from config import settings
from agents.onchain.base import BaseOnChainAgent


def _rpc(method: str, params: list) -> dict:
    """Make a Bitcoin RPC call."""
    payload = {"jsonrpc": "1.0", "id": "rca", "method": method, "params": params}
    try:
        resp = httpx.post(
            settings.bitcoin_rpc_url,
            json=payload,
            auth=(settings.bitcoin_rpc_user, settings.bitcoin_rpc_pass),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            return {"error": data["error"]}
        return data.get("result", {})
    except Exception as e:
        return {"error": str(e)}


class BitcoinOnChainAgent(BaseOnChainAgent):

    @property
    def chain(self) -> str:
        return "bitcoin"

    @property
    def tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "get_transaction",
                "description": "Get details of a Bitcoin transaction by txid. Returns status, confirmations, inputs, outputs.",
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
                "description": "Check if a transaction is in the mempool. Returns mempool entry details or not-found.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "txid": {"type": "string", "description": "Bitcoin transaction ID to check in mempool"},
                    },
                    "required": ["txid"],
                },
            },
            {
                "name": "get_fee_rate",
                "description": "Get current Bitcoin network fee rates (sat/vbyte) for fast, normal, and slow confirmation targets.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "get_address_utxos",
                "description": "Get unspent transaction outputs (UTXOs) for a Bitcoin address.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "Bitcoin address to check UTXOs for"},
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_mempool_info",
                "description": "Get general mempool stats: size, bytes, min fee rate.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "get_transaction":
            result = _rpc("getrawtransaction", [tool_input["txid"], True])
            return str(result)

        elif tool_name == "check_mempool":
            result = _rpc("getmempoolentry", [tool_input["txid"]])
            if "error" in result:
                return f"Transaction NOT in mempool: {result['error']}"
            return f"Transaction IS in mempool: {result}"

        elif tool_name == "get_fee_rate":
            fast = _rpc("estimatesmartfee", [1])
            normal = _rpc("estimatesmartfee", [6])
            slow = _rpc("estimatesmartfee", [144])
            # feerate is in BTC/kB, convert to sat/vbyte
            def to_sat(r):
                if "feerate" in r:
                    return round(r["feerate"] * 1e8 / 1000, 2)
                return "unavailable"
            return f"Fee rates (sat/vbyte): fast(1-block)={to_sat(fast)}, normal(6-block)={to_sat(normal)}, slow(144-block)={to_sat(slow)}"

        elif tool_name == "get_address_utxos":
            result = _rpc("scantxoutset", ["start", [f"addr({tool_input['address']})"]])
            return str(result)

        elif tool_name == "get_mempool_info":
            result = _rpc("getmempoolinfo", [])
            return str(result)

        return f"[Unknown tool: {tool_name}]"
