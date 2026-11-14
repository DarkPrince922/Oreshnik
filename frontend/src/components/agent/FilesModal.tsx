import { useCallback, useEffect, useRef, useState } from "react";

import {
  cloneIntoAgent,
  downloadAgentArchive,
  downloadAgentFile,
  errorMessage,
  listAgentFiles,
  readAgentFile,
  uploadAgentFile,
  type AgentFileListing,
  type AgentFilePreview,
  type AgentSession,
} from "../../lib/api";
import { formatBytes, joinPath, parentPath } from "../../lib/files";
import { Button, ErrorNote, Modal, Spinner } from "../ui";

/** The files of a session's working folder: browse, read, download, upload, clone a repository, take everything as a zip. */
export default function FilesModal({ open, session, onClose }: { open: boolean; session: AgentSession; onClose: () => void }) {
  const [path, setPath] = useState("");
  const [listing, setListing] = useState<AgentFileListing | null>(null);
  const [preview, setPreview] = useState<{ path: string; data: AgentFilePreview } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cloneUrl, setCloneUrl] = useState("");
  const input = useRef<HTMLInputElement>(null);

  const load = useCallback(
    async (target: string) => {
      setBusy("list");
      setError(null);
      try {
        setListing(await listAgentFiles(session.id, target));
        setPath(target);
        setPreview(null);
      } catch (e) {
        setError(errorMessage(e));
      } finally {
        setBusy(null);
      }
    },
    [session.id]
  );

  useEffect(() => {
    if (open) void load("");
  }, [open, load]);

  const run = async (what: string, fn: () => Promise<void>) => {
    setBusy(what);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  const open_ = (name: string, dir: boolean) => {
    const target = joinPath(path, name);
    if (dir) return void load(target);
    void run("read", async () => setPreview({ path: target, data: await readAgentFile(session.id, target) }));
  };

  const upload = (files: FileList | null) => {
    if (!files?.length) return;
    void run("upload", async () => {
      for (const file of Array.from(files)) await uploadAgentFile(session.id, joinPath(path, file.name), file);
      setListing(await listAgentFiles(session.id, path));
    });
  };

  const crumbs = path ? path.split("/") : [];
  return (
    <Modal open={open} onClose={onClose} title="Файлы сессии" wide>
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <button type="button" className="text-accent-soft hover:underline" onClick={() => void load("")}>
            папка сессии
          </button>
          {crumbs.map((c, i) => (
            <span key={i} className="flex items-center gap-2">
              <span className="text-slate-600">/</span>
              <button type="button" className="text-accent-soft hover:underline" onClick={() => void load(crumbs.slice(0, i + 1).join("/"))}>
                {c}
              </button>
            </span>
          ))}
          {busy === "list" && <Spinner className="h-3 w-3" />}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <input ref={input} type="file" multiple hidden onChange={(e) => { upload(e.target.files); e.target.value = ""; }} />
          <Button className="px-3 py-1.5 text-xs" loading={busy === "upload"} onClick={() => input.current?.click()}>
            Загрузить файлы сюда
          </Button>
          <Button className="px-3 py-1.5 text-xs" loading={busy === "zip"} onClick={() => void run("zip", () => downloadAgentArchive(session.id, session.title))}>
            Скачать всё (zip)
          </Button>
          <form
            className="flex min-w-[16rem] flex-1 items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              void run("clone", async () => {
                await cloneIntoAgent(session.id, cloneUrl.trim());
                setCloneUrl("");
                await load("");
              });
            }}
          >
            <input className="field flex-1 text-xs" value={cloneUrl} onChange={(e) => setCloneUrl(e.target.value)} placeholder="https://github.com/владелец/репозиторий" aria-label="Ссылка на репозиторий" />
            <Button type="submit" className="px-3 py-1.5 text-xs" loading={busy === "clone"} disabled={!cloneUrl.trim()}>
              Клонировать
            </Button>
          </form>
        </div>
        <ErrorNote message={error} />

        <div className="grid gap-3 md:grid-cols-[1fr_1.4fr]">
          <div className="max-h-96 overflow-auto rounded-xl border border-white/5">
            {listing && path !== "" && (
              <button type="button" className="flex w-full items-center gap-2 border-b border-white/5 px-3 py-2 text-left text-xs text-slate-400 hover:bg-white/5" onClick={() => void load(parentPath(path))}>
                ↑ выше
              </button>
            )}
            {listing?.entries.length === 0 && <p className="px-3 py-6 text-center text-xs text-slate-500">Папка пуста. Загрузите файлы или клонируйте репозиторий.</p>}
            {listing?.entries.map((e) => (
              <button key={e.name} type="button" className="flex w-full items-center gap-2 border-b border-white/5 px-3 py-2 text-left last:border-0 hover:bg-white/5" onClick={() => open_(e.name, e.dir)}>
                <span className="text-xs">{e.dir ? "📁" : "📄"}</span>
                <span className="min-w-0 flex-1 truncate font-mono text-xs text-slate-200">{e.name}</span>
                {e.size !== null && <span className="shrink-0 text-[11px] text-slate-500">{formatBytes(e.size)}</span>}
              </button>
            ))}
          </div>

          <div className="min-h-[8rem] rounded-xl border border-white/5 p-3">
            {!preview && <p className="py-6 text-center text-xs text-slate-500">Выберите файл, чтобы посмотреть.</p>}
            {preview && (
              <>
                <div className="mb-2 flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate font-mono text-xs text-slate-300">{preview.path}</span>
                  <Button className="px-2.5 py-1 text-[11px]" loading={busy === "download"} onClick={() => void run("download", () => downloadAgentFile(session.id, preview.path))}>
                    Скачать
                  </Button>
                </div>
                {preview.data.binary ? (
                  <p className="text-xs text-slate-500">Бинарный файл ({formatBytes(preview.data.size)}): посмотреть нельзя, можно скачать.</p>
                ) : (
                  <>
                    <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-black/40 p-3 font-mono text-[11px] leading-relaxed text-slate-300">{preview.data.text}</pre>
                    {preview.data.truncated && <p className="mt-1 text-[11px] text-slate-500">Показано начало файла ({formatBytes(preview.data.size)} всего): остальное — в скачанном файле.</p>}
                  </>
                )}
              </>
            )}
          </div>
        </div>
        <p className="text-[11px] leading-relaxed text-slate-600">
          Это рабочая папка агента в его контейнере. Файлы сессии видны только вам; при удалении сессии они удаляются.
        </p>
      </div>
    </Modal>
  );
}
