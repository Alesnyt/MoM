/** Poll only after the previous request settles; ignore responses after cleanup. */
export function startPolling<T>(
  request: (signal: AbortSignal) => Promise<T>,
  onValue: (value: T) => void,
  intervalMs = 2000,
  { immediate = false }: { immediate?: boolean } = {},
): () => void {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout>;
  async function run() {
    try {
      const value = await request(controller.signal);
      if (!controller.signal.aborted) onValue(value);
    } catch {
      // Preserve the last known state and retry after the normal interval.
    } finally {
      if (!controller.signal.aborted) timer = setTimeout(run, intervalMs);
    }
  }
  timer = setTimeout(run, immediate ? 0 : intervalMs);
  return () => { controller.abort(); clearTimeout(timer); };
}
