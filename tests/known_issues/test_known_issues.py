"""Unit tests for the known-issue knowledge base — offline, no network/LLM."""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tools.known_issues import KnownIssueStore, build_fingerprint, normalize_signature
from models.known_issue import KnownIssueFingerprint
from models.investigate import SwapState
from models.order import OrderApiResponse, OrderResult, SwapData, CreateOrder, AdditionalData


def _store(tmp_path) -> KnownIssueStore:
    return KnownIssueStore(tmp_path / "kb.json")


def _fp(**over) -> KnownIssueFingerprint:
    base = dict(
        state="DestInitPending",
        service="executor",
        source_chain="evm",
        destination_chain="bitcoin",
        source_asset="ethereum:USDC",
        destination_asset="bitcoin:BTC",
        tags=["state:destinitpending", "route:evm->bitcoin", "liquidity"],
        error_signature="",
    )
    base.update(over)
    return KnownIssueFingerprint(**base)


# ── Store round-trip ──────────────────────────────────────────────────────────

def test_record_then_match_returns_known_issue(tmp_path):
    store = _store(tmp_path)
    store.record(
        _fp(error_signature=normalize_signature("solver had insufficient liquidity")),
        probable_cause="solver had insufficient liquidity on destination",
        findings="full analysis...",
        confidence=0.9,
        confidence_label="high",
        origin="llm",
    )
    # A fresh query for the same structural fingerprint matches.
    match = store.match(_fp(), threshold=0.72)
    assert match is not None
    assert match.score >= 0.72
    assert "chain-pair" in match.matched_on
    assert match.issue.probable_cause.startswith("solver had insufficient")


def test_no_match_below_threshold(tmp_path):
    store = _store(tmp_path)
    store.record(_fp(), probable_cause="x", confidence=0.9, origin="llm")
    # Completely different chain pair + state + assets → far below threshold.
    other = _fp(
        state="UserRedeemPending", service="relayer",
        source_chain="solana", destination_chain="tron",
        source_asset="solana:SOL", destination_asset="tron:USDT",
        tags=["state:userredeempending"],
    )
    assert store.match(other, threshold=0.72) is None


def test_record_dedups_and_bumps_occurrence(tmp_path):
    store = _store(tmp_path)
    fp = _fp(error_signature=normalize_signature("gas balance too low"))
    a = store.record(fp, probable_cause="gas balance too low", confidence=0.6, origin="llm")
    b = store.record(fp, probable_cause="gas balance too low", confidence=0.6, origin="llm")
    assert a.issue_id == b.issue_id
    assert b.occurrence_count == 2
    assert len(store.all()) == 1
    # first_seen preserved, last_seen advanced (or equal).
    assert b.first_seen <= b.last_seen


def test_higher_confidence_upgrades_cause(tmp_path):
    store = _store(tmp_path)
    fp = _fp(error_signature=normalize_signature("watcher lag"))
    store.record(fp, probable_cause="low-conf guess", confidence=0.3, origin="llm")
    store.record(fp, probable_cause="watcher lagging behind blocks", confidence=0.9, origin="llm")
    iss = store.all()[0]
    assert iss.probable_cause == "watcher lagging behind blocks"
    assert iss.confidence == 0.9


# ── Short-circuit gating ────────────────────────────────────────────────────────

def test_deterministic_issue_not_short_circuitable(tmp_path):
    store = _store(tmp_path)
    store.record(_fp(), probable_cause="blacklisted", confidence=0.85,
                 origin="deterministic", short_circuitable=False)
    # Visible to a plain match...
    assert store.match(_fp(), threshold=0.72) is not None
    # ...but excluded when the caller requires short-circuitable entries.
    assert store.match(_fp(), threshold=0.72, require_short_circuitable=True) is None


def test_persistence_survives_new_store_instance(tmp_path):
    path = tmp_path / "kb.json"
    KnownIssueStore(path).record(_fp(), probable_cause="x", confidence=0.9, origin="llm")
    reloaded = KnownIssueStore(path)
    assert len(reloaded.all()) == 1


# ── Fingerprint construction from an order ──────────────────────────────────────

def _swap(chain, asset, **over) -> SwapData:
    base = dict(
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        swap_id="s_" + chain,
        chain=chain,
        asset=asset,
        initiator="0xinit",
        redeemer="0xredeem",
        timelock=7200,
        filled_amount="0",
        amount="1000",
        secret_hash="0xabc",
    )
    base.update(over)
    return SwapData(**base)


def _order(src_chain="ethereum", dst_chain="bitcoin",
           src_asset="ethereum:USDC", dst_asset="bitcoin:BTC",
           initiated=True) -> OrderApiResponse:
    src = _swap(src_chain, src_asset, initiate_tx_hash="0xinit_tx" if initiated else "")
    dst = _swap(dst_chain, dst_asset)
    result = OrderResult(
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        source_swap=src,
        destination_swap=dst,
        order_id="order123",
        solver_id="solver1",
    )
    return OrderApiResponse(status="ok", result=result)


def test_build_fingerprint_extracts_route_and_service(tmp_path):
    order = _order()
    fp = build_fingerprint(order, SwapState.DEST_INIT_PENDING, "evm", "bitcoin")
    assert fp.source_chain == "evm"
    assert fp.destination_chain == "bitcoin"
    assert fp.service == "executor"          # DestInitPending → executor
    assert fp.source_asset == "ethereum:USDC"
    assert "route:evm->bitcoin" in fp.tags
    assert fp.error_signature == ""          # no cause at query time


def test_build_fingerprint_user_redeem_is_relayer(tmp_path):
    fp = build_fingerprint(_order(), SwapState.USER_REDEEM_PENDING, "evm", "bitcoin")
    assert fp.service == "relayer"


def test_build_fingerprint_enriches_tags_from_cause():
    fp = build_fingerprint(
        _order(), SwapState.DEST_INIT_PENDING, "evm", "bitcoin",
        probable_cause="Solver had insufficient liquidity and gas balance was low",
    )
    assert "liquidity" in fp.tags
    assert "gas-balance" in fp.tags
    assert fp.error_signature  # populated when a cause is supplied


def test_normalize_signature_masks_volatile_tokens():
    sig = normalize_signature("Address 0xDEADBEEF moved 1,234.5 by 12%")
    assert "0xdeadbeef" not in sig
    assert "<addr>" in sig and "<n>" in sig
