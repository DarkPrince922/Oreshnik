#!/usr/bin/env python3
"""Зашить в панель Oreshnik навыки Decepticon и 21 личность-специалиста — через её же API, только для того
аккаунта, которым вы войдёте (войдите АДМИНОМ). Повторный запуск обновляет созданное, дубли не плодит.

Запуск (на машине с панелью):
    python3 scripts/setup-decepticon.py --url http://localhost:5173 --email admin@oreshnik.dev --password '...'

Можно через переменные окружения ORESHNIK_URL / ORESHNIK_EMAIL / ORESHNIK_PASSWORD.
Нужен только python3 (стандартная библиотека) и git (чтобы скачать Decepticon). Своего Decepticon-клона можно
указать через --repo /путь, тогда git не нужен.
"""
import argparse, io, json, os, re, subprocess, sys, tempfile, urllib.request, urllib.error, zipfile
from pathlib import Path

DECEPTICON_URL = "https://github.com/BitterSecurity/Decepticon"

# личность (файл промта) -> (имя в панели, описание, папка навыков под standard/ или None=только общие)
PERSONAS = [
    ("recon",                 "RECON — разведка",             "Пассивная и активная разведка: поддомены, порты, живые хосты, веб, OSINT.", "recon"),
    ("osint_operator",        "OSINT — открытые источники",   "Сбор данных о цели из открытых источников.",                               "osint"),
    ("exploit",               "EXPLOIT — эксплуатация",        "Получение доступа и эксплуатация: web, API, AD, CI/CD, крипто.",            "exploit"),
    ("postexploit",           "POST-EXPLOIT — закрепление",    "После доступа: учётки, повышение прав, боковое движение, C2.",             "post-exploit"),
    ("ad_operator",           "AD — Active Directory",         "Атаки на Active Directory: Kerberoast, DCSync, ADCS.",                     "ad"),
    ("cloud_hunter",          "CLOUD — облака",                "Атаки на AWS/GCP/Azure и их сервисы.",                                     "cloud"),
    ("wireless_operator",     "WIRELESS — радиосети",          "Wi-Fi, Bluetooth и другие радиосети (нужно железо).",                      "wireless"),
    ("iot_operator",          "IOT — умные устройства",        "Атаки на IoT-устройства (часто нужно железо).",                            "iot"),
    ("mobile_operator",       "MOBILE — мобильные",            "Android/iOS приложения (нужен телефон или эмулятор).",                     "mobile"),
    ("ics_operator",          "ICS/OT — промышленные",         "Промышленные протоколы: Modbus, S7, DNP3, OPC UA.",                        "ics"),
    ("phisher",               "PHISHER — фишинг",              "Фишинговые сценарии и доставка.",                                          "phisher"),
    ("supply_chain_operator", "SUPPLY-CHAIN — цепочки поставок","Атаки через зависимости и сборки.",                                        "supply-chain"),
    ("reverser",              "REVERSER — реверс",             "Реверс-инжиниринг бинарников.",                                            "reverser"),
    ("forensicator",          "DFIR — форензика",              "Форензика и анти-форензика.",                                              "dfir"),
    ("analyst",               "ANALYST — анализ",              "Анализ находок, приоритизация, корреляция.",                               "analyst"),
    ("contract_auditor",      "CONTRACTS — смарт-контракты",   "Аудит смарт-контрактов.",                                                  "contracts"),
    ("soundwave",             "SOUNDWAVE — планирование/RoE",  "Планирование операции: RoE, ConOps, деконфликция, очистка.",               "soundwave"),
    ("autohunt",              "AUTOHUNT — авто-охота",         "Автономный прогон по шаблонам операции.",                                  "soundwave"),
    ("decepticon",            "DECEPTICON — оркестратор",      "Координатор: делит задачу, ведёт kill-chain, собирает отчёт.",             "decepticon"),
    ("finding_reporter",      "REPORTER — отчёты",             "Оформление находок в отчёт.",                                              "finding_reporter"),
    ("finding_verifier",      "VERIFIER — проверка находок",   "Подтверждение находок перед отчётом.",                                     None),
]


def api(url, token, method, path, *, body=None, multipart=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = None
    if multipart is not None:
        boundary = "----oreshnik-setup-boundary"
        field, (fname, content, ctype) = multipart
        parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"shared\"\r\n\r\nfalse\r\n".encode(),
                 f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{fname}\"\r\n".encode(),
                 f"Content-Type: {ctype}\r\n\r\n".encode(), content, f"\r\n--{boundary}--\r\n".encode()]
        data = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise SystemExit(f"Ошибка API {method} {path}: {e.code} {detail[:300]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Не достучаться до панели ({url}): {e.reason}. Проверьте --url.")


def ensure_repo(repo):
    if repo:
        return Path(repo)
    dest = Path(tempfile.mkdtemp(prefix="decepticon-")) / "repo"
    print(f"Скачиваю Decepticon в {dest} …")
    env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1")
    r = subprocess.run(["git", "clone", "--depth", "1", DECEPTICON_URL, str(dest)], env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"git clone не удался: {r.stderr[-300:]}\nУстановите git или укажите свой клон через --repo.")
    return dest


def skill_name(skill_md_path):
    text = skill_md_path.read_text(encoding="utf-8", errors="replace")[:3000]
    m = re.search(r"^name:\s*(.+)$", text, re.M)
    raw = (m.group(1).strip().strip("\"'") if m else skill_md_path.parent.name)
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", raw.lower())).strip("-")[:64]
    return slug or skill_md_path.parent.name


def global_roots(skills_dir):
    dirs = sorted({p.parent for p in skills_dir.rglob("SKILL.md")}, key=lambda d: len(str(d)))
    roots = [d for d in dirs if not any(d != o and str(d).startswith(str(o) + os.sep) for o in dirs)]
    return roots


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("ORESHNIK_URL"))
    ap.add_argument("--email", default=os.environ.get("ORESHNIK_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("ORESHNIK_PASSWORD"))
    ap.add_argument("--repo", help="путь к уже скачанному Decepticon (иначе клонируется сам)")
    ap.add_argument("--skip-skills", action="store_true", help="не заливать навыки (только личности)")
    a = ap.parse_args()
    if not (a.url and a.email and a.password):
        raise SystemExit("Нужны --url, --email, --password (или ORESHNIK_URL/EMAIL/PASSWORD).")

    tok = api(a.url, None, "POST", "/api/auth/login", body={"email": a.email, "password": a.password})["access_token"]
    me = api(a.url, tok, "GET", "/api/auth/me")
    if me.get("role") != "admin":
        print(f"ВНИМАНИЕ: вы вошли как {me.get('role')}, не админ. Общие навыки/личности создать не выйдет; личное — да.")
    print(f"Вошёл как {me['email']} ({me['role']}).")

    repo = ensure_repo(a.repo)
    skills_dir = repo / "packages/decepticon/decepticon/skills"
    prompts_dir = repo / "packages/decepticon/decepticon/agents/prompts/standard"
    if not skills_dir.is_dir() or not prompts_dir.is_dir():
        raise SystemExit(f"Не нашёл навыки/промты в {repo}. Это точно клон Decepticon?")

    roots = global_roots(skills_dir)
    # имя навыка -> True (для проверки), и категория(папка под standard/ или 'shared') -> [имена]
    cat_names: dict[str, list[str]] = {}
    for r in roots:
        rel = r.relative_to(skills_dir).parts  # ('standard','exploit','web') или ('shared','opsec') или ('plugins',...)
        cat = rel[1] if rel[0] == "standard" and len(rel) > 1 else rel[0]
        cat_names.setdefault(cat, []).append(skill_name(r / "SKILL.md"))
    shared = cat_names.get("shared", [])

    if not a.skip_skills:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in skills_dir.rglob("*"):
                if p.is_file():
                    z.write(p, str(p.relative_to(skills_dir)))
        print("Заливаю навыки …")
        res = api(a.url, tok, "POST", "/api/skills/import", multipart=("file", ("decepticon-skills.zip", buf.getvalue(), "application/zip")))
        print(f"  навыков загружено: {len(res['skills'])}" + (f", пропущено: {len(res['skipped'])}" if res.get("skipped") else ""))
        for s in (res.get("skipped") or [])[:10]:
            print("   пропуск:", s)
        # всё выключаем, чтобы сессии без личности не хватали всё; личности берут своё даже у выключенных
        for s in api(a.url, tok, "GET", "/api/skills"):
            if s["mine"] and s["enabled"]:
                api(a.url, tok, "PATCH", f"/api/skills/{s['id']}", body={"enabled": False})
        print("  все навыки выключены глобально (личности всё равно получат свои).")

    existing = {p["name"]: p for p in api(a.url, tok, "GET", "/api/personas") if p.get("mine")}
    print("Создаю личности …")
    for pfile, pname, pdesc, cat in PERSONAS:
        md = (prompts_dir / f"{pfile}.md")
        if not md.is_file():
            print(f"  ! пропуск {pname}: нет промта {pfile}.md"); continue
        instr = md.read_text(encoding="utf-8", errors="replace").strip()[:20000]
        names = (cat_names.get(cat, []) if cat else []) + shared
        names = sorted(set(names))
        body = {"name": pname, "description": pdesc[:200], "instructions": instr, "scope": "both", "skills": names}
        if pname in existing:
            api(a.url, tok, "PATCH", f"/api/personas/{existing[pname]['id']}", body=body)
            print(f"  обновлена: {pname}  ({len(names)} навыков)")
        else:
            api(a.url, tok, "POST", "/api/personas", body=body)
            print(f"  создана:   {pname}  ({len(names)} навыков)")
    print("Готово. Открой «Мастерская» → «Личности».")


if __name__ == "__main__":
    main()
