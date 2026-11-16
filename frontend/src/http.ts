export type SessionScope = "user" | "admin";

export class ApiError extends Error {
  constructor(message: string, public readonly status: number, public readonly session?: SessionScope) {
    super(message);
    this.name = "ApiError";
  }
}

const listeners = new Set<(scope: SessionScope) => void>();
const revisions = { user: 0, admin: 0 };

export function sessionChanged(scope: SessionScope) { revisions[scope] += 1; }

export function onSessionExpired(listener: (scope: SessionScope) => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export async function parseError(response: Response): Promise<string> {
  try {
    const data = await response.json();
    if (typeof data?.detail === "string") return data.detail;
    if (Array.isArray(data?.detail)) {
      return data.detail.map((item: { msg?: string }) => item.msg).join("; ");
    }
  } catch { /* A proxy can return HTML instead of an API error. */ }
  return `Ошибка ${response.status}`;
}

function sessionFor(path: string): SessionScope | undefined {
  if (/^\/api\/(admin|settings)(\/|\?|$)/.test(path)) return "admin";
  if (/^\/api\/(meetings|people|calendar)(\/|\?|$)/.test(path)) return "user";
  // Login failures must remain in the login form, not expire another session.
  return undefined;
}

export async function apiRequest(path: string, options: RequestInit = {}, allowedStatuses: number[] = []): Promise<Response> {
  const scope = sessionFor(path);
  const revision = scope ? revisions[scope] : 0;
  const response = await fetch(path, options);
  if (response.ok || allowedStatuses.includes(response.status)) return response;
  const message = await parseError(response);
  let expired = response.status === 401 && scope !== undefined;
  if (expired && path.startsWith("/api/calendar/")) {
    // Exchange also uses 401 for its own disconnected/expired credentials.
    try {
      const session = await fetch("/api/session", { credentials: "include", signal: options.signal });
      expired = session.ok && (await session.json()).authenticated === false;
    } catch { expired = false; }
  }
  if (expired && scope && !options.signal?.aborted && revision === revisions[scope]) {
    revisions[scope] += 1;
    listeners.forEach((listener) => listener(scope));
  }
  throw new ApiError(message, response.status, expired ? scope : undefined);
}
