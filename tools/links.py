"""
Report link auto-generation.

Given an Alert, on-chain findings, and affected_components list, emit
ReportLink entries with block-explorer and Gitea URLs that the Discord
renderer (or any other client) can hyperlink directly.

Pure / offline — no HTTP at runtime.
"""
import re
from models.alert import Alert
from models.report import ReportLink
from config import settings


_MAX_LINKS = 12

# Per-chain block-explorer URL templates (keyed by API chain name, i.e. the
# value of `alert.metadata["source_chain"]` / `destination_chain`, which come
# from the Garden orders API).
_EXPLORER_TX_TEMPLATES: dict[str, str] = {
    "bitcoin":       "https://mempool.space/tx/{hash}",
    "bitcoin_testnet":"https://mempool.space/testnet/tx/{hash}",
    "ethereum":      "https://etherscan.io/tx/{hash}",
    "arbitrum":      "https://arbiscan.io/tx/{hash}",
    "base":          "https://basescan.org/tx/{hash}",
    "bnbchain":      "https://bscscan.com/tx/{hash}",
    "bsc":           "https://bscscan.com/tx/{hash}",
    "citrea":        "https://explorer.citrea.xyz/tx/{hash}",
    "botanix":       "https://botanixscan.io/tx/{hash}",
    "monad":         "https://monadexplorer.com/tx/{hash}",
    "hyperevm":      "https://hyperliquid.cloud.blockscout.com/tx/{hash}",
    "solana":        "https://solscan.io/tx/{hash}",
    "tron":          "https://tronscan.org/#/transaction/{hash}",
    "starknet":      "https://starkscan.co/tx/{hash}",
    "litecoin":      "https://litecoinspace.org/tx/{hash}",
    "alpen":         "https://explorer.testnet.alpenlabs.io/tx/{hash}",
}

# Tx hash patterns. We intentionally over-match then let the per-chain template
# decide — a bare "0x<64hex>" is valid for EVM / Bitcoin; Solana uses base58.
_EVM_TX_RE   = re.compile(r"\b0x[a-fA-F0-9]{64}\b")
_BTC_TX_RE   = re.compile(r"\b[a-fA-F0-9]{64}\b")
_SOL_SIG_RE  = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{43,88}\b")

# `<repo>/<path>:L<line>` or `<path>:L<line>` — capture (maybe_repo, path, line)
_COMPONENT_RE = re.compile(
    r"^(?:(?P<repo>[^/\s:]+)/)?(?P<path>[\w./\-]+):L(?P<line>\d+)$"
)


def _order_link(alert: Alert) -> ReportLink:
    return ReportLink(
        label="Order on Garden Finance",
        url=f"https://api.garden.finance/orders/id/{alert.order_id}",
        kind="order",
    )


def _tx_link_for_chain(chain_api_name: str, tx_hash: str) -> ReportLink | None:
    template = _EXPLORER_TX_TEMPLATES.get((chain_api_name or "").lower())
    if not template:
        return None
    return ReportLink(
        label=f"{chain_api_name}: {tx_hash[:10]}…",
        url=template.format(hash=tx_hash),
        kind="tx",
    )


def _looks_like_hash_for_chain(value: str, chain: str) -> bool:
    """Rough check: does `value` look like a tx hash for `chain`?"""
    if not value:
        return False
    c = (chain or "").lower()
    if c in {"solana"}:
        return bool(_SOL_SIG_RE.fullmatch(value))
    if c in {"bitcoin", "bitcoin_testnet", "litecoin", "alpen"}:
        return bool(_BTC_TX_RE.fullmatch(value))
    # EVM-family + starknet + tron all accept 0x-prefixed 64-hex in practice.
    return bool(_EVM_TX_RE.fullmatch(value))


def _extract_hashes_from_text(text: str) -> list[str]:
    """All plausible tx hashes (EVM-style and Solana-style) from free text."""
    if not text:
        return []
    hashes = set()
    hashes.update(_EVM_TX_RE.findall(text))
    hashes.update(_SOL_SIG_RE.findall(text))
    return list(hashes)


def _code_link_for_component(chain: str, component: str) -> ReportLink | None:
    """
    Turn `evm-executor/src/main.rs:L42` (or similar) into a Gitea URL if
    the repo name matches one of the chain's configured gitea_repos.
    Returns None on no match / missing gitea config.
    """
    if not settings.gitea_url:
        return None
    m = _COMPONENT_RE.match(component.strip())
    if not m:
        return None
    maybe_repo = m.group("repo") or ""
    path = m.group("path")
    line = m.group("line")

    # Map the chain's component names to gitea (repo_name, branch).
    try:
        gitea_repos = settings.gitea_repos(chain)
    except Exception:
        return None
    # Exact match against either the gitea repo name or the component key
    repo_name = None
    branch = None
    if maybe_repo:
        for comp_key, (gname, gbranch) in gitea_repos.items():
            if maybe_repo == gname or maybe_repo == comp_key:
                repo_name = gname
                branch = gbranch
                break
    if not repo_name:
        return None

    gitea_url = settings.gitea_url.rstrip("/")
    org = settings.gitea_org
    url = f"{gitea_url}/{org}/{repo_name}/src/branch/{branch}/{path}#L{line}"
    return ReportLink(label=component, url=url, kind="code")


def generate_report_links(
    alert: Alert,
    onchain_evidence: dict | None,
    affected_components: list[str],
) -> list[ReportLink]:
    """
    Build the link list for the report. Pure; no network.

    Sources of hashes:
      - alert.metadata keys containing "tx_hash"
      - free text inside onchain_evidence["findings"]
    Chain resolution for each hash:
      - prefer metadata["source_chain"] vs destination_chain via key name (src_*, dst_*, source_*, destination_*)
      - fallback: try every explorer template, pick the first that matches hash-shape
    """
    links: list[ReportLink] = []
    seen_urls: set[str] = set()

    def _add(link: ReportLink | None) -> None:
        if link is None:
            return
        if link.url in seen_urls:
            return
        if len(links) >= _MAX_LINKS:
            return
        seen_urls.add(link.url)
        links.append(link)

    # 1. Order link always first
    _add(_order_link(alert))

    metadata = alert.metadata or {}
    source_chain = metadata.get("source_chain", "")
    destination_chain = metadata.get("destination_chain", "")

    # 2. Tx hashes from metadata keys
    for key, value in metadata.items():
        if not isinstance(value, str) or not value:
            continue
        if "tx_hash" not in key:
            continue
        # Heuristic: key starts with "src_" or "source_" → source chain.
        # key starts with "dst_" or "destination_" → destination chain.
        lk = key.lower()
        if lk.startswith("src_") or lk.startswith("source_"):
            chain = source_chain
        elif lk.startswith("dst_") or lk.startswith("destination_"):
            chain = destination_chain
        else:
            chain = source_chain or destination_chain
        if not _looks_like_hash_for_chain(value, chain):
            continue
        _add(_tx_link_for_chain(chain, value))

    # 3. Tx hashes from free text in onchain findings — try both chains
    if onchain_evidence and isinstance(onchain_evidence, dict):
        text = str(onchain_evidence.get("findings", ""))
        candidate_chains = [c for c in (source_chain, destination_chain) if c]
        for h in _extract_hashes_from_text(text):
            # Try source chain first, then destination
            added = False
            for c in candidate_chains:
                if _looks_like_hash_for_chain(h, c):
                    link = _tx_link_for_chain(c, h)
                    if link:
                        _add(link)
                        added = True
                        break
            # If neither chain accepts the shape, skip.
            _ = added

    # 4. Code links from affected components
    internal_chain = alert.chain
    for comp in affected_components or []:
        if not isinstance(comp, str):
            continue
        _add(_code_link_for_component(internal_chain, comp))

    return links
