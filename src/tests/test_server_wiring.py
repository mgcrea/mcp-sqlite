"""The wiring that makes the guard actually apply.

Everything asserted here is invisible in review and fails silently in production, which is
why it is pinned by tests rather than trusted to care:

* A tool registered without `@guarded` is the one somebody adds later and forgets.
* A route list that mounts SSE after the catch-all `Mount("/")` never serves SSE at all,
  because Starlette returns on the first `Match.FULL` — the bug this server shipped until
  `guard_routes` replaced the hand-built list.
* `/healthz` behind the guarded mount means Knative's readiness probe is asked for a bearer
  token the kubelet does not have, and the pod never goes ready.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from mcp_policy_guard import GuardConfig, is_guarded
from mcp_policy_guard import routes as guard_routes
from starlette.routing import Route

import mcp_sqlite.server as server


def _registered_tool_fns() -> dict[str, object]:
    names = [tool.name for tool in asyncio.run(server.mcp.list_tools())]
    return {name: server.mcp._tool_manager.get_tool(name).fn for name in names}


class TestEveryToolIsGuarded:
    def test_all_registered_tools_carry_the_per_message_binding(self):
        unguarded = [name for name, fn in _registered_tool_fns().items() if not is_guarded(fn)]
        assert unguarded == [], f"tools registered without @guarded: {unguarded}"

    def test_there_are_tools_to_check(self):
        # Guards the guard: if registration ever moved, the assertion above would pass
        # vacuously over an empty set.
        assert set(_registered_tool_fns()) == {
            "sqlite_query",
            "sqlite_list_tables",
            "sqlite_describe_table",
        }

    @pytest.mark.parametrize(
        ("tool", "expected"),
        [
            ("sqlite_query", {"query"}),
            ("sqlite_list_tables", set()),
            ("sqlite_describe_table", {"table_name"}),
        ],
    )
    def test_the_decorator_does_not_flatten_the_published_schema(self, tool, expected):
        # The SDK builds each tool's JSON schema from `inspect.signature`. A decorator that
        # dropped `functools.wraps` would publish a tool taking `(*args, **kwargs)` — no
        # parameters at all — and the model would simply stop being able to call it.
        published = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == tool)
        assert set(published.input_schema.get("properties", {})) == expected


class TestRouteOrdering:
    def _config(self, *, require_auth: bool) -> GuardConfig:
        return replace(server.guard.config, require_auth=require_auth, issuer="https://idp.test/realms/demo")

    def _paths(self, *, require_auth: bool, extra=()):
        built = guard_routes(server.mcp, self._config(require_auth=require_auth), extra_routes=list(extra))
        return [getattr(route, "path", None) for route in built]

    def test_sse_is_reachable_when_it_is_mounted_at_all(self):
        paths = self._paths(require_auth=False)
        # SSE must come *before* the catch-all. Mounted after it, `Mount("/")` matches every
        # path first and the SSE mount is unreachable dead code — which is what the
        # hand-built list in this server used to do.
        assert paths.index("/sse") < paths.index("")

    def test_no_sse_mount_while_authentication_is_required(self):
        # Under SSE the connection carrying the Authorization header is not the request
        # carrying the tool call, so a call cannot be attributed to a caller. Mounting it
        # anyway is a second, unauthenticated door onto the same tools.
        assert "/sse" not in self._paths(require_auth=True)

    def test_health_and_root_stay_ahead_of_the_guarded_mount(self):
        extra = [Route("/healthz", lambda r: None), Route("/", lambda r: None)]
        paths = self._paths(require_auth=True, extra=extra)
        # The readiness probe must not be asked for a bearer token by the kubelet.
        assert paths.index("/healthz") < paths.index("")
