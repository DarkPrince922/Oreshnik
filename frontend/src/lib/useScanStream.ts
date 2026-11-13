import { useEffect, useRef, useState } from "react";

import { apiWsUrl, getToken, notifyUnauthorized, type ScanStatus } from "./api";
import { LogBuffer } from "./logStream";

export type StreamState = "connecting" | "live" | "ended" | "error";

interface Options {
  /** Called for every status frame the server sends (including the initial one). */
  onStatus?: (status: ScanStatus) => void;
}

/**
 * Live log of a scan over WebSocket.
 *
 * Protocol (see backend/app/api/scans.py): the first client frame authenticates and says where to
 * resume; the server replays the log from there, then tails it, and finally sends `end`.
 * If the connection drops before `end`, we reconnect with `from = nextSeq`, so nothing is lost or
 * duplicated (LogBuffer drops replays anyway).
 */
export function useScanStream(scanId: number, { onStatus }: Options = {}) {
  const bufferRef = useRef<LogBuffer>(new LogBuffer());
  const onStatusRef = useRef(onStatus);
  onStatusRef.current = onStatus;
  const controls = useRef<{ restart: () => void }>({ restart: () => undefined });
  const [, setVersion] = useState(0);
  const [state, setState] = useState<StreamState>("connecting");

  useEffect(() => {
    const buffer = new LogBuffer();
    bufferRef.current = buffer;
    setVersion((v) => v + 1);
    setState("connecting");

    let socket: WebSocket | null = null;
    let disposed = false;
    let ended = false;
    let retries = 0;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let renderTimer: ReturnType<typeof setTimeout> | undefined;

    // Lines can arrive in bursts (a finished scan replays thousands); render at most ~12 times a second.
    const scheduleRender = () => {
      if (renderTimer) return;
      renderTimer = setTimeout(() => {
        renderTimer = undefined;
        setVersion((v) => v + 1);
      }, 80);
    };

    const connect = () => {
      if (disposed) return;
      setState("connecting");
      const ws = new WebSocket(apiWsUrl(`/api/scans/${scanId}/ws`));
      socket = ws;

      ws.onopen = () => ws.send(JSON.stringify({ token: getToken(), from: buffer.nextSeq }));

      ws.onmessage = (event) => {
        let frame: { type?: string; seq?: number; line?: string; status?: ScanStatus };
        try {
          frame = JSON.parse(event.data);
        } catch {
          return;
        }
        retries = 0;
        if (frame.type === "log" && typeof frame.seq === "number" && typeof frame.line === "string") {
          if (buffer.add(frame.seq, frame.line)) scheduleRender();
        } else if (frame.type === "status" && frame.status) {
          setState("live");
          onStatusRef.current?.(frame.status);
        } else if (frame.type === "end") {
          ended = true;
          setState("ended");
          if (frame.status) onStatusRef.current?.(frame.status);
          scheduleRender();
        }
      };

      ws.onclose = (event) => {
        if (disposed || ended) return;
        if (event.code === 4401) {
          notifyUnauthorized(); // token rejected: the session is over
          setState("error");
          return;
        }
        if (event.code === 4404) {
          setState("error"); // the scan is gone or not visible
          return;
        }
        // Dropped mid-scan: back off and resume where we stopped.
        setState("connecting");
        const delay = Math.min(8000, 400 * 2 ** retries++);
        retryTimer = setTimeout(connect, delay);
      };
    };

    // A scan continued in place goes back to work after the stream has ended: reconnect, keeping what is on screen
    // (the server replays from where we stopped, so nothing is duplicated).
    controls.current.restart = () => {
      if (disposed || (socket && socket.readyState <= WebSocket.OPEN && !ended)) return;
      ended = false;
      retries = 0;
      clearTimeout(retryTimer);
      connect();
    };

    connect();
    return () => {
      disposed = true;
      clearTimeout(retryTimer);
      clearTimeout(renderTimer);
      socket?.close();
    };
  }, [scanId]);

  return { buffer: bufferRef.current, state, restart: () => controls.current.restart() };
}
