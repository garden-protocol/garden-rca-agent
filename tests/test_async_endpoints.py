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
