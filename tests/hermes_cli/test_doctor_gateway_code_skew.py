"""Tests for hermes_cli.doctor._check_gateway_code_skew.

CLI-side counterpart to tests/test_code_skew.py's in-process guard tests:
this proves `hermes doctor` surfaces a stale-code gateway using the
persisted ``code_boot_rev`` status field (gateway/status.py) rather than
querying the live gateway process directly.
"""

import gateway.status as gw_status
import hermes_cli.doctor as doctor


class TestCheckGatewayCodeSkew:
    def test_no_running_gateway_is_silent(self, monkeypatch):
        monkeypatch.setattr(gw_status, "read_runtime_status", lambda: None)

        issues: list = []
        doctor._check_gateway_code_skew(issues)

        assert issues == []

    def test_dead_recorded_pid_is_silent(self, monkeypatch):
        """A stale record whose PID is gone doesn't describe a live gateway —
        nothing to compare against disk."""
        monkeypatch.setattr(
            gw_status, "read_runtime_status", lambda: {"code_boot_rev": "abc1234567"}
        )
        monkeypatch.setattr(gw_status, "runtime_status_pid_is_live", lambda record: False)

        issues: list = []
        doctor._check_gateway_code_skew(issues)

        assert issues == []

    def test_fresh_gateway_reports_ok(self, monkeypatch, capsys):
        monkeypatch.setattr(
            gw_status, "read_runtime_status", lambda: {"code_boot_rev": "abc1234567"}
        )
        monkeypatch.setattr(gw_status, "runtime_status_pid_is_live", lambda record: True)
        import gateway.code_skew as code_skew

        monkeypatch.setattr(code_skew, "current_disk_rev_short", lambda root=None: "abc1234567")

        issues: list = []
        doctor._check_gateway_code_skew(issues)

        assert issues == []
        assert "fresh code" in capsys.readouterr().out.lower()

    def test_stale_gateway_reports_actionable_issue(self, monkeypatch, capsys):
        monkeypatch.setattr(
            gw_status, "read_runtime_status", lambda: {"code_boot_rev": "abc1234567"}
        )
        monkeypatch.setattr(gw_status, "runtime_status_pid_is_live", lambda record: True)
        import gateway.code_skew as code_skew

        monkeypatch.setattr(code_skew, "current_disk_rev_short", lambda root=None: "def4567890")

        issues: list = []
        doctor._check_gateway_code_skew(issues)

        assert len(issues) == 1
        assert "abc1234567" in issues[0]
        assert "def4567890" in issues[0]
        assert "hermes gateway restart" in issues[0]
        out = capsys.readouterr().out
        assert "stale" in out.lower()

    def test_missing_boot_rev_is_silent(self, monkeypatch):
        """Older gateway processes (pre-this-feature) never wrote
        code_boot_rev — must not be treated as a mismatch."""
        monkeypatch.setattr(gw_status, "read_runtime_status", lambda: {})
        monkeypatch.setattr(gw_status, "runtime_status_pid_is_live", lambda record: True)

        issues: list = []
        doctor._check_gateway_code_skew(issues)

        assert issues == []
