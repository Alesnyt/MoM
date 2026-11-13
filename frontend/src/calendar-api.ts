import { parseError } from "./api";
import type { CalendarEvent, CalendarStatus, ExchangeSettings } from "./calendar-types";

async function call<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { credentials: "include", ...options });
  if (!response.ok) throw Object.assign(new Error(await parseError(response)), { status:response.status });
  return response.json();
}
const json = (body: unknown): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const getExchangeSettings = (signal?: AbortSignal) => call<ExchangeSettings>("/api/settings/exchange", { signal });
export const saveExchangeSettings = (settings: ExchangeSettings) => call<ExchangeSettings>("/api/settings/exchange", json(settings));
export const calendarStatus = (signal?: AbortSignal) => call<CalendarStatus>("/api/calendar/status", { signal });
export const connectCalendar = (username: string, password: string, signal?: AbortSignal) =>
  call<CalendarStatus>("/api/calendar/connect", { ...json({ username, password }), signal });
export const disconnectCalendar = () => call<{ connected: false }>("/api/calendar/connection", { method: "DELETE" });
export const calendarEvents = (start: string, end: string, refresh: boolean, signal?: AbortSignal) =>
  call<{ events: CalendarEvent[]; truncated: boolean; cached_seconds: number }>(`/api/calendar/events?${new URLSearchParams({ start, end, refresh: String(refresh) })}`, { signal });
export const calendarEvent = (id: string, signal?: AbortSignal) => call<CalendarEvent>(`/api/calendar/events/${encodeURIComponent(id)}`, { signal });
