"""Every data route says whose data it is (multi-tenancy.md §5).

Ownership is enforced at ~130 routes, and the failure mode of "remember to
scope this lookup" is that one is forgotten, nothing goes red, and the bug is
found by somebody seeing another account's work. `test_tenant_isolation.py`
asks "can Vera reach Archer's?" of the surfaces it knows about; this asks a
structural question no new route can avoid: does it take a scope at all?

A route that legitimately has none is listed below **with the reason**. Adding
to that list is a deliberate act with a justification attached; forgetting to
scope a new route is not.
"""

from __future__ import annotations

import inspect

from fastapi.routing import APIRoute

from server.main import app

# The dependency aliases that answer "whose rows may this request see". Matched
# by name because `from __future__ import annotations` leaves annotations as
# strings in some routers and as objects in others, so both spellings appear.
SCOPE_MARKERS = (
    "ScopeUser",
    "scope_user_id",
    "CurrentUser",
    "current_user",
    "AdminUser",
    "require_admin",
    "OperatorUser",
    "operator_user",
    "ViewerUser",
    "viewer_user_id",
)

# (method, path) -> why this one carries no account scope.
UNSCOPED_BY_DESIGN: dict[tuple[str, str], str] = {
    ("GET", "/health"): "liveness; says nothing about anybody's data",
    ("GET", "/api/backends"): (
        "which harnesses this box has installed — a property of the install"
    ),
    ("GET", "/api/auth/state"): (
        "whether this install has accounts yet; the sign-in screen cannot ask "
        "the right question without it, and it is answered before anyone is "
        "signed in"
    ),
    ("POST", "/api/auth/login"): "the request that establishes who you are",
    ("POST", "/api/auth/register"): "same, with an invite code",
    ("POST", "/api/auth/bootstrap"): (
        "creates the first account; authenticated with the install's own token, "
        "which is the only credential that exists at that moment"
    ),
    ("POST", "/api/auth/rotate"): (
        "rotates the install token, which exists only before accounts do "
        "(token-rotation.md)"
    ),
    ("GET", "/api/connectors/catalog"): (
        "what this install can connect to: the kinds and whether each has an "
        "OAuth client registered. Install-level and the same for everybody"
    ),
    ("GET", "/api/connectors/{kind}/oauth-client"): (
        "the same install-level config, per kind, minus the secret — you cannot "
        "decide whether to connect GitHub without being told this box has a "
        "GitHub app"
    ),
    ("GET", "/api/connectors/oauth/callback"): (
        "the provider's browser redirect. It carries no bearer of ours at all; "
        "`state` is the CSRF anchor and the account comes from the pending "
        "login the authenticated `oauth/start` created"
    ),
    # The application surface authorises through `_viewer`, which returns the
    # account to scope the row lookup with — or the sentinel for an app's own
    # scoped token, which already names one application. A `ScopeUser` here
    # would refuse the app's own credential.
    ("GET", "/apps/{app_id}"): "scoped by `_viewer`",
    ("GET", "/apps/{app_id}/{path}"): "scoped by `_viewer`",
    ("GET", "/apps/{app_id}/api/{path}"): "scoped by `_viewer`",
    ("POST", "/apps/{app_id}/api/{path}"): "scoped by `_viewer`",
    ("PUT", "/apps/{app_id}/api/{path}"): "scoped by `_viewer`",
    ("PATCH", "/apps/{app_id}/api/{path}"): "scoped by `_viewer`",
    ("DELETE", "/apps/{app_id}/api/{path}"): "scoped by `_viewer`",
    ("GET", "/apps/{app_id}/agent/agents"): "scoped by `_viewer`",
    ("GET", "/apps/{app_id}/agent/conversations"): "scoped by `_viewer`",
    ("GET", "/apps/{app_id}/agent/conversations/{conversation_id}"): (
        "scoped by `_viewer`"
    ),
    ("DELETE", "/apps/{app_id}/agent/conversations/{conversation_id}"): (
        "scoped by `_viewer`"
    ),
    ("POST", "/apps/{app_id}/agent/ask"): "scoped by `_viewer`",
    ("POST", "/apps/{app_id}/agent/chat"): "scoped by `_viewer`",
}


def _routes() -> list[tuple[str, str, bool]]:
    out: list[tuple[str, str, bool]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        annotations = " ".join(
            f"{p.annotation} {p.default}"
            for p in inspect.signature(route.endpoint).parameters.values()
        )
        scoped = any(m in annotations for m in SCOPE_MARKERS)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append((method, route.path, scoped))
    return out


def test_every_route_is_scoped_or_listed_with_a_reason():
    missing = [
        f"{method} {path}"
        for method, path, scoped in _routes()
        if not scoped and (method, _normalise(path)) not in UNSCOPED_BY_DESIGN
    ]
    assert not missing, (
        "these routes take no account scope and are not in "
        "UNSCOPED_BY_DESIGN:\n  " + "\n  ".join(sorted(missing)) + "\n\n"
        "Add `user_id: ScopeUser = None` and pass it to the lookup, or list the "
        "route above with the reason it needs none."
    )


def test_the_exemption_list_has_no_stale_entries():
    """A route that has since been scoped, renamed or removed must leave the
    list, or the list stops meaning anything."""
    live = {(method, _normalise(path)) for method, path, scoped in _routes() if not scoped}
    stale = sorted(f"{m} {p}" for (m, p) in UNSCOPED_BY_DESIGN if (m, p) not in live)
    assert not stale, (
        "UNSCOPED_BY_DESIGN lists routes that are now scoped or gone:\n  "
        + "\n  ".join(stale)
    )


def _normalise(path: str) -> str:
    """`{path:path}` and `{path}` are the same route to a reader."""
    return path.replace(":path}", "}")


def test_the_scope_markers_are_real_dependencies():
    """The names above are matched textually, so a typo would silently exempt
    everything. Each one has to exist in `server.deps`."""
    from server import deps

    for name in SCOPE_MARKERS:
        assert hasattr(deps, name), f"{name} is not in server.deps"
