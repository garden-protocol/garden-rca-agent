"""
Integration-ish tests for the new /investigate + /jobs endpoints.

Uses FastAPI TestClient. The orchestrator is stubbed so tests run offline.
"""
import os
import sys
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jobs
from config import settings


@pytest.fixture
def secret():
    return settings.server_secret or "test-secret"


@pytest.fixture
def client(monkeypatch, secret):
    # Stub settings.server_secret if blank
    monkeypatch.setattr(settings, "server_secret", secret)
    jobs._JOBS.clear()

    # Stub orchestrator.investigate to return a trivial response quickly
    from models.investigate import InvestigateResponse, SwapState
    import agents.orchestrator as orchestrator

    def _fake_investigate(order_id: str, force: bool):
        return InvestigateResponse(
            order_id=order_id,
            state=SwapState.UNKNOWN,
            source_chain="evm",
            destination_chain="bitcoin",
            early_return=True,
            reason=f"stub for {order_id} force={force}",
            generated_at=datetime.now(timezone.utc),
            duration_seconds=0.01,
        )

    monkeypatch.setattr(orchestrator, "investigate", _fake_investigate)

    import main
    return TestClient(main.app)


def test_post_investigate_forbidden_on_wrong_secret(client):
    resp = client.post("/investigate/wrong", json={"order_id": "x"})
    assert resp.status_code == 403


def test_post_investigate_returns_202_and_job_id(client, secret):
    resp = client.post(f"/investigate/{secret}", json={"order_id": "abc"})
    assert resp.status_code == 202
    data = resp.json()
    assert "job_id" in data
    assert data["status"] == "queued"
    assert data["poll_url"].endswith(data["job_id"])


def test_get_job_forbidden_on_wrong_secret(client, secret):
    post = client.post(f"/investigate/{secret}", json={"order_id": "abc"})
    job_id = post.json()["job_id"]
    resp = client.get(f"/jobs/wrong/{job_id}")
    assert resp.status_code == 403


def test_get_job_404_on_unknown(client, secret):
    resp = client.get(f"/jobs/{secret}/does-not-exist")
    assert resp.status_code == 404


def test_post_and_poll_happy_path(client, secret):
    post = client.post(f"/investigate/{secret}", json={"order_id": "happy"})
    job_id = post.json()["job_id"]

    # Poll until done (stub finishes nearly instantly but the task scheduling
    # may take a tick)
    for _ in range(20):
        r = client.get(f"/jobs/{secret}/{job_id}")
        assert r.status_code == 200
        body = r.json()
        if body["status"] == "done":
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"job never finished; last body={body}")

    assert body["status"] == "done"
    assert body["result"]["order_id"] == "happy"
    assert body["result"]["early_return"] is True
    assert body["result"]["reason"].startswith("stub for happy")


def test_job_response_has_no_raw_control_chars(client, secret, monkeypatch):
    """
    Response bodies must be strict-JSON-parseable. A RCAReport with raw
    newlines inside raw_analysis (a common case — the specialist's markdown
    block contains literal newlines) must come back with those newlines
    escaped, not as raw 0x0a bytes.
    """
    from datetime import datetime as _dt, timezone as _tz
    from models.investigate import InvestigateResponse, SwapState
    from models.report import RCAReport
    import agents.orchestrator as orchestrator

    def _fake_with_newlines(order_id, force):
        rca = RCAReport(
            order_id=order_id,
            chain="evm", service="executor", network="mainnet",
            root_cause="test",
            affected_components=["a"],
            remediation_actions=["b"],
            severity="low", confidence="high",
            raw_analysis="```json\n{\n  \"k\": \"v\"\n}\n```",  # raw newlines
            generated_at=_dt.now(_tz.utc), duration_seconds=0.1,
        )
        return InvestigateResponse(
            order_id=order_id, state=SwapState.REFUNDED,
            source_chain="evm", destination_chain="bitcoin",
            early_return=False, rca_report=rca,
            generated_at=_dt.now(_tz.utc), duration_seconds=0.1,
        )

    monkeypatch.setattr(orchestrator, "investigate", _fake_with_newlines)

    post = client.post(f"/investigate/{secret}", json={"order_id": "nl"})
    job_id = post.json()["job_id"]
    for _ in range(30):
        r = client.get(f"/jobs/{secret}/{job_id}")
        if r.json()["status"] == "done":
            break
        time.sleep(0.05)

    # Raw bytes must not contain any control chars (0x00..0x1f, except tab/none here).
    raw = r.content
    bad = [i for i, b in enumerate(raw) if b < 0x20]
    assert not bad, (
        f"response contains {len(bad)} raw control chars at offsets "
        f"{bad[:5]} — strict JSON parsers will reject it"
    )
    # And strict json.loads must succeed
    import json as _stdjson
    _stdjson.loads(raw)  # strict=True by default; raises if invalid


def test_post_and_poll_failure_path(client, secret, monkeypatch):
    import agents.orchestrator as orchestrator

    def _boom(order_id, force):
        raise RuntimeError("induced failure")

    monkeypatch.setattr(orchestrator, "investigate", _boom)

    post = client.post(f"/investigate/{secret}", json={"order_id": "boom"})
    job_id = post.json()["job_id"]

    for _ in range(20):
        r = client.get(f"/jobs/{secret}/{job_id}")
        body = r.json()
        if body["status"] == "failed":
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"job never failed; last body={body}")

    assert "induced failure" in body["error"]
