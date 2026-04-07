"""EVM on-chain query agent using web3.py."""
import json
from config import settings
from agents.onchain.base import BaseOnChainAgent

try:
    from web3 import Web3
    _w3 = Web3(Web3.HTTPProvider(settings.evm_rpc_url)) if settings.evm_rpc_url else None
except ImportError:
    _w3 = None


class EVMOnChainAgent(BaseOnChainAgent):

    @property
    def chain(self) -> str:
        return "evm"

    @property
    def tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "get_transaction",
                "description": "Get an EVM transaction by hash. Returns status, gas, from/to, value, block.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "tx_hash": {"type": "string", "description": "Transaction hash (0x...)"},
                    },
                    "required": ["tx_hash"],
                },
            },
            {
                "name": "get_transaction_receipt",
                "description": "Get EVM transaction receipt. Reveals success/failure, gas used, logs/events.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "tx_hash": {"type": "string", "description": "Transaction hash (0x...)"},
                    },
                    "required": ["tx_hash"],
                },
            },
            {
                "name": "get_pending_txs",
                "description": "Get pending transactions from an address in the txpool.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "EVM address (0x...)"},
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_gas_price",
                "description": "Get current gas price and EIP-1559 base fee.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "call_contract",
                "description": "Read-only call to an EVM contract method (eth_call). Use for checking contract state.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "contract_address": {"type": "string", "description": "Contract address (0x...)"},
                        "abi_fragment": {"type": "string", "description": "JSON ABI for the method, e.g. [{\"name\":\"getOrder\",...}]"},
                        "method": {"type": "string", "description": "Method name to call"},
                        "params": {
                            "type": "array",
                            "description": "Method parameters as JSON array",
                            "items": {},
                        },
                    },
                    "required": ["contract_address", "abi_fragment", "method"],
                },
            },
        ]

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        if _w3 is None:
            return "[web3.py not installed or EVM_RPC_URL not configured]"

        try:
            if tool_name == "get_transaction":
                tx = _w3.eth.get_transaction(tool_input["tx_hash"])
                return json.dumps(dict(tx), default=str)

            elif tool_name == "get_transaction_receipt":
                receipt = _w3.eth.get_transaction_receipt(tool_input["tx_hash"])
                if receipt is None:
                    return "Transaction not yet mined (receipt is null)"
                return json.dumps(dict(receipt), default=str)

            elif tool_name == "get_pending_txs":
                txpool = _w3.manager.request_blocking("txpool_content", [])
                pending = txpool.get("pending", {}).get(tool_input["address"].lower(), {})
                return json.dumps(pending, default=str) if pending else "No pending transactions found"

            elif tool_name == "get_gas_price":
                gas_price = _w3.eth.gas_price
                block = _w3.eth.get_block("latest")
                base_fee = block.get("baseFeePerGas", "N/A")
                return (
                    f"Gas price: {_w3.from_wei(gas_price, 'gwei')} Gwei | "
                    f"Base fee: {_w3.from_wei(base_fee, 'gwei') if base_fee != 'N/A' else 'N/A'} Gwei"
                )

            elif tool_name == "call_contract":
                abi = json.loads(tool_input["abi_fragment"])
                contract = _w3.eth.contract(
                    address=Web3.to_checksum_address(tool_input["contract_address"]),
                    abi=abi,
                )
                fn = getattr(contract.functions, tool_input["method"])
                params = tool_input.get("params", [])
                result = fn(*params).call()
                return str(result)

        except Exception as e:
            return f"[RPC error: {type(e).__name__}: {e}]"

        return f"[Unknown tool: {tool_name}]"
