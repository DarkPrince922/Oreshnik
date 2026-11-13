from app.core.config import settings
from tests.conftest import auth_headers, make_user
from app.models import Role


def login(client, email, password):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def test_login_success_returns_token_and_user(client):
    make_user("a@example.com")
    r = login(client, "A@Example.com ", "password123")  # email is normalised
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer" and body["user"]["email"] == "a@example.com"
    assert "password" not in str(body)


def test_login_wrong_password_and_unknown_user_look_identical(client):
    make_user("a@example.com")
    bad_pw = login(client, "a@example.com", "nope")
    unknown = login(client, "ghost@example.com", "nope")
    assert bad_pw.status_code == unknown.status_code == 401
    assert bad_pw.json() == unknown.json()


def test_inactive_user_cannot_login(client):
    make_user("off@example.com", active=False)
    assert login(client, "off@example.com", "password123").status_code == 401


def test_me_requires_valid_token(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401
    make_user("a@example.com")
    h = auth_headers(client, "a@example.com")
    assert client.get("/api/auth/me", headers=h).json()["email"] == "a@example.com"


def test_deactivated_user_token_stops_working(client, admin):
    user = make_user("victim@example.com")
    h = auth_headers(client, "victim@example.com")
    assert client.get("/api/auth/me", headers=h).status_code == 200
    r = client.patch(f"/api/users/{user.id}", json={"is_active": False}, headers=admin)
    assert r.status_code == 200
    assert client.get("/api/auth/me", headers=h).status_code == 401


def test_login_is_throttled_after_repeated_failures(client):
    make_user("a@example.com")
    for _ in range(settings.login_max_failures):
        assert login(client, "a@example.com", "wrong").status_code == 401
    # Even the right password is refused while blocked.
    assert login(client, "a@example.com", "password123").status_code == 429


def test_change_password(client):
    make_user("a@example.com")
    h = auth_headers(client, "a@example.com")
    bad = client.post("/api/auth/change-password", headers=h,
                      json={"current_password": "wrong", "new_password": "newpassword1"})
    assert bad.status_code == 400
    ok = client.post("/api/auth/change-password", headers=h,
                     json={"current_password": "password123", "new_password": "newpassword1"})
    assert ok.status_code == 204
    assert login(client, "a@example.com", "password123").status_code == 401
    assert login(client, "a@example.com", "newpassword1").status_code == 200


def test_bootstrap_creates_admin_and_mock_runner_once(client):
    from app.bootstrap import run_bootstrap
    from app.core.db import SessionLocal

    with SessionLocal() as db:
        run_bootstrap(db)
        run_bootstrap(db)  # idempotent
    h = auth_headers(client, "root@example.com", "bootstrap-pass")
    assert client.get("/api/auth/me", headers=h).json()["role"] == "admin"
    runners = client.get("/api/runners", headers=h).json()
    assert [r["kind"] for r in runners] == ["mock"]
    assert len(client.get("/api/users", headers=h).json()) == 1


def test_bootstrap_does_not_touch_existing_installation(client):
    from app.bootstrap import run_bootstrap
    from app.core.db import SessionLocal

    make_user("existing@example.com", role=Role.admin)
    with SessionLocal() as db:
        run_bootstrap(db)
    h = auth_headers(client, "existing@example.com")
    assert len(client.get("/api/users", headers=h).json()) == 1
