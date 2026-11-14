import { useEffect, useState } from "react";

import { apiWsUrl, getToken, notifyUnauthorized, type AgentEvent, type AgentSessionStatus } from "./api";

export type AgentStreamState = "connecting" | "live" | "error";
export type LiveItems = Record<string, Record<string, unknown>>;

/**
 * Live view of an agent session over WebSocket (protocol: backend/app/api/agent.py).
 *
 * Two kinds of frames: `event` (history; numbered, so a replayed one is ignored and nothing is lost or shown twice after a reconnect) and
 * `live` / `live_end` (an item still being written, as it is so far, and the end of it). Unlike a scan the stream never "ends" — the session
 * waits for the next message — so the socket stays open and a drop is always followed by a reconnect that resumes from the next unseen event.
 */
export function useAgentStream(sessionId: number | null) {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [live, setLive] = useState<LiveItems>({});
  const [status, setStatus] = useState<AgentSessionStatus>("idle");
  const [state, setState] = useState<AgentStreamState>("connecting");

  useEffect(() => {
    setEvents([]);
    setLive({});
    setStatus("idle");
    if (sessionId === null) return;
    setState("connecting");

    let socket: WebSocket | null = null;
    let disposed = false;
    let retries = 0;
    let next = 0;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let flushTimer: ReturnType<typeof setTimeout> | undefined;
    let pendingEvents: AgentEvent[] = [];
    let pendingLive: LiveItems | null = null; // the latest snapshots, applied at the next flush

    // A long history replays in a burst and snapshots arrive many times a second: render at most ~12 times a second.
    const flush = () => {
      flushTimer = undefined;
      const batch = pendingEvents;
      pendingEvents = [];
      if (batch.length) setEvents((old) => old.concat(batch));
      if (pendingLive) {
        setLive(pendingLive);
        pendingLive = null;
      }
    };
    const schedule = () => {
      flushTimer ??= setTimeout(flush, 80);
    };
    let current: LiveItems = {};

    const connect = () => {
      if (disposed) return;
      setState("connecting");
      const ws = new WebSocket(apiWsUrl(`${"/api/agent/sessions"}/${sessionId}/ws`));
      socket = ws;
      ws.onopen = () => ws.send(JSON.stringify({ token: getToken(), from: next }));
      ws.onmessage = (message) => {
        if (disposed || socket !== ws) return; // a connection we already gave up on must not feed the list
        let frame: { type?: string; seq?: number; kind?: string; key?: string; payload?: Record<string, unknown>; status?: AgentSessionStatus };
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
          pendingEvents.push({ seq: frame.seq, kind: frame.kind, payload: frame.payload ?? {} });
          schedule();
        } else if (frame.type === "live" && typeof frame.key === "string") {
          current = { ...current, [frame.key]: frame.payload ?? {} };
          pendingLive = current;
          schedule();
        } else if (frame.type === "live_end" && typeof frame.key === "string") {
          const { [frame.key]: _gone, ...rest } = current;
          current = rest;
          pendingLive = current;
          schedule();
        }
      };
      ws.onclose = (event) => {
        if (disposed) return;
        current = {}; // what was being written is asked for again by the next connection
        pendingLive = current;
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

  return { events, live, status, state };
}
