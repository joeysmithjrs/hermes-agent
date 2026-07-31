"""Watcher-level tests for the proactive code-skew self-heal (gateway/run.py
::_code_skew_watcher), mirroring tests/gateway/test_scale_to_zero_watcher.py's
approach: exercise the real GatewayRunner method against a lightweight
stand-in rather than booting a full gateway.

Companion to tests/test_code_skew.py (pure detect_code_skew()/message
helpers) and tests/hermes_cli/test_update_code_skew_verification.py
(update-side verification) — this proves the gateway itself notices drift
and self-heals instead of waiting for the reactive `/model` guard.
"""

from __future__ import annotations

import asyncio

import pytest

import gateway.code_skew as code_skew
from gateway.run import GatewayRunner


def _runner(monkeypatch):
    r = GatewayRunner.__new__(GatewayRunner)
    r._running = True
    r._code_skew_restart_requested = False
    r._restart_calls = []
    monkeypatch.setattr(
        r, "request_restart", lambda **kw: r._restart_calls.append(kw) or True, raising=False
    )
    return r


@pytest.mark.asyncio
async def test_watcher_restarts_once_on_detected_skew(monkeypatch):
    r = _runner(monkeypatch)
    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("abc1234567", "def4567890"))

    task = asyncio.create_task(r._code_skew_watcher(interval=0.01))
    await asyncio.sleep(0.1)
    r._running = False
    await asyncio.wait_for(task, timeout=2)

    assert r._restart_calls == [{"via_service": True}]
    assert r._code_skew_restart_requested is True


@pytest.mark.asyncio
async def test_watcher_does_not_restart_when_no_skew(monkeypatch):
    r = _runner(monkeypatch)
    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: None)

    task = asyncio.create_task(r._code_skew_watcher(interval=0.01))
    await asyncio.sleep(0.1)
    r._running = False
    await asyncio.wait_for(task, timeout=2)

    assert r._restart_calls == []
    assert r._code_skew_restart_requested is False


@pytest.mark.asyncio
async def test_watcher_does_not_re_request_after_first_trigger(monkeypatch):
    """Once a restart has been requested, further ticks (e.g. a slow drain
    still in progress) must not pile on more restart requests."""
    r = _runner(monkeypatch)
    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("abc1234567", "def4567890"))

    task = asyncio.create_task(r._code_skew_watcher(interval=0.01))
    await asyncio.sleep(0.15)
    r._running = False
    await asyncio.wait_for(task, timeout=2)

    assert len(r._restart_calls) == 1
