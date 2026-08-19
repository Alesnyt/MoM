import type { AuthStatus, Health, Meeting, PlatformUser, UserSession } from "./types";

async function parseError(response: Response): Promise<string> {
  try {
    const data = await response.json();
    if (typeof data?.detail === "string") return data.detail;
    if (Array.isArray(data?.detail)) {
      return data.detail.map((item: { msg?: string }) => item.msg).join("; ");
    }
  } catch {
    /* ignore */
  }
  return `Ошибка ${response.status}`;
}

export async function getHealth(): Promise<Health> {
  const response = await fetch("/api/health");
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function getAuthStatus(): Promise<AuthStatus> {
  const response = await fetch("/api/auth/status", { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function setupAdmin(username: string, password: string): Promise<AuthStatus> {
  const response = await fetch("/api/auth/setup", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function loginAdmin(username: string, password: string): Promise<AuthStatus> {
  const response = await fetch("/api/auth/login", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function logoutAdmin(): Promise<AuthStatus> {
  const response = await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function getUserSession(): Promise<UserSession> {
  const response = await fetch("/api/session", { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function loginUser(email: string, password: string): Promise<UserSession> {
  const response = await fetch("/api/session/login", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function logoutUser(): Promise<UserSession> {
  const response = await fetch("/api/session/logout", { method: "POST", credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function listUsers(): Promise<PlatformUser[]> {
  const response = await fetch("/api/admin/users", { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function createUser(
  email: string,
  archiveLimit: number,
): Promise<{ user: PlatformUser; password: string }> {
  const response = await fetch("/api/admin/users", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, archive_limit: archiveLimit }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function updateUserLimit(id: string, archiveLimit: number): Promise<PlatformUser> {
  const response = await fetch(`/api/admin/users/${id}`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ archive_limit: archiveLimit }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function resetUserPassword(id: string): Promise<{ password: string }> {
  const response = await fetch(`/api/admin/users/${id}/reset-password`, {
    method: "POST",
    credentials: "include",
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function getSettings(): Promise<Health> {
  const response = await fetch("/api/settings", { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function saveApiKey(key: string): Promise<Health> {
  const response = await fetch("/api/settings", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ openai_api_key: key }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function saveModels(chatModel: string, asrModel: string): Promise<Health> {
  const response = await fetch("/api/settings/models", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_model: chatModel, asr_model: asrModel }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function verifyApiKey(): Promise<Health> {
  const response = await fetch("/api/settings/verify", { method: "POST", credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function deleteApiKey(): Promise<Health> {
  const response = await fetch("/api/settings", { method: "DELETE", credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function listMeetings(): Promise<Meeting[]> {
  const response = await fetch("/api/meetings", { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function getMeeting(id: string): Promise<Meeting> {
  const response = await fetch(`/api/meetings/${id}`, { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function createMeeting(file: File, title: string): Promise<Meeting> {
  const body = new FormData();
  body.append("file", file);
  if (title.trim()) body.append("title", title.trim());
  const response = await fetch("/api/meetings", { method: "POST", credentials: "include", body });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function deleteMeeting(id: string): Promise<void> {
  const response = await fetch(`/api/meetings/${id}`, { method: "DELETE", credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
}

export async function retryMeeting(id: string): Promise<Meeting> {
  const response = await fetch(`/api/meetings/${id}/retry`, { method: "POST", credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export function openMeetingEmail(meeting: Meeting): void {
  if (!meeting.result) throw new Error("Протокол ещё не готов");
  const subject = `MoM: ${meeting.result.title || meeting.title}`;
  const body = buildEmailBody(meeting);
  const mailto = `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  if (mailto.length <= 1800) {
    window.location.assign(mailto);
    return;
  }
  downloadDraftEml(subject, body, meeting.result.title || meeting.title);
}

function buildEmailBody(meeting: Meeting): string {
  const result = meeting.result;
  if (!result) return "";
  const lines: string[] = [];
  lines.push(`Саммари встречи: ${result.title || meeting.title}`);
  lines.push("");
  if (result.date_hint) lines.push(`Дата: ${result.date_hint}`);
  if (result.participants?.length) {
    lines.push(`Участники: ${result.participants.join(", ")}`);
  }
  lines.push("");
  lines.push(result.summary?.trim() || "Саммари отсутствует.");
  if (result.key_points?.length) {
    lines.push("");
    lines.push("Ключевые тезисы:");
    for (const item of result.key_points) lines.push(`• ${item}`);
  }
  if (result.decisions?.length) {
    lines.push("");
    lines.push("Решения:");
    for (const item of result.decisions) lines.push(`• ${item}`);
  }
  const actions = result.action_items || [];
  lines.push("");
  lines.push("Поручения:");
  if (!actions.length) {
    lines.push("Поручений не зафиксировано.");
  } else {
    for (const item of actions) {
      const who = item.assignee || "не назначен";
      const due = item.due || "без срока";
      lines.push(`• ${item.task} — ${who}; срок: ${due}`);
    }
  }
  const next = result.mom?.next_meeting;
  if (next) {
    lines.push("");
    lines.push(`Следующая встреча: ${next}`);
  }
  return lines.join("\n").trim() + "\n";
}

function downloadDraftEml(subject: string, body: string, title: string): void {
  const encodedSubject = rfc2047(subject);
  const eml = [
    `Subject: ${encodedSubject}`,
    "MIME-Version: 1.0",
    "Content-Type: text/plain; charset=UTF-8",
    "Content-Transfer-Encoding: 8bit",
    "X-Unsent: 1",
    "",
    body.replace(/\n/g, "\r\n"),
    "",
  ].join("\r\n");
  const blob = new Blob([eml], { type: "message/rfc822" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${slug(title)}.eml`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function rfc2047(value: string): string {
  if (/^[\x20-\x7e]*$/.test(value)) return value;
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return `=?UTF-8?B?${btoa(binary)}?=`;
}

export async function downloadMarkdown(id: string, title: string): Promise<void> {
  const response = await fetch(`/api/meetings/${id}/export.md`, { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  const text = await response.text();
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${slug(title)}.md`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function slug(value: string): string {
  return (
    value
      .toLowerCase()
      .replace(/[^a-z0-9а-яё]+/gi, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 60) || "mom"
  );
}
