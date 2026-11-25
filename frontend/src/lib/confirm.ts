/** A single confirmation host presents requests in order. */
export interface ConfirmRequest {
  message: string;
  resolve: (ok: boolean) => void;
}

let listener: ((req: ConfirmRequest | null) => void) | null = null;
const pending: ConfirmRequest[] = [];

export function setConfirmListener(fn: ((req: ConfirmRequest | null) => void) | null): void {
  listener = fn;
  if (!fn) {
    for (const req of pending.splice(0)) req.resolve(false);
  }
}

export function confirm(message: string): Promise<boolean> {
  if (!listener) return Promise.resolve(window.confirm(message));
  return new Promise((resolve) => {
    const req: ConfirmRequest = {
      message,
      resolve(ok) {
        resolve(ok);
        if (pending[0] !== req) return;
        pending.shift();
        listener?.(pending[0] ?? null);
      },
    };
    pending.push(req);
    if (pending.length === 1) listener?.(req);
  });
}
