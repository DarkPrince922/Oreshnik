/** In-app confirmation that the browser cannot suppress (unlike window.confirm, which a "do not create dialogs" checkbox silently
 * forces to false). `confirm(...)` returns a Promise<boolean>; a mounted <ConfirmHost/> shows the dialog. Falls back to window.confirm
 * only if no host is mounted. */
export interface ConfirmRequest {
  message: string;
  resolve: (ok: boolean) => void;
}

let listener: ((req: ConfirmRequest | null) => void) | null = null;

export function setConfirmListener(fn: ((req: ConfirmRequest | null) => void) | null): void {
  listener = fn;
}

export function confirm(message: string): Promise<boolean> {
  return new Promise((resolve) => {
    if (listener) listener({ message, resolve });
    else resolve(window.confirm(message)); // no host mounted (e.g. a test): fall back
  });
}
