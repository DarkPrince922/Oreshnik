"""Reading, repairing and packing Agent Skills (see app/models/skill.py).

Checked with the real Codex 0.169: it loads `~/.agents/skills/<folder>/SKILL.md` and `~/.codex/skills/...`, lists every skill to the model
as "name: description (file: …)" and lets the model read the rest when it needs it. A SKILL.md without its `---` front matter, or without
a `description`, is skipped with only a line in Codex's own log — so the panel repairs such a file on import instead of letting a skill
silently do nothing. The name may be anything to Codex; the panel keeps it to the open standard's form (lower-case, digits, hyphens),
which every tool accepts and which is also the folder name.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass, field

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 2048
MAX_SKILL_MD = 600_000              # characters (a big reference-style SKILL.md is injected to the model only when the skill is used)
MAX_FILE = 1_000_000                # bytes, one file
MAX_SKILL_BYTES = 2_000_000         # bytes, all files of a skill together (SKILL.md included)
MAX_FILES = 200
MAX_ZIP_BYTES = 20_000_000          # an uploaded archive
MAX_SENT_BYTES = 8_000_000          # what one turn hands the agent: every enabled skill of the user together

_FRONT = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_KEY = re.compile(r"^([A-Za-z0-9_-]+):[ \t]*(.*)$")
_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f", "h",
                      "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


class SkillError(ValueError):
    """Why a skill cannot be taken (safe to show)."""


@dataclass
class ParsedSkill:
    name: str
    description: str
    skill_md: str                                      # repaired: always has front matter with this name and a description
    files: dict[str, str] = field(default_factory=dict)  # other files: relative path -> base64
    notes: list[str] = field(default_factory=list)       # what the panel changed, to tell the user


def slugify(text: str) -> str:
    out = []
    for ch in text.strip().lower():
        ch = _TRANSLIT.get(ch, ch)
        out.append(ch if re.fullmatch(r"[a-z0-9]+", ch or "-") else "-")
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")
    return slug[:MAX_NAME].rstrip("-")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == '"':
            try:
                return json.loads(value)
            except ValueError:
                return inner
        return inner.replace("''", "'")
    return value


def front_matter(text: str) -> tuple[dict[str, str], str, str] | None:
    """(the top-level `key: value` pairs, the front matter's text, the body) or None without front matter. Enough YAML for SKILL.md:
    plain, quoted and block (`|`, `>`) scalars; nested keys (metadata: …) are kept in the text and not read."""
    m = _FRONT.match(text)
    if not m:
        return None
    raw, body = m.group(1), text[m.end():]
    values: dict[str, str] = {}
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        km = _KEY.match(lines[i])
        i += 1
        if not km:
            continue
        key, value = km.group(1), km.group(2).strip()
        block = []
        while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
            block.append(lines[i].strip())
            i += 1
        if value[:1] in ("|", ">"):
            joiner = "\n" if value[0] == "|" else " "
            values[key] = joiner.join(b for b in block).strip()
        elif block and value and value[0] not in "\"'[{":
            values[key] = " ".join([value, *[b for b in block if b]]).strip()   # a plain scalar folded over several lines
        else:
            values[key] = _unquote(value)
    return values, raw, body


def _first_paragraph(body: str) -> str:
    for para in re.split(r"\n\s*\n", body):
        text = " ".join(line.strip().lstrip("#").strip() for line in para.strip().splitlines()).strip()
        if text:
            return text[:MAX_DESCRIPTION]
    return ""


def _set_key(raw: str, key: str, value: str) -> str:
    """The front matter with `key` set to `value` (a JSON string is valid YAML), replacing the old value and its continuation lines."""
    lines, out, i, done = raw.splitlines(), [], 0, False
    while i < len(lines):
        km = _KEY.match(lines[i])
        if km and km.group(1) == key:
            out.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
            i += 1
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                i += 1
            done = True
            continue
        out.append(lines[i])
        i += 1
    if not done:
        out.insert(0 if key == "name" else min(1, len(out)), f"{key}: {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(out)


def parse_skill_md(text: str, fallback_name: str = "") -> ParsedSkill:
    """SKILL.md made fit for every agent: front matter present, a name in the standard's form, a description. `fallback_name` (the
    folder or file it came from) names a skill that does not name itself."""
    text = text.replace("\r\n", "\n")
    if len(text) > MAX_SKILL_MD:
        raise SkillError(f"SKILL.md длиннее {MAX_SKILL_MD} символов")
    notes: list[str] = []
    parsed = front_matter(text)
    if parsed is None:
        values, raw, body = {}, "", text
        notes.append("добавлена шапка --- с именем и описанием (без неё Codex навык не загружает)")
    else:
        values, raw, body = parsed
    original = (values.get("name") or "").strip()
    name = original if NAME_RE.match(original) and len(original) <= MAX_NAME else slugify(original or fallback_name)
    if not name:
        raise SkillError("У навыка нет имени: впишите в шапку строку name: имя-навыка (латиница, цифры, дефисы)")
    if original and name != original:
        notes.append(f"имя «{original}» приведено к виду {name} (латиница, цифры, дефисы)")
    description = " ".join((values.get("description") or "").split())
    if not description:
        description = _first_paragraph(body)
        if not description:
            raise SkillError("У навыка нет описания: впишите в шапку строку description: когда и зачем его применять")
        notes.append("описания не было — взят первый абзац текста; лучше написать своё: по нему агент решает, когда брать навык")
    if len(description) > MAX_DESCRIPTION:
        raise SkillError(f"Описание длиннее {MAX_DESCRIPTION} символов: агент видит его в каждом разговоре, сократите")
    if name != original or "name" not in values:
        raw = _set_key(raw, "name", name)
    if not values.get("description") or " ".join(values["description"].split()) != description:
        raw = _set_key(raw, "description", description)
    skill_md = f"---\n{raw.strip()}\n---\n{body.lstrip(chr(10))}" if (raw != (parsed[1] if parsed else None)) else text
    return ParsedSkill(name=name, description=description, skill_md=skill_md, notes=notes)


def clean_path(path: str) -> str:
    """A file's path inside a skill folder, or SkillError: relative, no `..`, no hidden tricks."""
    path = path.replace("\\", "/").strip()
    norm = posixpath.normpath(path)
    if not path or norm in (".", "") or norm.startswith(("../", "/")) or norm == ".." or path.startswith("/") or "\x00" in path:
        raise SkillError(f"Недопустимый путь файла: {path!r}")
    if any(part in ("", ".", "..") for part in norm.split("/")) or len(norm) > 300:
        raise SkillError(f"Недопустимый путь файла: {path!r}")
    if norm == "SKILL.md":
        raise SkillError("SKILL.md правится в редакторе навыка, а не как отдельный файл")
    return norm


def check_files(skill_md: str, files: dict[str, str]) -> None:
    if len(files) > MAX_FILES:
        raise SkillError(f"Не больше {MAX_FILES} файлов в навыке")
    total = len(skill_md.encode())
    for content in files.values():
        size = len(base64.b64decode(content))
        if size > MAX_FILE:
            raise SkillError(f"Файл больше {MAX_FILE // 1_000_000} МБ")
        total += size
    if total > MAX_SKILL_BYTES:
        raise SkillError(f"Навык со всеми файлами больше {MAX_SKILL_BYTES // 1_000_000} МБ")


_SKIP = re.compile(r"(^|/)(__MACOSX|\.git|\.DS_Store|node_modules|__pycache__)(/|$)")


def read_archive(data: bytes) -> tuple[list[ParsedSkill], list[str]]:
    """Every skill in a .zip (each folder holding a SKILL.md), plus a list of "<folder>: why" for the ones that could not be taken. A
    bad skill is skipped, not fatal: a collection of dozens must not fail to import over one broken file."""
    if len(data) > MAX_ZIP_BYTES:
        raise SkillError(f"Архив больше {MAX_ZIP_BYTES // 1_000_000} МБ")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillError("Это не zip-архив")
    with archive:
        entries = {}
        for info in archive.infolist():
            path = info.filename.replace("\\", "/")
            if not info.is_dir() and not path.startswith("/") and not _SKIP.search(path):
                entries[path] = info
        roots = {posixpath.dirname(path) for path in entries if posixpath.basename(path) == "SKILL.md"}
        if not roots:
            raise SkillError("В архиве нет SKILL.md")

        files_by_root: dict[str, list[tuple[str, zipfile.ZipInfo]]] = {root: [] for root in roots}
        for path, info in entries.items():
            if posixpath.basename(path) == "SKILL.md":
                continue
            parent = posixpath.dirname(path)
            while parent and parent not in roots:
                parent = posixpath.dirname(parent)
            if parent in roots:
                relative = path[len(parent) + 1:] if parent else path
                files_by_root[parent].append((relative, info))

        parsed_by_name: dict[str, list[tuple[str, ParsedSkill]]] = {}
        skipped: list[str] = []
        for root in sorted(roots):
            label = root or "."
            try:
                with archive.open(entries[posixpath.join(root, "SKILL.md")]) as f:
                    text = f.read(MAX_SKILL_MD * 4 + 1).decode("utf-8", "replace")
                skill = parse_skill_md(text, posixpath.basename(root) or "skill")
                for relative, info in files_by_root[root]:
                    if info.file_size > MAX_FILE:
                        raise SkillError(f"файл {relative} больше {MAX_FILE // 1_000_000} МБ")
                    with archive.open(info) as f:
                        skill.files[clean_path(relative)] = base64.b64encode(f.read(MAX_FILE + 1)).decode()
                check_files(skill.skill_md, skill.files)
                parsed_by_name.setdefault(skill.name, []).append((label, skill))
            except (SkillError, zipfile.BadZipFile, RuntimeError) as exc:
                skipped.append(f"{label}: {exc}")

    skills: list[ParsedSkill] = []
    for name, candidates in parsed_by_name.items():
        if len(candidates) == 1:
            skills.append(candidates[0][1])
        else:
            folders = ", ".join(folder for folder, _ in candidates)
            skipped.append(f"Имя {name!r} совпадает у нескольких навыков: {folders}")
    if not skills:
        raise SkillError("Ни один навык из архива не подошёл: " + "; ".join(skipped[:5]))
    return skills, skipped


def build_archive(name: str, skill_md: str, files: dict[str, str]) -> bytes:
    """The skill as a folder in a .zip — the same layout another tool imports."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{name}/SKILL.md", skill_md)
        for path, content in sorted(files.items()):
            z.writestr(f"{name}/{path}", base64.b64decode(content))
    return buf.getvalue()


def payload(skills) -> tuple[list[dict], str]:
    """What a turn hands the agent — [{name, files: {path: base64}}], SKILL.md among the files — and a digest of it (a change restarts
    the agent's process, so Codex reads the skills again)."""
    out = [{"name": s.name, "files": {"SKILL.md": base64.b64encode(s.skill_md.encode()).decode(), **(s.files or {})}} for s in skills]
    out.sort(key=lambda s: s["name"])
    digest = hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest()[:16] if out else ""
    return out, digest
