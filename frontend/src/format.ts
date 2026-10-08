import type { OpenAIStatus } from "./types";

export function count(items: unknown[]): string {
  return items.length ? ` · ${items.length}` : "";
}

export function priorityLabel(value: string): string {
  if (value === "high") return "высокий";
  if (value === "low") return "низкий";
  return "средний";
}

export function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatDuration(seconds: number): string {
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours) return `${hours} ч ${minutes} мин`;
  return `${minutes} мин`;
}

export function formatClock(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

export function connectionClass(status: OpenAIStatus | undefined): string {
  if (!status?.configured) return "missing";
  if (status.connected) return "ok";
  return "bad";
}

export function connectionLabel(status: OpenAIStatus | undefined): string {
  const name = status?.provider_label || "API";
  if (!status?.configured) return "Ключ не задан";
  if (status.connected) return `${name} подключён`;
  return `${name}: ключ не принят`;
}

export function queueFromMeetings(
  meetings: { status: string }[],
  limit: number,
): { active: number; waiting: number; limit: number } {
  let active = 0;
  let waiting = 0;
  for (const item of meetings) {
    if (item.status === "queued") waiting += 1;
    else if (item.status === "extracting" || item.status === "transcribing" || item.status === "analyzing") {
      active += 1;
    }
  }
  return { active, waiting, limit };
}
