"""
Unit tests for tools.links.generate_report_links.
Fully offline — no HTTP, no settings mutation (uses defaults).
"""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from models.alert import Alert
from tools.links import generate_report_links


def _alert(source_chain="ethereum", destination_chain="solana", metadata_extra=None):
    md = {
        "source_chain": source_chain,
        "destination_chain": destination_chain,
    }
    if metadata_extra:
        md.update(metadata_extra)
    return Alert(
        order_id="abc123",
        alert_type="missed_init",
        chain="evm",
        service="executor",
        network="mainnet",
        message="test",
        timestamp=datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc),
        metadata=md,
    )


def test_emits_order_link_always():
    links = generate_report_links(_alert(), None, [])
    kinds = {l.kind for l in links}
    assert "order" in kinds
    order = [l for l in links if l.kind == "order"][0]
    assert "abc123" in order.url


def test_emits_tx_link_for_ethereum_init_hash():
    alert = _alert(
        source_chain="ethereum",
        metadata_extra={"src_initiate_tx_hash": "0x" + "a" * 64},
    )
    links = generate_report_links(alert, None, [])
    tx_links = [l for l in links if l.kind == "tx"]
    assert any("etherscan.io/tx/0x" + "a" * 64 in l.url for l in tx_links)


def test_emits_tx_link_for_solana_hash_on_destination_chain():
    alert = _alert(
        source_chain="ethereum",
        destination_chain="solana",
        metadata_extra={"dst_initiate_tx_hash": "5K" + "x" * 42},
    )
    links = generate_report_links(alert, None, [])
    tx_links = [l for l in links if l.kind == "tx"]
    assert any("solscan.io" in l.url for l in tx_links)


def test_tx_link_from_onchain_findings_text():
    alert = _alert(source_chain="ethereum")
    onchain = {"findings": "Tx 0x" + "b" * 64 + " reverted at block 123"}
    links = generate_report_links(alert, onchain, [])
    tx_links = [l for l in links if l.kind == "tx"]
    assert any("0x" + "b" * 64 in l.url for l in tx_links)


def test_no_tx_link_for_unknown_chain():
    alert = _alert(source_chain="zzzchain", metadata_extra={"src_initiate_tx_hash": "0x" + "c" * 64})
    links = generate_report_links(alert, None, [])
    tx_links = [l for l in links if l.kind == "tx"]
    assert tx_links == []


def test_dedupe_by_url():
    alert = _alert(
        source_chain="ethereum",
        metadata_extra={"src_initiate_tx_hash": "0x" + "d" * 64},
    )
    # Same hash appears in both metadata and findings
    onchain = {"findings": "tx 0x" + "d" * 64 + " done"}
    links = generate_report_links(alert, onchain, [])
    tx_links = [l for l in links if l.kind == "tx"]
    # Only one tx link for this hash
    hashes_in_urls = [l.url for l in tx_links if "d" * 64 in l.url]
    assert len(hashes_in_urls) == 1


def test_affected_component_code_link():
    """`evm-executor/src/main.rs:L42` → Gitea URL if gitea_url configured."""
    from config import settings
    original = settings.gitea_url
    try:
        settings.gitea_url = "https://git.example.com"
        alert = _alert()
        links = generate_report_links(
            alert,
            None,
            ["evm-executor/src/main.rs:L42"],
        )
        code_links = [l for l in links if l.kind == "code"]
        # Must contain the file path AND line anchor
        assert any(l.url.endswith("#L42") for l in code_links), (
            f"expected code link ending in #L42, got {[l.url for l in code_links]}"
        )
        assert any("src/main.rs" in l.url for l in code_links)
    finally:
        settings.gitea_url = original


def test_malformed_component_is_skipped_silently():
    """Garbage component strings should not raise."""
    alert = _alert()
    links = generate_report_links(
        alert,
        None,
        ["this is not a file path", "also:notrightshape", ""],
    )
    # No crashes; order + maybe something else, but no code link
    assert all(l.kind != "code" for l in links)


def test_cap_at_12_links():
    alert = _alert(
        source_chain="ethereum",
        metadata_extra={
            f"hash_{i}": "0x" + str(i).zfill(64)
            for i in range(20)
        },
    )
    # Add a long tx-hashy findings blob too
    onchain = {"findings": " ".join("0x" + str(i).zfill(64) for i in range(100, 120))}
    links = generate_report_links(alert, onchain, [])
    assert len(links) <= 12
