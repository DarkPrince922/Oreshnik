import { useEffect, useRef, useState } from "react";

import { getAttention, type Attention } from "./api";
import { lights, readSeen, sectionOf, writeSeen, type Light, type Section } from "./attention";

const POLL_MS = 8000;

/** Keeps the menu's lights up to date: asks every few seconds what is new since each section was last opened, and marks the section
 * the user is on as seen. The very first time (nothing remembered yet) everything counts as seen, so old scans do not light up. */
export function useAttention(userId: number | null, pathname: string): Partial<Record<Section, Light>> {
  const [attention, setAttention] = useState<Attention | null>(null);
  const current = sectionOf(pathname);
  const currentRef = useRef(current);
  currentRef.current = current;

  useEffect(() => {
    if (userId === null) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async () => {
      try {
        const scansSince = readSeen(userId, "scans");
        const agentSince = readSeen(userId, "agent");
        const result = await getAttention({ ...(scansSince ? { scans_since: scansSince } : {}), ...(agentSince ? { agent_since: agentSince } : {}) });
        if (disposed) return;
        if (!scansSince || currentRef.current === "scans") writeSeen(userId, "scans", result.now);
        if (!agentSince || currentRef.current === "agent") writeSeen(userId, "agent", result.now);
        setAttention(result);
      } catch {
        /* the API is away: keep what is shown, try again */
      }
      if (!disposed) timer = setTimeout(tick, document.hidden ? POLL_MS * 4 : POLL_MS);
    };
    void tick();
    return () => {
      disposed = true;
      clearTimeout(timer);
    };
  }, [userId]);

  // Opening a section turns its light off at once (the next poll confirms it against the server's clock).
  useEffect(() => {
    if (userId !== null && current && attention) writeSeen(userId, current, attention.now);
  }, [userId, current, attention]);

  return lights(attention, current);
}
