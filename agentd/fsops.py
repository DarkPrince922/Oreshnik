"""Files of one session's working directory: listing, reading, writing, cloning, archiving — and nothing outside that directory.

The agent runs as the same user in the same container and may create symbolic links, so every path is resolved first and refused
unless the REAL location is inside the working directory.
"""

import asyncio
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

MAX_PREVIEW_BYTES = 1_000_000
MAX_UPLOAD_BYTES = int(os.environ.get("AGENT_MAX_UPLOAD_MB", "50")) * 1024 * 1024
MAX_ARCHIVE_BYTES = int(os.environ.get("AGENT_MAX_ARCHIVE_MB", "300")) * 1024 * 1024
MAX_LIST_ENTRIES = 2000
HIDDEN = {".git"}
_CLONE_URL = re.compile(r"^https://[A-Za-z0-9._~-]+(:\d{1,5})?(/[A-Za-z0-9._~%+@-]+)+/?$")


class FsError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def resolve(root: Path, relative: str = "") -> Path:
    """The real path of `relative` under `root`; `FsError` for anything that is, or leads, outside."""
    if "\x00" in relative or relative.startswith(("/", "\\")):
        raise FsError(400, "Недопустимый путь")
    parts = [p for p in relative.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise FsError(400, "Недопустимый путь")
    real_root = Path(os.path.realpath(root))
    target = Path(os.path.realpath(real_root.joinpath(*parts)))
    if target != real_root and real_root not in target.parents:
        raise FsError(400, "Путь выходит за пределы рабочей папки")
    return target


def list_dir(root: Path, relative: str = "") -> dict:
    target = resolve(root, relative)
    if not target.is_dir():
        raise FsError(404, "Папки нет")
    entries = []
    for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
        if entry.name in HIDDEN:
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        entries.append({"name": entry.name, "dir": entry.is_dir(), "size": None if entry.is_dir() else stat.st_size, "modified": int(stat.st_mtime)})
        if len(entries) >= MAX_LIST_ENTRIES:
            break
    return {"path": "/".join(p for p in relative.replace("\\", "/").split("/") if p not in ("", ".")), "entries": entries}


def read_preview(root: Path, relative: str) -> dict:
    """The start of a text file for the page; binary files and huge ones are described, not sent."""
    target = resolve(root, relative)
    if not target.is_file():
        raise FsError(404, "Файла нет")
    size = target.stat().st_size
    with open(target, "rb") as handle:
        head = handle.read(MAX_PREVIEW_BYTES)
    if b"\x00" in head[:8192]:
        return {"binary": True, "size": size, "text": None, "truncated": False}
    return {"binary": False, "size": size, "text": head.decode("utf-8", "replace"), "truncated": size > MAX_PREVIEW_BYTES}


async def write_stream(root: Path, relative: str, chunks, limit: int | None = None) -> int:
    """Store an uploaded body at `relative` (parent folders are created); nothing is left behind when it is too big."""
    limit = MAX_UPLOAD_BYTES if limit is None else limit
    if not relative or relative.endswith("/"):
        raise FsError(400, "Нужно имя файла")
    target = resolve(root, relative)
    if target.is_dir():
        raise FsError(400, "Это папка")
    target.parent.mkdir(parents=True, exist_ok=True)
    resolve(root, relative)  # the folders just made could be a link: check again
    written = 0
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".upload-")
    try:
        with os.fdopen(fd, "wb") as out:
            async for chunk in chunks:
                written += len(chunk)
                if written > limit:
                    raise FsError(413, f"Файл больше {limit // (1024 * 1024)} МБ")
                out.write(chunk)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return written


def clone_target(root: Path, url: str) -> tuple[str, Path]:
    """(validated url, destination folder) for a `git clone`; only plain https links, a new folder named after the repository."""
    url = url.strip()
    if not _CLONE_URL.match(url):
        raise FsError(422, "Нужна ссылка вида https://хост/владелец/репозиторий")
    name = re.sub(r"\.git$", "", url.rstrip("/").rsplit("/", 1)[-1]) or "repo"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".") or "repo"
    dest = resolve(root, name)
    if dest.exists():
        raise FsError(409, f"Папка «{name}» уже есть")
    return url, dest


async def git_clone(url: str, dest: Path, env: dict[str, str], timeout: float = 300.0) -> None:
    # The protocol list is closed on purpose: `file://`, `ext::` and ssh transports could be made to read or run things in this container.
    proc = await asyncio.create_subprocess_exec(
        "git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "clone", "--depth", "1", "--no-tags", "--", url, str(dest),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        env={**env, "GIT_TERMINAL_PROMPT": "0"}, start_new_session=True,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        shutil.rmtree(dest, ignore_errors=True)
        raise FsError(504, "Клонирование заняло слишком много времени")
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise FsError(502, "Не удалось клонировать: " + (err or b"").decode("utf-8", "replace").strip().splitlines()[-1][:300] if err else "Не удалось клонировать")


def make_archive(root: Path, tmp_dir: Path) -> Path:
    """A zip of the working directory (without `.git`) in `tmp_dir`; `FsError` when it would be too big. The caller deletes it."""
    real_root = Path(os.path.realpath(root))
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=tmp_dir, suffix=".zip")
    os.close(fd)
    total = 0
    try:
        with zipfile.ZipFile(name, "w", zipfile.ZIP_DEFLATED) as archive:
            for current, dirs, files in os.walk(real_root, followlinks=False):
                dirs[:] = [d for d in dirs if d not in HIDDEN]
                for file in files:
                    path = Path(current) / file
                    if path.is_symlink() or not path.is_file():
                        continue
                    total += path.stat().st_size
                    if total > MAX_ARCHIVE_BYTES:
                        raise FsError(413, f"Папка больше {MAX_ARCHIVE_BYTES // (1024 * 1024)} МБ")
                    archive.write(path, path.relative_to(real_root))
    except BaseException:
        os.unlink(name)
        raise
    return Path(name)
