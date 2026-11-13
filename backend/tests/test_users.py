def test_only_admin_can_manage_users(client, admin, operator, viewer):
    assert client.get("/api/users", headers=admin).status_code == 200
    for h in (operator, viewer):
        assert client.get("/api/users", headers=h).status_code == 403
        assert client.post("/api/users", headers=h, json={"email": "x@example.com", "password": "password123"}).status_code == 403
    assert client.get("/api/users").status_code == 401


def test_admin_creates_user_with_role_and_rejects_duplicates(client, admin):
    r = client.post("/api/users", headers=admin,
                    json={"email": "New@Example.com", "password": "password123", "role": "viewer"})
    assert r.status_code == 201 and r.json()["email"] == "new@example.com" and r.json()["role"] == "viewer"
    dup = client.post("/api/users", headers=admin, json={"email": "new@example.com", "password": "password123"})
    assert dup.status_code == 409


def test_create_user_validates_input(client, admin):
    assert client.post("/api/users", headers=admin, json={"email": "not-an-email", "password": "password123"}).status_code == 422
    assert client.post("/api/users", headers=admin, json={"email": "a@example.com", "password": "short"}).status_code == 422
    assert client.post("/api/users", headers=admin, json={"email": "a@example.com", "password": "password123", "role": "root"}).status_code == 422


def test_admin_can_change_role_and_reset_password(client, admin):
    uid = client.post("/api/users", headers=admin, json={"email": "u@example.com", "password": "password123"}).json()["id"]
    r = client.patch(f"/api/users/{uid}", headers=admin, json={"role": "viewer", "password": "brandnewpass"})
    assert r.status_code == 200 and r.json()["role"] == "viewer"
    login = client.post("/api/auth/login", json={"email": "u@example.com", "password": "brandnewpass"})
    assert login.status_code == 200


def test_admin_cannot_demote_deactivate_or_delete_self(client, admin):
    me = client.get("/api/auth/me", headers=admin).json()["id"]
    assert client.patch(f"/api/users/{me}", headers=admin, json={"role": "viewer"}).status_code == 400
    assert client.patch(f"/api/users/{me}", headers=admin, json={"is_active": False}).status_code == 400
    assert client.delete(f"/api/users/{me}", headers=admin).status_code == 400
    # Re-sending the same role is harmless.
    assert client.patch(f"/api/users/{me}", headers=admin, json={"role": "admin"}).status_code == 200


def test_delete_user_removes_their_profiles(client, admin):
    uid = client.post("/api/users", headers=admin, json={"email": "u@example.com", "password": "password123"}).json()["id"]
    h = {"Authorization": "Bearer " + client.post("/api/auth/login", json={"email": "u@example.com", "password": "password123"}).json()["access_token"]}
    client.post("/api/llm-profiles", headers=h, json={"name": "p", "provider": "openai", "model": "m", "api_key": "sk-abcdefghijkl"})
    assert client.delete(f"/api/users/{uid}", headers=admin).status_code == 204
    from app.core.db import SessionLocal
    from app.models import LLMProfile
    with SessionLocal() as db:
        assert db.query(LLMProfile).count() == 0
    assert client.delete(f"/api/users/{uid}", headers=admin).status_code == 404
