import { useState, type FormEvent } from "react";

import { Badge, Button, Card, ErrorNote, Field, PageHeader } from "../components/ui";
import { changePassword, errorMessage } from "../lib/api";
import { useAuth } from "../lib/auth";

export default function Account() {
  const { user, logout } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setDone(false);
    if (next !== repeat) {
      setError("Новые пароли не совпадают");
      return;
    }
    setBusy(true);
    try {
      await changePassword(current, next);
      setCurrent("");
      setNext("");
      setRepeat("");
      setDone(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="w-full">
      <PageHeader title="Аккаунт" subtitle="Ваши данные и безопасность" />

      <div className="grid gap-6 lg:grid-cols-2">
        <Card className="p-6">
          <h3 className="text-sm font-semibold text-white">Профиль</h3>
          <dl className="mt-4 space-y-3 text-sm">
            <div className="flex justify-between"><dt className="text-slate-500">Email</dt><dd className="text-slate-200">{user?.email}</dd></div>
            <div className="flex justify-between"><dt className="text-slate-500">Роль</dt><dd>{user && <Badge tone="accent">{user.role}</Badge>}</dd></div>
          </dl>
          <Button className="mt-6" onClick={logout}>Выйти</Button>
        </Card>

        <Card className="p-6">
          <h3 className="text-sm font-semibold text-white">Смена пароля</h3>
          <form onSubmit={submit} className="mt-4 space-y-4">
            <Field label="Текущий пароль">
              <input className="field" type="password" value={current} onChange={(e) => setCurrent(e.target.value)} required autoComplete="current-password" />
            </Field>
            <Field label="Новый пароль" hint="Минимум 8 символов">
              <input className="field" type="password" value={next} onChange={(e) => setNext(e.target.value)} required minLength={8} autoComplete="new-password" />
            </Field>
            <Field label="Повторите новый пароль">
              <input className="field" type="password" value={repeat} onChange={(e) => setRepeat(e.target.value)} required minLength={8} autoComplete="new-password" />
            </Field>
            <ErrorNote message={error} />
            {done && <p className="rounded-lg border border-signal-ok/20 bg-signal-ok/10 px-3 py-2 text-xs text-signal-ok">Пароль изменён</p>}
            <Button type="submit" variant="primary" loading={busy}>Сменить пароль</Button>
          </form>
        </Card>
      </div>
    </div>
  );
}
