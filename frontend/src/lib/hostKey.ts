import type { Runner } from "./api";

export type HostKeyState =
  | "unknown" // nothing seen yet: the host has not been looked at
  | "pending" // a key was seen and waits for the administrator to confirm its fingerprint
  | "trusted" // a key is pinned and the host still shows it (or has not been contacted since)
  | "changed"; // a key is pinned and the host now shows a different one

type KeyFields = Pick<Runner, "ssh_host_key_fingerprint" | "ssh_host_key_pending_fingerprint">;

export function hostKeyState(r: KeyFields): HostKeyState {
  const pinned = r.ssh_host_key_fingerprint;
  const pending = r.ssh_host_key_pending_fingerprint;
  if (pinned) return pending && pending !== pinned ? "changed" : "trusted";
  return pending ? "pending" : "unknown";
}

/** How the panel logs in to the host: "ключ", "пароль" or "ключ + пароль". */
export function authLabel(r: Pick<Runner, "has_ssh_key" | "has_ssh_password">): string {
  return [r.has_ssh_key ? "ключ" : null, r.has_ssh_password ? "пароль" : null].filter(Boolean).join(" + ") || "нет входа";
}

/** Whether a runner form describes at least one way to log in (what is stored, what is typed, minus what is being removed). */
export function hasLogin(input: { key: string; password: string; hadKey: boolean; hadPassword: boolean; clearKey: boolean; clearPassword: boolean }): boolean {
  const key = input.key.trim() !== "" || (input.hadKey && !input.clearKey);
  const password = input.password !== "" || (input.hadPassword && !input.clearPassword);
  return key || password;
}

/** "SHA256:AbCd…WxYz" for a tight spot; the full value is always available on hover and in the dialog. */
export function shortFingerprint(fp: string, keep = 10): string {
  const body = fp.replace(/^SHA256:/, "");
  return body.length <= keep * 2 + 1 ? fp : `SHA256:${body.slice(0, keep)}…${body.slice(-keep)}`;
}

/** Fingerprints are compared by eye: groups of 8 characters are easier to read out and to check. */
export function groupFingerprint(fp: string): string {
  const prefix = fp.startsWith("SHA256:") ? "SHA256:" : "";
  const body = fp.slice(prefix.length);
  return prefix + (body.match(/.{1,8}/g)?.join(" ") ?? body);
}

/** What the card offers for an SSH runner, so the page does not carry that logic in its markup. */
export function sshActions(r: Pick<Runner, "status" | "ssh_host_key_fingerprint" | "ssh_host_key_pending_fingerprint">) {
  const state = hostKeyState(r);
  const busy = r.status === "provisioning";
  return {
    state,
    canCheck: !busy,
    canConfirm: state === "pending" && !busy,
    canForget: (state === "trusted" || state === "changed") && !busy,
    canInstall: state === "trusted" && !busy && r.status !== "online",
    busy,
  };
}
