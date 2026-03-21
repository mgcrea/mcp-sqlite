"""Tests for audit logging."""

import json

from mcp_sqlite.audit import _sanitize_params, audit_log


class TestSanitizeParams:
    def test_normal_params_pass_through(self):
        params = {"query": "SELECT 1", "schema": "public"}
        assert _sanitize_params(params) == params

    def test_sensitive_keys_redacted(self):
        params = {"password": "secret123", "api_key": "abc", "token": "xyz"}
        sanitized = _sanitize_params(params)
        assert sanitized["password"] == "***REDACTED***"
        assert sanitized["api_key"] == "***REDACTED***"
        assert sanitized["token"] == "***REDACTED***"

    def test_long_strings_truncated(self):
        long_val = "x" * 600
        sanitized = _sanitize_params({"query": long_val})
        assert len(sanitized["query"]) < 600
        assert sanitized["query"].endswith("...[truncated]")

    def test_short_strings_not_truncated(self):
        val = "x" * 500
        sanitized = _sanitize_params({"query": val})
        assert sanitized["query"] == val

    def test_non_string_values_pass_through(self):
        params = {"count": 42, "flag": True, "items": [1, 2, 3]}
        assert _sanitize_params(params) == params


class TestAuditLog:
    def test_success_logs(self, capsys):
        with audit_log("test_tool", {"query": "SELECT 1"}):
            pass

        output = capsys.readouterr().out
        log = json.loads(output.strip().split("\n")[0])
        assert log["tool"] == "test_tool"
        assert log["success"] is True
        assert "duration_ms" in log
        assert "timestamp" in log

    def test_failure_logs_and_reraises(self, capsys):
        try:
            with audit_log("test_tool", {"query": "bad"}):
                raise ValueError("boom")
        except ValueError:
            pass

        output = capsys.readouterr().out
        log = json.loads(output.strip().split("\n")[0])
        assert log["success"] is False
        assert log["error"] == "boom"

    def test_params_sanitized_in_log(self, capsys):
        with audit_log("test_tool", {"password": "secret"}):
            pass

        output = capsys.readouterr().out
        log = json.loads(output.strip().split("\n")[0])
        assert log["params"]["password"] == "***REDACTED***"
