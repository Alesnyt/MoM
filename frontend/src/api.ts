import type { AuditEvent, AuthStatus, Health, Meeting, PlatformUser, UserSession } from "./types";

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

export async function setupAdmin(
  username: string,
  password: string,
  setupToken?: string,
): Promise<AuthStatus> {
  const response = await fetch("/api/auth/setup", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, setup_token: setupToken || "" }),
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

export async function listAudit(): Promise<AuditEvent[]> {
  const response = await fetch("/api/admin/audit", { credentials: "include" });
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
  authMode: "local" | "ldap" = "local",
): Promise<{ user: PlatformUser; password: string | null }> {
  const response = await fetch("/api/admin/users", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, archive_limit: archiveLimit, auth_mode: authMode }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function setUserAuthMode(
  id: string,
  authMode: "local" | "ldap",
): Promise<{ user: PlatformUser; password?: string }> {
  const response = await fetch(`/api/admin/users/${id}`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ auth_mode: authMode }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  const data = await response.json();
  if (data?.user) return data;
  return { user: data };
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

export async function deleteUser(id: string): Promise<void> {
  const response = await fetch(`/api/admin/users/${id}`, {
    method: "DELETE",
    credentials: "include",
  });
  if (!response.ok) throw new Error(await parseError(response));
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

export async function saveTheme(theme: "classic" | "t2"): Promise<Health> {
  const response = await fetch("/api/settings/theme", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ theme }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function saveSmtp(payload: {
  host: string;
  port: number;
  user: string;
  password: string;
  from_addr: string;
  starttls: boolean;
  public_url: string;
}): Promise<Health> {
  const response = await fetch("/api/settings/smtp", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function saveLdap(payload: {
  enabled: boolean;
  url: string;
  bind_dn: string;
  bind_password: string;
  base_dn: string;
  user_filter: string;
  starttls: boolean;
  tls_verify: boolean;
  email_attr: string;
}): Promise<Health> {
  const response = await fetch("/api/settings/ldap", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function testLdap(username: string): Promise<{ ok: boolean; entries?: number | null; mail?: string | null }> {
  const response = await fetch("/api/settings/ldap/test", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function saveQueue(maxJobs: number): Promise<Health> {
  const response = await fetch("/api/settings/queue", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ max_jobs: maxJobs }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function testSmtp(to: string): Promise<{ ok: boolean; to: string }> {
  const response = await fetch("/api/settings/smtp/test", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ to }),
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

export async function renameSpeaker(id: string, speakerId: string, name: string): Promise<Meeting> {
  const response = await fetch(`/api/meetings/${id}/speakers/${encodeURIComponent(speakerId)}`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function assignSpeaker(id: string, speakerId: string, segmentIndexes: number[]): Promise<Meeting> {
  const response = await fetch(`/api/meetings/${id}/segments`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ speaker_id: speakerId, segment_indexes: segmentIndexes }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function getEmailBody(id: string): Promise<string> {
  const response = await fetch(`/api/meetings/${id}/email.txt`, { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.text();
}

export async function openMeetingEmail(meeting: Meeting): Promise<string> {
  if (!meeting.result) throw new Error("Протокол ещё не готов");
  const subject = `MoM: ${meeting.result.title || meeting.title}`;
  const body = await getEmailBody(meeting.id);
  const draft = `${subject}\n\n${body}`;
  try {
    await navigator.clipboard.writeText(draft);
  } catch {
    /* браузер может запретить буфер вне HTTPS */
  }
  const mailto = `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  if (mailto.length <= 1800) {
    window.location.assign(mailto);
    return "opened-mailto";
  }
  const response = await fetch(`/api/meetings/${meeting.id}/email`, {
    method: "POST",
    credentials: "include",
  });
  if (!response.ok) throw new Error(await parseError(response));
  const payload = (await response.json()) as { mode?: string };
  return payload.mode || "saved";
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
