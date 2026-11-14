/** Small helpers for the session's file browser. */

export function joinPath(base: string, name: string): string {
  return base ? `${base}/${name}` : name;
}

export function parentPath(path: string): string {
  const cut = path.lastIndexOf("/");
  return cut < 0 ? "" : path.slice(0, cut);
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} Б`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} КБ`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(n < 10 * 1024 * 1024 ? 1 : 0)} МБ`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(1)} ГБ`;
}
