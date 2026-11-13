import { motion } from "framer-motion";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Badge, Button, Card, ErrorNote, Field, Modal, PageHeader, Spinner, rise, stagger } from "../components/ui";
import { createUser, deleteUser, errorMessage, listUsers, updateUser, type Role, type User } from "../lib/api";
import { useAuth } from "../lib/auth";

const ROLES: { value: Role; label: string }[] = [
  { value: "admin", label: "admin" },
  { value: "operator", label: "operator" },
  { value: "viewer", label: "viewer" },
];

export default function Users() {
  const { user: me } = useAuth();
  const [users, setUsers] = useState<User[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<number | null>(null);

  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ email: "", password: "", role: "operator" as Role });
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      setUsers(await listUsers());
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);
  useEffect(() => {
    void reload();
  }, [reload]);

  const mutate = async (id: number, fn: () => Promise<unknown>) => {
    setPending(id);
    setError(null);
    try {
      await fn();
      await reload();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setPending(null);
    }
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setFormError(null);
    setBusy(true);
    try {
      await createUser(form);
      setOpen(false);
      setForm({ email: "", password: "", role: "operator" });
      await reload();
    } catch (err) {
      setFormError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const resetPassword = (u: User) => {
    const password = window.prompt(`Новый пароль для ${u.email} (минимум 8 символов):`);
    if (password) void mutate(u.id, () => updateUser(u.id, { password }));
  };

  return (
    <>
      <PageHeader
        title="Пользователи"
        subtitle="admin — всё; operator — сканы и свои LLM-профили; viewer — только просмотр."
        action={<Button variant="primary" onClick={() => { setFormError(null); setOpen(true); }}>+ Новый пользователь</Button>}
      />
      <ErrorNote message={error} />

      {users === null && !error && <div className="flex justify-center py-20"><Spinner className="h-6 w-6" /></div>}

      {users && (
        <Card className="overflow-hidden">
          <motion.ul variants={stagger} initial="hidden" animate="show" className="divide-y divide-white/5">
            {users.map((u) => {
              const self = u.id === me?.id;
              return (
                <motion.li key={u.id} variants={rise} className="flex flex-wrap items-center gap-x-4 gap-y-3 px-5 py-4">
                  <div className="min-w-0 flex-1 basis-56">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-sm text-white">{u.email}</span>
                      {self && <Badge tone="accent">вы</Badge>}
                      {!u.is_active && <Badge tone="bad">отключён</Badge>}
                    </div>
                    <div className="mt-0.5 text-xs text-slate-600">с {new Date(u.created_at).toLocaleDateString("ru-RU")}</div>
                  </div>

                  <select
                    className="field !w-auto !py-1.5 text-xs"
                    value={u.role}
                    disabled={self || pending === u.id}
                    onChange={(e) => mutate(u.id, () => updateUser(u.id, { role: e.target.value as Role }))}
                    aria-label={`Роль ${u.email}`}
                  >
                    {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
                  </select>

                  <div className="flex gap-2">
                    <Button className="!px-3 !py-1.5 text-xs" disabled={pending === u.id} onClick={() => resetPassword(u)}>Пароль</Button>
                    <Button className="!px-3 !py-1.5 text-xs" disabled={self || pending === u.id} onClick={() => mutate(u.id, () => updateUser(u.id, { is_active: !u.is_active }))}>
                      {u.is_active ? "Отключить" : "Включить"}
                    </Button>
                    <Button
                      variant="danger"
                      className="!px-3 !py-1.5 text-xs"
                      disabled={self || pending === u.id}
                      onClick={() => window.confirm(`Удалить ${u.email}? Его LLM-профили тоже будут удалены.`) && mutate(u.id, () => deleteUser(u.id))}
                    >
                      ✕
                    </Button>
                  </div>
                </motion.li>
              );
            })}
          </motion.ul>
        </Card>
      )}

      <Modal open={open} onClose={() => setOpen(false)} title="Новый пользователь">
        <form onSubmit={submit} className="space-y-4">
          <Field label="Email">
            <input className="field" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required autoComplete="off" />
          </Field>
          <Field label="Пароль" hint="Минимум 8 символов. Пользователь сможет сменить его в разделе «Аккаунт».">
            <input className="field" type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required minLength={8} autoComplete="new-password" />
          </Field>
          <Field label="Роль">
            <select className="field" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
              {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
            </select>
          </Field>
          <ErrorNote message={formError} />
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" onClick={() => setOpen(false)}>Отмена</Button>
            <Button type="submit" variant="primary" loading={busy}>Создать</Button>
          </div>
        </form>
      </Modal>
    </>
  );
}
