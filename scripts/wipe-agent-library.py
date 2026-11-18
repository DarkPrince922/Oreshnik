#!/usr/bin/env python3
"""Удалить из панели Oreshnik ВСЕ личности и навыки, доступные вам на удаление (ваши личные; общие — только если вы админ),
чтобы начать с чистого листа и добавить нужное вручную. Через API панели, под тем аккаунтом, которым войдёте.

    python3 scripts/wipe-agent-library.py --url http://localhost:5173 --email admin@oreshnik.dev --password '...'

По умолчанию спрашивает подтверждение. Что чистить — выбирается флагами:
    --personas        только личности
    --skills          только навыки
    (без флагов — и то, и другое)
    --yes             не спрашивать подтверждение (для автоматизации)

Переменные окружения: ORESHNIK_URL / ORESHNIK_EMAIL / ORESHNIK_PASSWORD.
Удаление необратимо. Сессии «Кода», что уже шли с удалённой личностью, продолжат работать без неё.
"""
import argparse, json, os, urllib.request, urllib.error


def api(url, token, method, path):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url.rstrip("/") + path, headers=headers, method=method)
    if method == "POST":
        headers["Content-Type"] = "application/json"
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Ошибка API {method} {path}: {e.code} {e.read().decode('utf-8','replace')[:200]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Не достучаться до панели ({url}): {e.reason}")


def login(url, email, password):
    # логин отдельным запросом с телом
    body = json.dumps({"email": email, "password": password}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api/auth/login", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())["access_token"]
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Логин не удался: {e.code} {e.read().decode('utf-8','replace')[:200]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("ORESHNIK_URL"))
    ap.add_argument("--email", default=os.environ.get("ORESHNIK_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("ORESHNIK_PASSWORD"))
    ap.add_argument("--personas", action="store_true", help="удалить только личности")
    ap.add_argument("--skills", action="store_true", help="удалить только навыки")
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    a = ap.parse_args()
    if not (a.url and a.email and a.password):
        raise SystemExit("Нужны --url, --email, --password (или ORESHNIK_URL/EMAIL/PASSWORD).")
    do_personas = a.personas or not a.skills
    do_skills = a.skills or not a.personas

    tok = login(a.url, a.email, a.password)
    me = api(a.url, tok, "GET", "/api/auth/me")
    print(f"Вошёл как {me['email']} ({me['role']}).")

    personas = [p for p in (api(a.url, tok, "GET", "/api/personas") or []) if p.get("editable")] if do_personas else []
    skills = [s for s in (api(a.url, tok, "GET", "/api/skills") or []) if s.get("editable")] if do_skills else []
    if not personas and not skills:
        print("Удалять нечего (нет доступных вам личностей/навыков)."); return

    print(f"Будет удалено: личностей {len(personas)}, навыков {len(skills)}.")
    if not a.yes:
        if input("Это необратимо. Напишите DELETE, чтобы продолжить: ").strip() != "DELETE":
            print("Отменено."); return

    for p in personas:
        api(a.url, tok, "DELETE", f"/api/personas/{p['id']}")
    for s in skills:
        api(a.url, tok, "DELETE", f"/api/skills/{s['id']}")
    print(f"Удалено: личностей {len(personas)}, навыков {len(skills)}. Готово.")


if __name__ == "__main__":
    main()
