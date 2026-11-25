"""«Навыки»: SKILL.md repaired for every agent, own and shared skills, import and export of folders, and how «Код» gets them."""
import asyncio
import base64
import io
import zipfile

import httpx
import pytest

from app.core.config import settings
from app.models import Role
from app.services import agent as agent_service
from app.services import skills as svc
from tests.test_agent import new_session, op_user, openai_profile, report, wired  # noqa: F401  (`wired`: the agent stand-in)
from tests.test_agentd import agentd  # noqa: F401  (needed by `wired`)

MD = "---\nname: recon\ndescription: Разведка поддоменов\n---\nСначала subfinder, потом httpx.\n"


def make(client, headers, skill_md=MD, **body):
    r = client.post("/api/skills", json={"skill_md": skill_md, **body}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def zip_of(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, text in files.items():
            z.writestr(path, text)
    return buf.getvalue()


def test_skill_md_is_repaired_so_codex_does_not_skip_it():
    # Codex 0.169 skips a SKILL.md without front matter or without a description (only a line in its own log says so)
    plain = svc.parse_skill_md("Проверка JWT: alg none, слабый ключ.\n\nДальше подробно.", "JWT Checks")
    assert plain.name == "jwt-checks" and plain.description == "Проверка JWT: alg none, слабый ключ."
    assert svc.front_matter(plain.skill_md)[0] == {"name": "jwt-checks", "description": "Проверка JWT: alg none, слабый ключ."}
    assert plain.skill_md.endswith("Дальше подробно.") and plain.notes
    folded = svc.parse_skill_md("---\nname: jwt\ndescription: >\n  длинное\n  описание\nlicense: MIT\nmetadata:\n  a: b\n---\nтело\n")
    assert folded.description == "длинное описание" and folded.notes == [] and "license: MIT" in folded.skill_md
    assert svc.parse_skill_md("---\nname: Разведка\ndescription: d\n---\n").name == "razvedka"
    with pytest.raises(svc.SkillError):
        svc.parse_skill_md("---\nname: x\n---\n")          # no description and no text to take one from
    for bad in ("../x", "/etc/passwd", "a/../../b", "SKILL.md", ""):
        with pytest.raises(svc.SkillError):
            svc.clean_path(bad)


def test_own_skills_are_private_shared_ones_are_for_everybody_and_only_admins_change_them(client):
    _, admin = op_user(client, "admin@example.com", Role.admin)
    _, op1 = op_user(client, "op1@example.com")
    _, op2 = op_user(client, "op2@example.com")
    mine = make(client, op1)
    shared = make(client, admin, MD.replace("recon", "report"), shared=True)
    assert sorted(s["name"] for s in client.get("/api/skills", headers=op1).json()) == ["recon", "report"]
    assert [s["name"] for s in client.get("/api/skills", headers=op2).json()] == ["report"]
    assert client.post("/api/skills", json={"skill_md": MD, "shared": True}, headers=op1).status_code == 403
    assert client.patch(f"/api/skills/{shared['id']}", json={"enabled": False}, headers=op1).status_code == 403
    assert client.patch(f"/api/skills/{mine['id']}", json={"enabled": False}, headers=op2).status_code == 404
    assert client.post("/api/skills", json={"skill_md": MD}, headers=op1).status_code == 409       # the name is taken
    renamed = client.patch(f"/api/skills/{mine['id']}", json={"skill_md": MD.replace("name: recon", "name: Recon Two")}, headers=op1).json()
    assert renamed["name"] == "recon-two" and renamed["notes"]


def test_a_zip_from_another_tool_is_imported_with_its_files_and_exported_back(client):
    _, h = op_user(client)
    archive = zip_of({
        "skills/recon/SKILL.md": MD, "skills/recon/scripts/run.sh": "subfinder -d $1", "skills/recon/.DS_Store": "junk",
        "skills/jwt/SKILL.md": "# JWT\n\nПроверка токенов.", "__MACOSX/skills/recon/._SKILL.md": "junk",
    })
    r = client.post("/api/skills/import", files={"file": ("my-skills.zip", archive, "application/zip")}, headers=h)
    assert r.status_code == 200, r.text
    got = {s["name"]: s for s in r.json()["skills"]}
    assert set(got) == {"recon", "jwt"} and [f["path"] for f in got["recon"]["files"]] == ["scripts/run.sh"]
    assert got["jwt"]["description"] == "JWT"                # no description: the first paragraph (the heading) stands in
    again = client.post("/api/skills/import", files={"file": ("SKILL.md", MD.replace("subfinder", "amass"), "text/markdown")}, headers=h).json()
    assert again["skills"][0]["id"] == got["recon"]["id"] and "заменил" in " ".join(again["skills"][0]["notes"])
    assert again["skills"][0]["files"] == []                 # a re-import replaces the files too
    out = client.get(f"/api/skills/{got['jwt']['id']}/export", headers=h)
    assert out.status_code == 200 and zipfile.ZipFile(io.BytesIO(out.content)).namelist() == ["jwt/SKILL.md"]
    assert client.post("/api/skills/import", files={"file": ("x.zip", zip_of({"a.txt": "x"}), "application/zip")}, headers=h).status_code == 422


def test_files_of_a_skill_are_added_read_and_removed(client):
    _, h = op_user(client)
    skill = make(client, h)
    r = client.put(f"/api/skills/{skill['id']}/files", params={"path": "references/owasp.md"}, content=b"# OWASP", headers=h)
    assert r.status_code == 200
    [file] = r.json()["files"]
    assert file["path"] == "references/owasp.md" and file["size"] == 7 and file["revision"]
    assert client.get(f"/api/skills/{skill['id']}/files/content", params={"path": "references/owasp.md"}, headers=h).json()["text"] == "# OWASP"
    assert client.put(f"/api/skills/{skill['id']}/files", params={"path": "../x"}, content=b"x", headers=h).status_code == 422
    assert client.delete(f"/api/skills/{skill['id']}/files", params={"path": "references/owasp.md"}, headers=h).json()["files"] == []


def test_a_session_of_kod_gets_the_enabled_skills_and_the_users_own_wins_over_a_shared_one(client, wired, monkeypatch):
    sent: list[dict] = []
    real = agent_service.stream_turn

    def spy(**kw):
        sent.append(kw)
        return real(**kw)

    monkeypatch.setattr(agent_service, "stream_turn", spy)
    _, admin = op_user(client, "admin@example.com", Role.admin)
    user, h = op_user(client)
    monkeypatch.setattr(settings, "agent_runtime_owner_id", user.id)
    make(client, admin, MD.replace("Сначала", "ОБЩИЙ"), shared=True)
    make(client, admin, MD.replace("recon", "only-agent"), shared=True, scope="agent")
    make(client, h, MD.replace("Сначала", "СВОЙ"))
    off = make(client, h, MD.replace("recon", "off"))
    client.patch(f"/api/skills/{off['id']}", json={"enabled": False}, headers=h)
    report(client, h, new_session(client, h, openai_profile(user.id))["id"])
    skills = sent[-1]["skills"]
    assert [s["name"] for s in skills] == ["recon"] and sent[-1]["skills_digest"]
    assert "СВОЙ" in base64.b64decode(skills[0]["files"]["SKILL.md"]).decode()


def test_a_persona_can_name_its_own_skills_disabled_ones_too(client, wired, monkeypatch):
    sent: list[dict] = []
    real = agent_service.stream_turn
    monkeypatch.setattr(agent_service, "stream_turn", lambda **kw: sent.append(kw) or real(**kw))
    user, h = op_user(client)
    make(client, h)                                                        # recon: enabled
    off = make(client, h, MD.replace("recon", "jwt"))
    client.patch(f"/api/skills/{off['id']}", json={"enabled": False}, headers=h)   # jwt: kept for one role only
    r = client.post("/api/personas", json={"name": "JWT-охотник", "instructions": "x", "skills": ["jwt"]}, headers=h)
    assert r.status_code == 201 and r.json()["skills"] == ["jwt"]
    persona = r.json()
    report(client, h, new_session(client, h, openai_profile(user.id), persona_id=persona["id"])["id"])
    assert [s["name"] for s in sent[-1]["skills"]] == ["jwt"]
    # back to every enabled skill
    assert client.patch(f"/api/personas/{persona['id']}", json={"skills": None}, headers=h).json()["skills"] is None
    report(client, h, new_session(client, h, openai_profile(user.id, "b"), persona_id=persona["id"])["id"])
    assert [s["name"] for s in sent[-1]["skills"]] == ["recon"]
    assert client.post("/api/personas", json={"name": "x", "instructions": "x", "skills": ["Bad Name"]}, headers=h).status_code == 422


def test_a_bad_skill_in_an_archive_is_skipped_not_fatal(client):
    _, h = op_user(client)
    good = "---\nname: ok-skill\ndescription: нормальный навык\n---\n# ok\nделай X"
    bad = "no frontmatter at all, and no way to make a description"       # parses to a name but the body-as-description works… use a truly broken one
    archive = zip_of({"pack/ok-skill/SKILL.md": good, "pack/broken/SKILL.md": "---\nname: broken\n---\n"})  # no description, empty body
    r = client.post("/api/skills/import", files={"file": ("pack.zip", archive, "application/zip")}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["name"] for s in body["skills"]] == ["ok-skill"]
    assert len(body["skipped"]) == 1 and "broken" in body["skipped"][0]
    _ = bad


def test_nested_skills_keep_their_own_files_and_duplicate_names_are_reported(client, operator):
    markdown = "---\nname: {}\ndescription: Sample\n---\nExample"
    archive = zip_of({
        "SKILL.md": markdown.format("root"),
        "child/SKILL.md": markdown.format("child"),
        "child/example.txt": "child only",
        "first/SKILL.md": markdown.format("collision"),
        "second/SKILL.md": markdown.format("collision"),
    })
    response = client.post("/api/skills/import", files={"file": ("skills.zip", archive)}, headers=operator)
    assert response.status_code == 200
    body = response.json()
    results = {skill["name"]: skill["files"] for skill in body["skills"]}
    assert set(results) == {"root", "child"}
    assert results["root"] == []
    assert [file["path"] for file in results["child"]] == ["example.txt"]
    [collision] = body["skipped"]
    assert all(word in collision for word in ("collision", "first", "second"))


def test_shared_skill_attachments_and_stale_revisions(client, admin):
    from tests.conftest import auth_headers, make_user

    make_user("second-admin@example.com", role=Role.admin)
    second = auth_headers(client, "second-admin@example.com")
    skill = make(client, admin, shared=True)
    url = f"/api/skills/{skill['id']}"

    async def upload_both():
        gate = asyncio.Event()
        waiting = 0

        async def content(value):
            nonlocal waiting
            waiting += 1
            if waiting == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), timeout=5)
            yield value

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            return await asyncio.gather(
                api.put(f"{url}/files", headers=admin, params={"path": "a.txt", "expected_revision": ""}, content=content(b"a")),
                api.put(f"{url}/files", headers=second, params={"path": "b.txt", "expected_revision": ""}, content=content(b"b")),
            )

    assert [r.status_code for r in asyncio.run(upload_both())] == [200, 200]
    [latest] = client.get("/api/skills", headers=admin).json()
    assert [file["path"] for file in latest["files"]] == ["a.txt", "b.txt"]
    assert latest["edit_revision"] == skill["edit_revision"]
    [a, _] = latest["files"]
    params = {"path": a["path"], "expected_revision": a["revision"]}
    assert client.put(f"{url}/files", headers=admin, params=params, content=b"new").status_code == 200
    assert client.delete(f"{url}/files", headers=second, params=params).status_code == 409

    original = skill["edit_revision"]
    assert client.patch(url, headers=admin, json={"expected_revision": original, "enabled": False}).status_code == 200
    assert client.patch(url, headers=second, json={"expected_revision": original, "scope": "code"}).status_code == 409
