"""Tests for hermes_cli.update_cmd's post-restart fresh-boot-rev verification.

Companion to tests/test_code_skew.py: that module proves the gateway's own
in-process drift detection; this proves the CLI-side polling that `hermes
update` uses to confirm a restarted gateway actually booted the code that
was just pulled (see gateway/status.py::write_runtime_status's
``code_boot_rev`` field and update_cmd.py's `_poll_gateway_code_boot_rev`).
"""

import json
import threading
import time

import hermes_cli.update_cmd as update_mod


def _write_status(home, **fields):
    payload = {
        "pid": 12345,
        "kind": "hermes-gateway",
        "argv": ["hermes", "gateway", "run"],
        "start_time": 1000,
        "updated_at": "2026-07-31T00:00:00Z",
    }
    payload.update(fields)
    (home / "gateway_state.json").write_text(json.dumps(payload), encoding="utf-8")


class TestPollGatewayCodeBootRev:
    def test_matches_immediately_when_already_fresh(self, tmp_path):
        _write_status(tmp_path, code_boot_rev="abc1234567")

        matched, last_seen = update_mod._poll_gateway_code_boot_rev(
            tmp_path, "abc1234567", timeout=2.0, poll_interval=0.05
        )

        assert matched is True
        assert last_seen == "abc1234567"

    def test_times_out_on_persistent_mismatch(self, tmp_path):
        _write_status(tmp_path, code_boot_rev="OLDOLDOLD1")

        matched, last_seen = update_mod._poll_gateway_code_boot_rev(
            tmp_path, "NEWNEWNEW1", timeout=0.3, poll_interval=0.05
        )

        assert matched is False
        assert last_seen == "OLDOLDOLD1"

    def test_returns_none_seen_when_no_status_file_exists(self, tmp_path):
        matched, last_seen = update_mod._poll_gateway_code_boot_rev(
            tmp_path, "NEWNEWNEW1", timeout=0.2, poll_interval=0.05
        )

        assert matched is False
        assert last_seen is None

    def test_picks_up_a_late_write_before_timeout(self, tmp_path):
        """The new gateway process may take a moment after restart to finish
        booting and write its status file — the poll must not give up early."""
        _write_status(tmp_path, code_boot_rev="OLDOLDOLD1")

        def _write_fresh_after_delay():
            time.sleep(0.15)
            _write_status(tmp_path, code_boot_rev="NEWNEWNEW1")

        writer = threading.Thread(target=_write_fresh_after_delay)
        writer.start()
        try:
            matched, last_seen = update_mod._poll_gateway_code_boot_rev(
                tmp_path, "NEWNEWNEW1", timeout=3.0, poll_interval=0.05
            )
        finally:
            writer.join()

        assert matched is True
        assert last_seen == "NEWNEWNEW1"
