"""
Known-issue knowledge base: persistence + smart retrieval.

Two responsibilities:

  1. record_*()  — after a diagnosis, distill it into a `KnownIssue` and persist
                   it to a JSON file (keyed by a stable fingerprint hash, so the
                   same recurring issue is de-duplicated and its occurrence count
                   bumped rather than appended).

  2. match()     — given a new order's `KnownIssueFingerprint`, score it against
                   every stored issue (weighted on chain-pair / service / state /
                   assets / tag-overlap / error-signature similarity) and return
                   the best hit above a confidence threshold so the orchestrator
                   can short-circuit the expensive LLM pipeline.

Single-replica deployment, like jobs.py: a plain JSON file on disk, no DB.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path

from models.known_issue import (
    CONFIDENCE_LABEL_TO_FLOAT,
    KnownIssue,
    KnownIssueFingerprint,
    KnownIssueMatch,
)
from config import settings

logger = logging.getLogger("rca-agent.known_issues")

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "known_issues.json"

# ── Scoring weights (sum to 1.0 when every component is present) ──────────────
_W_CHAIN = 0.30
_W_SERVICE = 0.18
_W_STATE = 0.14
_W_ASSETS = 0.16
_W_TAGS = 0.12
_W_SIGNATURE = 0.10


# ── Text normalization ────────────────────────────────────────────────────────

_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
_NUM_RE = re.compile(r"\d[\d,\.]*")
_WS_RE = re.compile(r"\s+")


def normalize_signature(text: str) -> str:
    """Mask volatile tokens (addresses, tx hashes, amounts) so two reports about
    the *same* class of failure produce comparable signatures."""
    if not text:
        return ""
    t = text.lower()
    t = _HEX_RE.sub("<addr>", t)
    t = _NUM_RE.sub("<n>", t)
    t = _WS_RE.sub(" ", t).strip()
    return t[:240]


# Keyword → tag, scanned over the probable cause to enrich the fingerprint.
_CAUSE_TAG_KEYWORDS: list[tuple[str, str]] = [
    ("liquidit", "liquidity"),
    ("balance", "gas-balance"),
    ("gas", "gas-balance"),
    ("fund", "gas-balance"),
    ("confirmation", "confirmation-pending"),
    ("price", "price-fluctuation"),
    ("deadline", "deadline"),
    ("blacklist", "blacklist"),
    ("watcher", "watcher"),
    ("rpc", "rpc"),
    ("filled", "filled-amount"),
    ("refund", "refund"),
    ("not initiat", "not-initiated"),
    ("never initiat", "not-initiated"),
    ("redeem", "redeem"),
    ("timelock", "timelock"),
    ("congest", "congestion"),
    ("nonce", "nonce"),
]


def _cause_tags(text: str) -> list[str]:
    low = (text or "").lower()
    return sorted({tag for kw, tag in _CAUSE_TAG_KEYWORDS if kw in low})


def _service_for_state(state_value: str, dst_is_initiated: bool) -> str:
    """Mirror orchestrator._build_alert_from_order's chain→service mapping."""
    if state_value == "UserRedeemPending":
        return "relayer"
    # Refunded / DestInitPending / SolverRedeemPending all point at the executor.
    return "executor"


def _structural_tags(
    state_value: str,
    src_chain: str,
    dst_chain: str,
    src_asset: str,
    dst_asset: str,
    service: str,
    is_blacklisted: bool,
) -> list[str]:
    tags = {
        f"state:{state_value.lower()}",
        f"service:{service}",
        f"src-chain:{src_chain}",
        f"dst-chain:{dst_chain}",
        f"route:{src_chain}->{dst_chain}",
    }
    if src_asset:
        tags.add(f"src-asset:{src_asset.lower()}")
    if dst_asset:
        tags.add(f"dst-asset:{dst_asset.lower()}")
    if is_blacklisted:
        tags.add("blacklist")
    return sorted(tags)


# ── Fingerprint construction ──────────────────────────────────────────────────

def build_fingerprint(
    order,                      # OrderApiResponse
    state,                      # SwapState
    src_chain: str,
    dst_chain: str,
    *,
    probable_cause: str = "",
) -> KnownIssueFingerprint:
    """Build the structural signature for an order.

    At query time `probable_cause` is empty (we haven't diagnosed yet) so the
    error-signature component is omitted and the remaining weights renormalize.
    At persist time we pass the diagnosed cause to enrich tags + signature.
    """
    result = order.result
    co = result.create_order
    src = result.source_swap
    dst = result.destination_swap
    state_value = state.value if hasattr(state, "value") else str(state)

    service = _service_for_state(state_value, dst.is_initiated)
    tags = _structural_tags(
        state_value, src_chain, dst_chain, src.asset, dst.asset,
        service, bool(co.additional_data.is_blacklisted),
    )
    if probable_cause:
        tags = sorted(set(tags) | set(_cause_tags(probable_cause)))

    return KnownIssueFingerprint(
        state=state_value,
        service=service,
        source_chain=src_chain,
        destination_chain=dst_chain,
        source_asset=src.asset,
        destination_asset=dst.asset,
        tags=tags,
        error_signature=normalize_signature(probable_cause),
    )


def _issue_id(fp: KnownIssueFingerprint) -> str:
    """Stable id: same structural class + same cause signature → same record."""
    basis = "|".join([
        fp.state, fp.source_chain, fp.destination_chain, fp.service,
        fp.source_asset, fp.destination_asset, fp.error_signature,
    ])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score(q: KnownIssueFingerprint, iss: KnownIssue) -> tuple[float, list[str]]:
    got = 0.0
    total = 0.0
    matched: list[str] = []

    # Chain pair
    total += _W_CHAIN
    cp = (q.source_chain == iss.source_chain) + (q.destination_chain == iss.destination_chain)
    got += _W_CHAIN * (cp / 2)
    if cp == 2:
        matched.append("chain-pair")

    # Service
    total += _W_SERVICE
    if q.service and iss.service and q.service == iss.service:
        got += _W_SERVICE
        matched.append("service")

    # State
    total += _W_STATE
    if q.state and q.state == iss.state:
        got += _W_STATE
        matched.append("state")

    # Assets
    total += _W_ASSETS
    ap = (q.source_asset == iss.source_asset) + (q.destination_asset == iss.destination_asset)
    got += _W_ASSETS * (ap / 2)
    if ap == 2:
        matched.append("assets")

    # Tag overlap (Jaccard)
    total += _W_TAGS
    if q.tags or iss.tags:
        qs, is_ = set(q.tags), set(iss.tags)
        union = len(qs | is_) or 1
        jac = len(qs & is_) / union
        got += _W_TAGS * jac
        if jac >= 0.5:
            matched.append("tags")

    # Error-signature similarity — only when both sides have one.
    if q.error_signature and iss.error_signature:
        total += _W_SIGNATURE
        ratio = SequenceMatcher(None, q.error_signature, iss.error_signature).ratio()
        got += _W_SIGNATURE * ratio
        if ratio >= 0.6:
            matched.append("signature")

    score = got / total if total else 0.0
    return score, matched


# ── Store ─────────────────────────────────────────────────────────────────────

class KnownIssueStore:
    """JSON-file-backed known-issue knowledge base (single-replica, thread-safe)."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else _DEFAULT_PATH
        self._lock = threading.Lock()

    # ── Persistence primitives ──────────────────────────────────────────────
    def _load(self) -> dict[str, KnownIssue]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text() or "{}")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Known-issue KB unreadable (%s); starting empty", exc)
            return {}
        out: dict[str, KnownIssue] = {}
        for key, val in raw.items():
            try:
                out[key] = KnownIssue.model_validate(val)
            except Exception as exc:  # noqa: BLE001 — skip a single corrupt row
                logger.warning("Skipping corrupt KB entry %s: %s", key, exc)
        return out

    def _save(self, issues: dict[str, KnownIssue]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v.model_dump(mode="json") for k, v in issues.items()}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.path)  # atomic on POSIX

    def all(self) -> list[KnownIssue]:
        return list(self._load().values())

    # ── Recording ────────────────────────────────────────────────────────────
    def record(
        self,
        fingerprint: KnownIssueFingerprint,
        *,
        probable_cause: str,
        findings: str = "",
        remediation: list[str] | None = None,
        severity: str = "",
        confidence: float = 0.0,
        confidence_label: str = "",
        origin: str = "llm",
        short_circuitable: bool = True,
        metadata: dict | None = None,
    ) -> KnownIssue:
        """Insert a new issue or merge into the existing one with the same id."""
        issue_id = _issue_id(fingerprint)
        now = datetime.now(timezone.utc)
        with self._lock:
            issues = self._load()
            existing = issues.get(issue_id)
            if existing is not None:
                existing.last_seen = now
                existing.occurrence_count += 1
                # Keep the highest-confidence framing of the cause.
                if confidence > existing.confidence:
                    existing.probable_cause = probable_cause
                    existing.findings = findings or existing.findings
                    existing.remediation = remediation or existing.remediation
                    existing.severity = severity or existing.severity
                    existing.confidence = confidence
                    existing.confidence_label = confidence_label or existing.confidence_label
                if metadata:
                    existing.metadata.update(metadata)
                issues[issue_id] = existing
                self._save(issues)
                return existing

            issue = KnownIssue(
                issue_id=issue_id,
                first_seen=now,
                last_seen=now,
                occurrence_count=1,
                state=fingerprint.state,
                service=fingerprint.service,
                source_chain=fingerprint.source_chain,
                destination_chain=fingerprint.destination_chain,
                source_asset=fingerprint.source_asset,
                destination_asset=fingerprint.destination_asset,
                tags=fingerprint.tags,
                error_signature=fingerprint.error_signature,
                probable_cause=probable_cause,
                findings=findings,
                remediation=remediation or [],
                severity=severity,
                confidence=confidence,
                confidence_label=confidence_label,
                origin=origin,  # type: ignore[arg-type]
                short_circuitable=short_circuitable,
                metadata=metadata or {},
            )
            issues[issue_id] = issue
            self._save(issues)
            return issue

    def record_deterministic(
        self, order, state, src_chain: str, dst_chain: str, *, reason: str,
    ) -> KnownIssue:
        """Persist a cheap deterministic early-return diagnosis.

        Stored for knowledge/analytics but NOT short-circuitable: these depend on
        live state (liquidity, balances, confirmations) and are re-checked each run.
        """
        fp = build_fingerprint(order, state, src_chain, dst_chain, probable_cause=reason)
        return self.record(
            fp,
            probable_cause=reason,
            findings=reason,
            confidence=0.85,
            confidence_label="high",
            origin="deterministic",
            short_circuitable=False,
            metadata={"order_id": order.result.order_id or order.result.create_order.create_id},
        )

    def record_rca(self, order, state, src_chain: str, dst_chain: str, report) -> KnownIssue:
        """Persist an LLM specialist RCAReport as a short-circuitable known issue."""
        cause = report.root_cause
        fp = build_fingerprint(order, state, src_chain, dst_chain, probable_cause=cause)
        conf = CONFIDENCE_LABEL_TO_FLOAT.get(report.confidence, 0.5)
        return self.record(
            fp,
            probable_cause=cause,
            findings=report.raw_analysis or report.investigation_summary or cause,
            remediation=list(report.remediation_actions or []),
            severity=report.severity,
            confidence=conf,
            confidence_label=report.confidence,
            origin="llm",
            short_circuitable=True,
            metadata={
                "order_id": report.order_id,
                "affected_components": report.affected_components,
                "next_action": report.next_action,
            },
        )

    # ── Retrieval ──────────────────────────────────────────────────────────────
    def match(
        self,
        fingerprint: KnownIssueFingerprint,
        threshold: float = 0.72,
        *,
        require_short_circuitable: bool = False,
    ) -> KnownIssueMatch | None:
        """Return the best stored issue scoring at/above `threshold`, else None."""
        best: KnownIssueMatch | None = None
        for iss in self._load().values():
            if require_short_circuitable and not iss.short_circuitable:
                continue
            score, matched_on = _score(fingerprint, iss)
            if score >= threshold and (best is None or score > best.score):
                best = KnownIssueMatch(issue=iss, score=round(score, 4), matched_on=matched_on)
        return best


# Module-level singleton, mirroring how the rest of the codebase uses `settings`.
store = KnownIssueStore(getattr(settings, "known_issues_path", "") or None)
