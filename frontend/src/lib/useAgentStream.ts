import { useEffect, useState } from "react";

import { apiWsUrl, getToken, notifyUnauthorized, type AgentEvent, type AgentSessionStatus } from "./api";

export type AgentStreamState = "connecting" | "live" | "error";

/**
 * Live events of an agent session over WebSocket (protocol: backend/app/api/agent.py).
 *
 * Unlike a scan the stream never "ends" — the session waits for the next message — so the socket stays open and a drop is
 * always followed by a reconnect that resumes from the next event we have not seen. Events carry a sequence number, so a
 * replayed one is ignored and nothing is lost or shown twice.
 */
export function useAgentStream(sessionId: number | null) {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [status, setStatus] = useState<AgentSessionStatus>("idle");
  const [state, setState] = useState<AgentStreamState>("connecting");

  useEffect(() => {
    setEvents([]);
    setStatus("idle");
    if (sessionId === null) return;
    setState("connecting");

    let socket: WebSocket | null = null;
    let disposed = false;
    let retries = 0;
    let next = 0;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let flushTimer: ReturnType<typeof setTimeout> | undefined;
    let pending: AgentEvent[] = [];

    // A long history replays in a burst: render at most ~12 times a second.
    const flush = () => {
      flushTimer = undefined;
      const batch = pending;
      pending = [];
      if (batch.length) setEvents((old) => old.concat(batch));
    };

    const connect = () => {
      if (disposed) return;
      setState("connecting");
      const ws = new WebSocket(apiWsUrl(`/api/agent/sessions/${sessionId}/ws`));
      socket = ws;
      ws.onopen = () => ws.send(JSON.stringify({ token: getToken(), from: next }));
      ws.onmessage = (message) => {
        if (disposed || socket !== ws) return; // a connection we already gave up on must not feed the list
        let frame: { type?: string; seq?: number; kind?: string; payload?: Record<string, unknown>; status?: AgentSessionStatus };
        try {
          frame = JSON.parse(message.data);
        } catch {
          return;
        }
        retries = 0;
        if (frame.type === "status" && frame.status) {
          setState("live");
          setStatus(frame.status);
        } else if (frame.type === "event" && typeof frame.seq === "number" && frame.seq === next && typeof frame.kind === "string") {
          next = frame.seq + 1;
          pending.push({ seq: frame.seq, kind: frame.kind, payload: frame.payload ?? {} });
          flushTimer ??= setTimeout(flush, 80);
        }
      };
      ws.onclose = (event) => {
        if (disposed) return;
        if (event.code === 4401) {
          notifyUnauthorized();
          setState("error");
          return;
        }
        if (event.code === 4404) {
          setState("error");
          return;
        }
        setState("connecting");
        retryTimer = setTimeout(connect, Math.min(8000, 400 * 2 ** retries++));
      };
    };

    connect();
    return () => {
      disposed = true;
      clearTimeout(retryTimer);
      clearTimeout(flushTimer);
      socket?.close();
    };
  }, [sessionId]);

  return { events, status, state };
}
