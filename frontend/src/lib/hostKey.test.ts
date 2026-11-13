import { describe, expect, it } from "vitest";

import { authLabel, groupFingerprint, hasLogin, hostKeyState, shortFingerprint, sshActions } from "./hostKey";

const FP = "SHA256:WbG543CEp/v2MBcF1fK4H+yLpG2/YatrKrRVq1dIE0w";
const OTHER = "SHA256:Zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz";

describe("hostKeyState", () => {
  it("tells the four situations apart", () => {
    expect(hostKeyState({ ssh_host_key_fingerprint: null, ssh_host_key_pending_fingerprint: null })).toBe("unknown");
    expect(hostKeyState({ ssh_host_key_fingerprint: null, ssh_host_key_pending_fingerprint: FP })).toBe("pending");
    expect(hostKeyState({ ssh_host_key_fingerprint: FP, ssh_host_key_pending_fingerprint: null })).toBe("trusted");
    expect(hostKeyState({ ssh_host_key_fingerprint: FP, ssh_host_key_pending_fingerprint: FP })).toBe("trusted");
    expect(hostKeyState({ ssh_host_key_fingerprint: FP, ssh_host_key_pending_fingerprint: OTHER })).toBe("changed");
  });
});

describe("fingerprints", () => {
  it("shortens only what is long and keeps both ends", () => {
    expect(shortFingerprint(FP)).toBe(`SHA256:${FP.slice(7, 17)}…${FP.slice(-10)}`);
    expect(shortFingerprint("SHA256:abc")).toBe("SHA256:abc");
  });
  it("groups the body in eights for reading aloud", () => {
    expect(groupFingerprint("SHA256:ABCDEFGHIJKLMNOPQ")).toBe("SHA256:ABCDEFGH IJKLMNOP Q");
    expect(groupFingerprint("ABCDEFGHI")).toBe("ABCDEFGH I");
  });
});

describe("sshActions", () => {
  const base = { ssh_host_key_fingerprint: null, ssh_host_key_pending_fingerprint: null };
  it("offers confirmation only for a key that waits for it, and never for a changed one", () => {
    expect(sshActions({ status: "offline", ...base, ssh_host_key_pending_fingerprint: FP }).canConfirm).toBe(true);
    expect(sshActions({ status: "error", ssh_host_key_fingerprint: FP, ssh_host_key_pending_fingerprint: OTHER }).canConfirm).toBe(false);
  });
  it("offers installation only for a trusted host that is not ready", () => {
    expect(sshActions({ status: "offline", ...base }).canInstall).toBe(false); // nothing is trusted yet
    expect(sshActions({ status: "offline", ssh_host_key_fingerprint: FP, ssh_host_key_pending_fingerprint: null }).canInstall).toBe(true);
    expect(sshActions({ status: "online", ssh_host_key_fingerprint: FP, ssh_host_key_pending_fingerprint: null }).canInstall).toBe(false);
  });
  it("offers dropping the pinned key, but not while an installation runs", () => {
    expect(sshActions({ status: "error", ssh_host_key_fingerprint: FP, ssh_host_key_pending_fingerprint: OTHER }).canForget).toBe(true);
    expect(sshActions({ status: "provisioning", ssh_host_key_fingerprint: FP, ssh_host_key_pending_fingerprint: null })).toMatchObject({
      canForget: false,
      canCheck: false,
      canInstall: false,
      busy: true,
    });
  });
});

describe("authLabel", () => {
  it("names the ways the panel logs in", () => {
    expect(authLabel({ has_ssh_key: true, has_ssh_password: false })).toBe("ключ");
    expect(authLabel({ has_ssh_key: false, has_ssh_password: true })).toBe("пароль");
    expect(authLabel({ has_ssh_key: true, has_ssh_password: true })).toBe("ключ + пароль");
    expect(authLabel({ has_ssh_key: false, has_ssh_password: false })).toBe("нет входа");
  });
});

describe("hasLogin", () => {
  const none = { key: "", password: "", hadKey: false, hadPassword: false, clearKey: false, clearPassword: false };
  it("needs a key or a password", () => {
    expect(hasLogin(none)).toBe(false);
    expect(hasLogin({ ...none, key: "-----BEGIN" })).toBe(true);
    expect(hasLogin({ ...none, password: "x" })).toBe(true);
    expect(hasLogin({ ...none, key: "   " })).toBe(false); // whitespace is not a key
  });
  it("counts what is already stored, unless it is being removed", () => {
    expect(hasLogin({ ...none, hadPassword: true })).toBe(true);
    expect(hasLogin({ ...none, hadPassword: true, clearPassword: true })).toBe(false);
    expect(hasLogin({ ...none, hadKey: true, hadPassword: true, clearKey: true })).toBe(true); // the password remains
    expect(hasLogin({ ...none, hadPassword: true, clearPassword: true, password: "new" })).toBe(true); // replaced in the same step
  });
});
