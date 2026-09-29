"""Signing in, joining by invite, and the admin surface (multi-tenancy.md §3).

The route layer, where `tests/test_users.py` covers the rules underneath. What
is worth pinning here is what only exists once there is an HTTP boundary: that
a bearer is required, that the throttle actually bites, that an admin route is
closed to a normal account, and that the one way to lock everybody out — an
admin disabling themselves — is refused.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.agent_manager import AgentManager
from server.database import Database
from server.main import app
from server.routers import agents as agents_router
from server.routers import auth as auth_router
from server.session_manager import session_manager
from server.users import UserManager


@pytest.fixture
async def client(tmp_path, monkeypatch):
    from server import crypto
    from server.config import settings

    # A master key of this test's own, so a DEK minted here cannot be wrapped
    # with the developer's real one.
    monkeypatch.setattr(settings, "master_key", "test-master-key")
    crypto._MASTER_CACHE.clear()

    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)
    auth_router.set_db(db)
    # The routes these tests reach through, bound the way main.py binds them.
    agents_router.set_manager(AgentManager(db))
    users = UserManager(db)
    deps.set_user_manager(users)
    auth_router._FAILURES.clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, users, db
    await db.close()
    session_manager.sessions.clear()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_returns_a_bearer_that_works(client):
    c, users, _db = client
    await users.create_user(username="archer", password="password1")

    res = await c.post(
        "/api/auth/login", json={"username": "archer", "password": "password1"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["username"] == "archer" and body["token"]

    who = await c.get("/api/auth/identity", headers=_bearer(body["token"]))
    assert who.status_code == 200
    assert who.json()["label"] == "archer"


@pytest.mark.asyncio
async def test_identity_needs_a_bearer_and_the_password_is_not_one(client):
    c, users, _db = client
    await users.create_user(username="archer", password="password1")

    assert (await c.get("/api/auth/identity")).status_code == 401
    assert (
        await c.get("/api/auth/identity", headers=_bearer("password1"))
    ).status_code == 401


@pytest.mark.asyncio
async def test_a_wrong_password_is_401_and_eventually_429(client):
    """The throttle is the reply to swapping a 256-bit token for something a
    person chose. Without it the login route is an offline attack with a
    network hop in front of it."""
    c, users, _db = client
    await users.create_user(username="archer", password="password1")

    for _ in range(8):
        res = await c.post(
            "/api/auth/login", json={"username": "archer", "password": "wrong"}
        )
        assert res.status_code == 401

    blocked = await c.post(
        "/api/auth/login", json={"username": "archer", "password": "wrong"}
    )
    assert blocked.status_code == 429
    # Even the right password waits: otherwise the throttle is a hint that the
    # guess was close.
    right = await c.post(
        "/api/auth/login", json={"username": "archer", "password": "password1"}
    )
    assert right.status_code == 429


@pytest.mark.asyncio
async def test_a_successful_login_clears_the_count(client):
    c, users, _db = client
    await users.create_user(username="archer", password="password1")
    for _ in range(3):
        await c.post(
            "/api/auth/login", json={"username": "archer", "password": "wrong"}
        )
    ok = await c.post(
        "/api/auth/login", json={"username": "archer", "password": "password1"}
    )
    assert ok.status_code == 200
    for _ in range(5):
        assert (
            await c.post(
                "/api/auth/login", json={"username": "archer", "password": "wrong"}
            )
        ).status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_only_this_device(client):
    c, users, _db = client
    user = await users.create_user(username="archer", password="password1")
    here = await users.issue_token(user["id"])
    elsewhere = await users.issue_token(user["id"])

    assert (
        await c.post("/api/auth/logout", headers=_bearer(here))
    ).status_code == 204
    assert (await c.get("/api/auth/identity", headers=_bearer(here))).status_code == 401
    assert (
        await c.get("/api/auth/identity", headers=_bearer(elsewhere))
    ).status_code == 200


@pytest.mark.asyncio
async def test_changing_a_password_needs_the_current_one(client):
    """An authenticated request is not enough: a bearer left on a shared
    machine must not be able to take the account over."""
    c, users, _db = client
    user = await users.create_user(username="archer", password="password1")
    token = await users.issue_token(user["id"])

    wrong = await c.post(
        "/api/auth/password",
        json={"current_password": "nope", "new_password": "a-new-password"},
        headers=_bearer(token),
    )
    assert wrong.status_code == 403

    ok = await c.post(
        "/api/auth/password",
        json={"current_password": "password1", "new_password": "a-new-password"},
        headers=_bearer(token),
    )
    assert ok.status_code == 204
    # The tab that changed it stays in; the password really changed.
    assert (await c.get("/api/auth/identity", headers=_bearer(token))).status_code == 200
    assert (
        await c.post(
            "/api/auth/login", json={"username": "archer", "password": "a-new-password"}
        )
    ).status_code == 200


# ---------------------------------------------------------------------------
# Joining
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registration_needs_an_invite_and_signs_you_in(client):
    c, users, _db = client
    admin = await users.create_user(
        username="archer", password="password1", is_admin=True
    )
    admin_token = await users.issue_token(admin["id"])

    made = await c.post(
        "/api/auth/invites", json={"max_uses": 1}, headers=_bearer(admin_token)
    )
    assert made.status_code == 200
    code = made.json()["code"]

    no_code = await c.post(
        "/api/auth/register",
        json={"invite_code": "nope", "username": "vera", "password": "password2"},
    )
    assert no_code.status_code == 403

    joined = await c.post(
        "/api/auth/register",
        json={"invite_code": code, "username": "vera", "password": "password2"},
    )
    assert joined.status_code == 200
    assert joined.json()["is_admin"] is False
    assert (
        await c.get("/api/auth/identity", headers=_bearer(joined.json()["token"]))
    ).json()["label"] == "vera"

    # Single use.
    assert (
        await c.post(
            "/api/auth/register",
            json={"invite_code": code, "username": "pete", "password": "password3"},
        )
    ).status_code == 403


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_routes_are_closed_to_a_normal_account(client):
    c, users, _db = client
    normal = await users.create_user(username="vera", password="password2")
    token = await users.issue_token(normal["id"])

    for method, path in (
        ("post", "/api/auth/invites"),
        ("get", "/api/auth/invites"),
        ("get", "/api/auth/users"),
    ):
        res = await getattr(c, method)(
            path, headers=_bearer(token), **({"json": {}} if method == "post" else {})
        )
        assert res.status_code == 403, f"{method} {path} let a normal account in"


@pytest.mark.asyncio
async def test_an_admin_cannot_disable_themselves(client):
    """The one way to end up with a site nobody can administer."""
    c, users, _db = client
    admin = await users.create_user(
        username="archer", password="password1", is_admin=True
    )
    token = await users.issue_token(admin["id"])

    res = await c.post(
        f"/api/auth/users/{admin['id']}/disabled?disabled=true", headers=_bearer(token)
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_disabling_an_account_ends_its_sessions_now(client):
    c, users, _db = client
    admin = await users.create_user(
        username="archer", password="password1", is_admin=True
    )
    admin_token = await users.issue_token(admin["id"])
    victim = await users.create_user(username="vera", password="password2")
    victim_token = await users.issue_token(victim["id"])
    assert (
        await c.get("/api/auth/identity", headers=_bearer(victim_token))
    ).status_code == 200

    res = await c.post(
        f"/api/auth/users/{victim['id']}/disabled?disabled=true",
        headers=_bearer(admin_token),
    )
    assert res.status_code == 200 and res.json()["disabled_at"]
    assert (
        await c.get("/api/auth/identity", headers=_bearer(victim_token))
    ).status_code == 401, "a disabled account kept its bearer"


# ---------------------------------------------------------------------------
# The two eras (multi-tenancy.md §9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_legacy_token_opens_an_install_with_no_accounts(client):
    """Before the first account exists this is still the single-user install
    it has always been, and its token is how an upgrade gets in to create that
    account. Without this, upgrading would lock the box."""
    from server.config import settings

    c, _users, _db = client
    res = await c.get("/api/sessions", headers=_bearer(settings.auth_token))
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_the_legacy_token_dies_the_moment_an_account_exists(client):
    """The whole defence of keeping it at all: it is self-limiting. There is
    no setting that brings it back, and no account it can impersonate."""
    from server.config import settings

    c, users, _db = client
    assert (
        await c.get("/api/sessions", headers=_bearer(settings.auth_token))
    ).status_code == 200

    await users.create_user(username="archer", password="password1")

    assert (
        await c.get("/api/sessions", headers=_bearer(settings.auth_token))
    ).status_code == 401, "the global token outlived the first account"


@pytest.mark.asyncio
async def test_a_session_bearer_opens_the_ordinary_routes(client):
    """The replacement works on the surface the global token used to open,
    which is what makes retiring it possible rather than theoretical."""
    c, users, _db = client
    user = await users.create_user(username="archer", password="password1")
    token = await users.issue_token(user["id"])

    assert (await c.get("/api/sessions", headers=_bearer(token))).status_code == 200
    assert (await c.get("/api/agents", headers=_bearer(token))).status_code == 200


@pytest.mark.asyncio
async def test_a_personal_access_token_works_where_a_session_does(client):
    """What scripts and the CLI use once the global token is gone (§3.1)."""
    c, users, _db = client
    user = await users.create_user(username="archer", password="password1")
    pat = await users.issue_token(user["id"], kind="pat", label="a script")

    assert (await c.get("/api/sessions", headers=_bearer(pat))).status_code == 200
