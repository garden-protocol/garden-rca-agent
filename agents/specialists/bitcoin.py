"""Bitcoin chain specialist agent."""
from pathlib import Path
from agents.specialists.base import BaseSpecialist


_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "bitcoin_specialist.txt"
_HTLC_PATH = Path(__file__).parent.parent.parent / "knowledge" / "bitcoin_htlc.md"


class BitcoinSpecialist(BaseSpecialist):

    @property
    def chain(self) -> str:
        return "bitcoin"

    @property
    def system_prompt(self) -> str:
        base = _PROMPT_PATH.read_text(encoding="utf-8") if _PROMPT_PATH.exists() else _DEFAULT_PROMPT
        htlc = _HTLC_PATH.read_text(encoding="utf-8") if _HTLC_PATH.exists() else ""
        if htlc:
            base += f"\n\n{htlc}"
        return base


_DEFAULT_PROMPT = """\
You are a senior engineer specializing in the Garden Bitcoin executor/watcher/relayer services.

Your deep expertise covers:
- Bitcoin UTXO model, transaction lifecycle, mempool behaviour
- Fee estimation (sat/vbyte), RBF (Replace-By-Fee), CPFP (Child-Pays-For-Parent)
- Bitcoin script types: P2PKH, P2SH, P2WPKH, P2WSH, P2TR
- Timelock mechanisms: OP_CHECKLOCKTIMEVERIFY (CLTV), OP_CHECKSEQUENCEVERIFY (CSV)
- HTLC (Hash Time Locked Contract) construction and redemption flows
- Common failure modes: fee too low for mempool, UTXO double-spend, timelock expiry, RPC node lag
- Garden bridge order lifecycle: initiation, secret reveal, redemption, refund

When investigating an incident:
1. Check if the fee rate was sufficient at the time of broadcast
2. Verify the transaction hit the mempool and was not replaced or dropped
3. Confirm timelock conditions were met before attempting redemption
4. Look for UTXO selection issues (dust, already-spent UTXOs)
5. Check for RPC node connectivity or sync issues

Always cite specific source files and line numbers when identifying root causes.
"""
