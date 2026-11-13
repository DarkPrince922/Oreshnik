import { useCallback, useEffect, useRef, useState } from "react";

import { errorMessage, getActivity, type ActivityEvent, type AgentInfo } from "./api";

const POLL_MS = 1500;
const MAX_EVENTS = 30_000;

interface State {
  events: ActivityEvent[];
  agents: AgentInfo[];
  loaded: boolean;
  error: string | null;
}

/**
 * The agents' work (commands, messages, sub-agents) of one scan.
 *
 * The API serves an append-only stream addressed by a byte cursor, so every poll fetches only what is new
 * and a page boundary can never lose or repeat an event. While the scan is active we poll every 1.5 s; when
 * it stops being active the effect re-runs once, which drains whatever the worker flushed at the end.
 */
export function useScanActivity(scanId: number, { enabled, active }: { enabled: boolean; active: boolean }) {
  const [state, setState] = useState<State>({ events: [], agents: [], loaded: false, error: null });
  const cursor = useRef(0);
  const lastSeq = useRef(-1);
  const busy = useRef(false);

  const drain = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    try {
      for (;;) {
        const page = await getActivity(scanId, cursor.current);
        cursor.current = page.next_cursor;
        const fresh = page.events.filter((e) => e.seq > lastSeq.current);
        if (fresh.length) lastSeq.current = fresh[fresh.length - 1].seq;
        setState((s) => ({
          events: fresh.length ? s.events.concat(fresh).slice(-MAX_EVENTS) : s.events,
          agents: page.agents,
          loaded: true,
          error: null,
        }));
        if (page.events.length === 0) break; // caught up
      }
    } catch (e) {
      setState((s) => ({ ...s, loaded: true, error: errorMessage(e) }));
    } finally {
      busy.current = false;
    }
  }, [scanId]);

  useEffect(() => {
    if (!enabled) return;
    void drain();
    if (!active) return;
    const id = setInterval(() => !document.hidden && void drain(), POLL_MS);
    return () => clearInterval(id);
  }, [enabled, active, drain]);

  return state;
}
