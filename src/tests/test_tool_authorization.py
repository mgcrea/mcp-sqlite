"""How the tools behave once the guard is wired in.

The call *ordering* and the resources submitted are what is under test here, not the policy
semantics — those live in mcp-policy-guard's own suite. Specifically: nothing may open the
database before the decision has been made, a denial must not be distinguishable from absence
where that distinction would be an enumeration oracle, and a deployment nobody has configured
must behave exactly as it did before the guard was a dependency.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from unittest.mock import MagicMock

import pytest
from mcp_policy_guard import UNDETERMINED, Decision, GuardConfig, PolicyDenied, PolicyUnavailable
from structlog.testing import capture_logs

import mcp_sqlite.tools.sqlite as tools

ALLOWED = Decision(decision="allow", effect="allow", enforcing=True, reason="ok")

GUARD_ENV = (
    "MCP_REQUIRE_AUTH",
    "MCP_AUTH_ISSUER",
    "MCP_TOOL_ID",
    "MCP_POLICY_URL",
    "MCP_POLICY_FAIL_MODE",
)


class StubMCP:
    """Captures the tool functions `register_sqlite_tools` decorates."""

    def __init__(self):
        self.tools: dict[str, object] = {}

    def tool(self, *_args, **_kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture()
def db_path():
    """A real SQLite database — the tools are thin enough that faking one proves less."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name

    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE payroll (id INTEGER PRIMARY KEY, salary REAL)")
    conn.execute("INSERT INTO users (id, name) VALUES (1, 'Alice')")
    conn.execute("INSERT INTO payroll (id, salary) VALUES (1, 100.0)")
    conn.commit()
    conn.close()

    yield path
    os.unlink(path)


@pytest.fixture()
def call(db_path, monkeypatch):
    """Invoke one tool against the temporary database, in read-only mode."""
    monkeypatch.setenv("SQLITE_DB_PATH", db_path)
    monkeypatch.setenv("SQLITE_READONLY", "true")

    async def _call(tool_name: str, **kwargs):
        stub = StubMCP()
        tools.register_sqlite_tools(stub)
        return await stub.tools[tool_name](**kwargs)

    return _call


@pytest.fixture()
def never_opens(monkeypatch):
    """`sqlite3.connect` raising, so a check that ran too late is a test failure.

    The seam is the stdlib call rather than the module's own `_get_connection`, which is a
    closure created inside `register_sqlite_tools` and cannot be reached from here.
    """
    connect = MagicMock(side_effect=AssertionError("opened the database on a denial"))
    monkeypatch.setattr(tools.sqlite3, "connect", connect)
    return connect


@pytest.fixture(autouse=True)
def _allow_by_default(monkeypatch):
    monkeypatch.setattr(tools.guard, "require", lambda *_a, **_k: ALLOWED)
    monkeypatch.setattr(tools.guard, "filter_resources", lambda _kind, values, **_kw: list(values))


def use_the_real_guard(monkeypatch) -> None:
    """Undo the module-level stubs, so the guard's own unconfigured path decides.

    Stubbing `require` proves the tool calls it; only the real object proves what it answers
    when nobody has configured anything, which is the state every deployment starts in.
    """
    for var in GUARD_ENV:
        monkeypatch.delenv(var, raising=False)
    guard_class = type(tools.guard)
    monkeypatch.setattr(tools.guard, "config", GuardConfig.from_env())
    monkeypatch.setattr(tools.guard, "require", guard_class.require.__get__(tools.guard))
    monkeypatch.setattr(tools.guard, "filter_resources", guard_class.filter_resources.__get__(tools.guard))


def submitted(require: MagicMock) -> set[str]:
    """The `kind:value` set handed to `guard.require`."""
    return {str(resource) for resource in require.call_args.args[1]}


def last_audit_record(logs: list[dict]) -> dict:
    return next(entry for entry in reversed(logs) if str(entry.get("event", "")).startswith("tool_call"))


class TestTheResourcesSubmitted:
    """What a policy rule about this tool gets written about.

    The database file and the tables, as two independent resources. `evaluate()` requires
    every one to be allowed, so the caller must hold both — and a rule can still say "any
    table, but only in this database". Changing this set changes the decision, which is why
    it is asserted exactly rather than loosely.
    """

    async def test_a_query_submits_the_database_and_every_table_it_reads(self, call, db_path, monkeypatch):
        require = MagicMock(return_value=ALLOWED)
        monkeypatch.setattr(tools.guard, "require", require)

        await call("sqlite_query", query="SELECT u.name FROM users u JOIN payroll p ON u.id = p.id")

        assert submitted(require) == {
            f"path_prefix:{db_path}",
            "sql_table:users",
            "sql_table:payroll",
        }

    async def test_a_join_cannot_launder_access(self, call, monkeypatch):
        # Both tables go in, and the platform denies the whole call if either is denied. A
        # query joining `users` and `payroll` *is* a payroll read.
        require = MagicMock(return_value=ALLOWED)
        monkeypatch.setattr(tools.guard, "require", require)

        await call("sqlite_query", query="SELECT * FROM users JOIN payroll ON 1 = 1")

        assert "sql_table:payroll" in submitted(require)

    def test_the_database_path_is_absolute(self, db_path, monkeypatch):
        # Two deployments of this image differ only by their file. A relative path would
        # produce two spellings of one database depending on the working directory, and a
        # rule matches textually.
        monkeypatch.setenv("SQLITE_DB_PATH", os.path.basename(db_path))
        assert os.path.isabs(tools.database_resource().value)

    async def test_the_database_is_submitted_even_when_no_table_is_read(self, call, db_path, monkeypatch):
        # `SELECT 1` touches no table, but it is still a call against this database, and
        # that is a real claim rather than a placeholder.
        require = MagicMock(return_value=ALLOWED)
        monkeypatch.setattr(tools.guard, "require", require)

        await call("sqlite_query", query="SELECT 1")

        assert submitted(require) == {f"path_prefix:{db_path}"}

    async def test_describe_table_submits_the_database_and_the_named_table(self, call, db_path, monkeypatch):
        require = MagicMock(return_value=ALLOWED)
        monkeypatch.setattr(tools.guard, "require", require)

        await call("sqlite_describe_table", table_name="Users")

        # Lowercased, the way `extract_referenced_tables` produces it and the way a rule is
        # authored — or the same table would be authorized under two spellings.
        assert submitted(require) == {f"path_prefix:{db_path}", "sql_table:users"}

    async def test_list_tables_gates_on_the_database_alone(self, call, db_path, monkeypatch):
        # The table names are filtered afterwards, not required up front: requiring them
        # would refuse the listing rather than narrow it.
        require = MagicMock(return_value=ALLOWED)
        monkeypatch.setattr(tools.guard, "require", require)

        await call("sqlite_list_tables")

        assert submitted(require) == {f"path_prefix:{db_path}"}

    async def test_each_tool_submits_its_own_function_name(self, call, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(tools.guard, "require", lambda fn, _resources=(): (seen.append(fn), ALLOWED)[1])

        await call("sqlite_query", query="SELECT * FROM users")
        await call("sqlite_describe_table", table_name="users")
        await call("sqlite_list_tables")

        # Rules scope to a function, so these names are part of the contract.
        assert seen == ["sqlite_query", "sqlite_describe_table", "sqlite_list_tables"]


class TestNothingHappensBeforeTheDecision:
    async def test_a_denied_query_never_opens_the_database(self, call, monkeypatch, never_opens):
        # The most important assertion in this file. A check that ran *after* the query
        # would have already read the data it exists to prevent reading.
        monkeypatch.setattr(
            tools.guard,
            "require",
            MagicMock(side_effect=PolicyDenied("denied", resources=("sql_table:payroll",))),
        )
        result = await call("sqlite_query", query="SELECT * FROM payroll")
        assert "do not have access to payroll" in result
        never_opens.assert_not_called()

    async def test_a_denied_describe_never_opens_the_database(self, call, monkeypatch, never_opens):
        monkeypatch.setattr(tools.guard, "require", MagicMock(side_effect=PolicyDenied("denied")))
        assert await call("sqlite_describe_table", table_name="payroll") == "Table 'payroll' not found"
        never_opens.assert_not_called()

    async def test_a_denied_listing_never_opens_the_database(self, call, monkeypatch, never_opens):
        monkeypatch.setattr(tools.guard, "require", MagicMock(side_effect=PolicyDenied("denied")))
        assert "do not have permission" in await call("sqlite_list_tables")
        never_opens.assert_not_called()

    async def test_read_only_validation_still_runs_first(self, call, monkeypatch, never_opens):
        # sqlglot parses DELETE perfectly happily — enumeration is not policy, so the
        # deny-list gate has to stay, and has to stay first.
        require = MagicMock()
        monkeypatch.setattr(tools.guard, "require", require)
        result = await call("sqlite_query", query="DELETE FROM users")
        assert result.startswith("Error:")
        require.assert_not_called()
        never_opens.assert_not_called()


class TestAnUnprovableReadSet:
    """Extraction failure goes to `UNDETERMINED`, never to `[]` and never to an early return.

    `[]` would claim the statement touches nothing, converting the extractor's failure into
    an allow. An early return would refuse regardless of whether policy exists — and
    `evaluate()` checks `policy_enabled` before it inspects the sentinel, so the two answers
    differ precisely on the unconfigured deployments this image only ever has.
    """

    async def test_the_sentinel_is_passed_rather_than_an_empty_list(self, call, monkeypatch):
        require = MagicMock(return_value=ALLOWED)
        monkeypatch.setattr(tools.guard, "require", require)

        await call("sqlite_query", query="SELECT * FROM json_each('[1,2]')")

        assert require.call_args.args[1] is UNDETERMINED

    async def test_a_refused_sentinel_stops_the_query(self, call, monkeypatch, never_opens):
        monkeypatch.setattr(tools.guard, "require", MagicMock(side_effect=PolicyDenied("undetermined")))
        result = await call("sqlite_query", query="SELECT * FROM json_each('[1,2]')")
        # The extraction error, not the policy reason: it says what could not be analyzed,
        # which is the part the model can act on.
        assert "cannot be authorized" in result
        never_opens.assert_not_called()

    async def test_with_no_policy_the_statement_runs_as_it_always_did(self, call, monkeypatch):
        use_the_real_guard(monkeypatch)
        # `UNDETERMINED` short-circuits to allow when nothing is configured, which is what
        # keeps a parser limitation from becoming an outage on a deployment with no policy.
        assert "Alice" in await call("sqlite_query", query="SELECT name FROM users")


class TestDiscoveryScoping:
    async def test_list_tables_hides_denied_names(self, call, monkeypatch):
        monkeypatch.setattr(
            tools.guard,
            "filter_resources",
            lambda _kind, values, **_kw: [v for v in values if v != "payroll"],
        )
        result = await call("sqlite_list_tables")
        assert "users" in result
        # No count, no placeholder, no "1 hidden" — the denied name must not be inferable
        # from the response at all.
        assert "payroll" not in result
        assert "hidden" not in result.lower()

    async def test_list_tables_looks_empty_when_everything_is_denied(self, call, monkeypatch):
        monkeypatch.setattr(tools.guard, "filter_resources", lambda _kind, _values, **_kw: [])
        # Byte-identical to a genuinely empty database.
        assert await call("sqlite_list_tables") == "No tables found in database"

    async def test_list_tables_normalizes_names_the_way_rules_are_written(self, call, monkeypatch):
        seen: list[str] = []

        def fake_filter(_kind, values, **kwargs):
            key = kwargs["key"]
            seen.extend(key(v) for v in values)
            return list(values)

        monkeypatch.setattr(tools.guard, "filter_resources", fake_filter)
        await call("sqlite_list_tables")
        # Must match what `extract_referenced_tables` produces, or a table would be listed
        # under one spelling and authorized under another.
        assert seen == ["payroll", "users"]

    async def test_describe_table_denial_is_indistinguishable_from_absence(self, call, monkeypatch):
        monkeypatch.setattr(tools.guard, "require", MagicMock(side_effect=PolicyDenied("denied")))
        denied = await call("sqlite_describe_table", table_name="payroll")

        monkeypatch.setattr(tools.guard, "require", lambda *_a, **_k: ALLOWED)
        absent = await call("sqlite_describe_table", table_name="nosuchtable")

        # One template, one substitution: the caller's own argument. Nothing else in the two
        # answers differs, so a denial cannot confirm that a table exists — the enumeration
        # oracle that filtering `sqlite_list_tables` exists to avoid, rebuilt one name at a
        # time.
        template = "Table '{}' not found"
        assert denied == template.format("payroll")
        assert absent == template.format("nosuchtable")

    async def test_the_query_tool_names_the_denied_table_on_purpose(self, call, monkeypatch):
        # The opposite of the discovery tools, and deliberately so: the model already named
        # this table, so there is no oracle to protect. Being explicit stops a retry loop.
        monkeypatch.setattr(
            tools.guard,
            "require",
            MagicMock(side_effect=PolicyDenied("denied", resources=("sql_table:payroll",))),
        )
        assert "payroll" in await call("sqlite_query", query="SELECT * FROM payroll")

    async def test_the_database_path_is_never_echoed_to_the_model(self, call, db_path, monkeypatch):
        # The model never named the file, and it is a location on the pod's filesystem.
        monkeypatch.setattr(
            tools.guard,
            "require",
            MagicMock(side_effect=PolicyDenied("denied", resources=(f"path_prefix:{db_path}",))),
        )
        assert db_path not in await call("sqlite_query", query="SELECT * FROM users")


class TestAnOutageIsNotADenial:
    """`PolicyUnavailable` subclasses `PolicyDenied`, so it fails closed — but telling
    someone they lack permission while the decision point is down sends them to request
    access they already hold."""

    async def test_the_query_tool_says_so(self, call, monkeypatch, never_opens):
        monkeypatch.setattr(tools.guard, "require", MagicMock(side_effect=PolicyUnavailable("PDP down")))
        result = await call("sqlite_query", query="SELECT * FROM users")
        assert "temporarily unavailable" in result
        assert "do not have access" not in result
        never_opens.assert_not_called()

    async def test_describe_table_does_not_claim_the_table_is_missing(self, call, monkeypatch):
        # The one denial worth distinguishing here: the answer depends on neither the table
        # nor the caller's grants, so it leaks nothing — while "not found" would tell the
        # model a table it may well be allowed to read does not exist.
        monkeypatch.setattr(tools.guard, "require", MagicMock(side_effect=PolicyUnavailable("PDP down")))
        result = await call("sqlite_describe_table", table_name="users")
        assert "temporarily unavailable" in result
        assert "not found" not in result


class TestUnconfiguredGuardIsANoOp:
    """The constraint that protects every deployment nobody has configured.

    No Knative service runs this image today, so *every* deployment it ever gets starts with
    no `MCP_*` variables at all and the guard has to be invisible to it. The failure mode is
    not hypothetical: mcp-policy-guard's own suite carries a regression test for an outage
    where a tool picked the guard up through a dependency update and started answering
    `401 Guard has no issuer configured`.
    """

    @pytest.fixture(autouse=True)
    def _unconfigured(self, monkeypatch):
        use_the_real_guard(monkeypatch)

    async def test_a_query_still_returns_its_rows(self, call):
        assert "Alice" in await call("sqlite_query", query="SELECT name FROM users")

    async def test_a_listing_still_returns_every_table(self, call):
        result = await call("sqlite_list_tables")
        assert "users" in result
        assert "payroll" in result

    async def test_describe_table_still_describes(self, call):
        assert "Table: users" in await call("sqlite_describe_table", table_name="users")

    def test_the_guard_reports_itself_inert(self):
        config = GuardConfig.from_env()
        # No bearer demanded, no PDP consulted, and `sse_allowed` stays True so an
        # unconfigured deployment keeps the transport list it advertises today.
        assert config.require_auth is False
        assert config.policy_enabled is False
        assert config.sse_allowed is True


class TestAuditShape:
    """`success` and `decision` answer different questions and both have to be recorded.

    A denied call is a call that *completed* — the tool did its job and refused — so folding
    a refusal into `success` files it alongside genuine breakage and makes both harder to
    find.
    """

    async def test_a_denial_is_audited_as_a_completed_call(self, call, monkeypatch):
        monkeypatch.setattr(
            tools.guard,
            "require",
            MagicMock(side_effect=PolicyDenied("no rule matched (default deny)")),
        )
        with capture_logs() as logs:
            await call("sqlite_query", query="SELECT * FROM payroll")

        record = last_audit_record(logs)
        assert record["decision"] == "deny"
        assert record["reason"] == "no rule matched (default deny)"
        assert record["success"] is True

    async def test_an_allowed_call_records_what_it_touched(self, call, db_path):
        with capture_logs() as logs:
            await call("sqlite_query", query="SELECT * FROM users")

        record = last_audit_record(logs)
        assert record["decision"] == "allow"
        assert set(record["resources"]) == {f"path_prefix:{db_path}", "sql_table:users"}

    async def test_a_broken_call_is_recorded_as_a_failure(self, call):
        # sqlite3 raises rather than returning an error envelope, which is why this server
        # can adopt the guard's exception-only `audit_call` unchanged. A server fronting an
        # HTTP client that *returns* `{"success": False}` would need an explicit failure
        # channel instead — see `mcp-web-search/src/mcp_web_search/audit.py`.
        with capture_logs() as logs, pytest.raises(sqlite3.OperationalError):
            await call("sqlite_query", query="SELECT * FROM nonexistent_table")

        record = last_audit_record(logs)
        assert record["success"] is False
        # The call was authorized; it was the database that said no. Two fields, two
        # questions.
        assert record["decision"] == "allow"

    async def test_a_query_is_redacted_the_way_the_guard_redacts(self, call):
        # `audit_call` redacts recursively and on whole words, which is the reason for
        # adopting it over the 63-line local copy this replaced.
        with capture_logs() as logs:
            await call("sqlite_query", query="SELECT name FROM users")

        assert last_audit_record(logs)["params"] == {"query": "SELECT name FROM users"}
