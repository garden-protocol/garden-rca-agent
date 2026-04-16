"""
Unit tests for the time-window computation in agents/log_agent.run().

Tests run without hitting the provider or Loki — we stub them both.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from models.alert import Alert
import agents.log_agent as log_agent


def _make_alert(created_at: datetime, deadline_unix: int | None) -> Alert:
    metadata = {"order_created_at": created_at.isoformat()}
    if deadline_unix is not None:
        metadata["deadline"] = deadline_unix
    return Alert(
        order_id="test-order",
        alert_type="missed_init",
        chain="evm",
        service="executor",
        network="mainnet",
        message="test",
        timestamp=created_at,
        metadata=metadata,
    )


def _run_capturing_user_message(alert: Alert) -> str:
    """Stub the provider; return the user message text passed to it."""
    captured = {}

    fake_response = MagicMock()
    fake_response.usage = MagicMock(
        input_tokens=0, output_tokens=0,
        cache_read_tokens=0, cache_creation_tokens=0,
    )
    fake_response.stop_reason = "end_turn"
    fake_response.tool_calls = []
    fake_response.text = "stubbed"

    fake_provider = MagicMock()

    def capture_call(*args, **kwargs):
        messages = kwargs.get("messages") or (args[-1] if args else [])
        for m in messages:
            if m.get("role") == "user":
                captured["user_message"] = m["content"]
        return fake_response

    fake_provider.create_message.side_effect = capture_call

    with patch.object(log_agent, "get_provider", return_value=fake_provider):
        log_agent.run(alert)

    return captured["user_message"]


def test_window_starts_5min_before_created_at():
    created = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
    deadline = int((created + timedelta(hours=2)).timestamp())
    msg = _run_capturing_user_message(_make_alert(created, deadline))
    assert "2026-04-10T11:55:00" in msg


def test_window_ends_30min_after_deadline_when_deadline_in_past():
    """If deadline is in the past, window_end = deadline + 30min (still capped by now)."""
    created = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
    deadline_dt = created + timedelta(hours=2)
    deadline = int(deadline_dt.timestamp())

    fake_now = deadline_dt + timedelta(hours=1)

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    with patch.object(log_agent, "datetime", FakeDateTime):
        msg = _run_capturing_user_message(_make_alert(created, deadline))

    assert "2026-04-10T14:30:00" in msg


def test_window_capped_at_now_when_deadline_is_future():
    """If deadline is in the future, window_end = now (not deadline + 30min)."""
    created = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
    future_deadline = int((created + timedelta(hours=10)).timestamp())

    fake_now = created + timedelta(hours=1)

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    with patch.object(log_agent, "datetime", FakeDateTime):
        msg = _run_capturing_user_message(_make_alert(created, future_deadline))

    assert "2026-04-10T13:00:00" in msg  # fake_now
    assert "2026-04-10T22" not in msg   # future deadline not used


def test_window_falls_back_to_4h_when_no_deadline():
    """No deadline in metadata → window_end = created_at + 4h (capped at now)."""
    created = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
    fake_now = created + timedelta(hours=8)  # far in future so cap doesn't apply

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    with patch.object(log_agent, "datetime", FakeDateTime):
        msg = _run_capturing_user_message(_make_alert(created, None))

    assert "2026-04-10T16:00:00" in msg  # created + 4h
