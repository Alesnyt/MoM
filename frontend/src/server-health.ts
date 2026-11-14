import { useCallback, useEffect, useRef, useState, type SetStateAction } from "react";
import { getHealth } from "./api";
import type { Health } from "./types";

/** Keep the last real capabilities; a transport failure is not a missing key/ffmpeg. */
export function useServerHealth() {
  const [health, setValue] = useState<Health | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const version = useRef(0);
  const checkRef = useRef<() => void>(() => undefined);
  const recheck = useCallback(() => checkRef.current(), []);
  const setHealth = useCallback((value: SetStateAction<Health | null>) => {
    version.current += 1;
    setValue(value);
    setUnavailable(false);
  }, []);

  useEffect(() => {
    let disposed = false;
    let inFlight = false;
    let controller: AbortController | undefined;
    let timer: number | undefined;
    let timeout: number | undefined;
    async function check() {
      if (disposed || inFlight) return;
      window.clearTimeout(timer);
      inFlight = true;
      const request = new AbortController();
      controller = request;
      const started = version.current;
      timeout = window.setTimeout(() => request.abort(), 10_000);
      let delay = 30_000;
      try {
        const next = await getHealth(request.signal);
        if (request.signal.aborted) throw new Error("Health request timed out");
        if (!disposed && started === version.current) {
          setValue(next);
          setUnavailable(false);
        }
      } catch {
        if (!disposed && started === version.current) setUnavailable(true);
        delay = 5_000;
      } finally {
        window.clearTimeout(timeout);
        inFlight = false;
        if (!disposed) timer = window.setTimeout(check, delay);
      }
    }
    checkRef.current = () => { void check(); };
    window.addEventListener("online", recheck);
    window.addEventListener("focus", recheck);
    void check();
    return () => {
      disposed = true;
      checkRef.current = () => undefined;
      window.clearTimeout(timer);
      window.clearTimeout(timeout);
      controller?.abort();
      window.removeEventListener("online", recheck);
      window.removeEventListener("focus", recheck);
    };
  }, [recheck]);

  return { health, setHealth, unavailable, recheck };
}
