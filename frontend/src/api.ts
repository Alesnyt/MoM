import type { AuditEvent, AuthStatus, Health, Meeting, MonitoringSnapshot, PlatformUser, ProtocolRecipient, UserSession } from "./types";

export async function parseError(response: Response): Promise<string> {
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

export type CorporateTerm = { term: string; aliases: string[]; description: string; department: string };
export type DirectoryPolicy = { fields: string[]; domains: string[]; ldap_filter: string };
export type SpeakerSearchMode = "live" | "cache";
export type CorporateContext = {
  enabled: boolean; use_ad: boolean; sync_ad: boolean; revision: number; term_count: number; people_count: number;
  directory_current: boolean; ldap_configured: boolean; synced_at: string | null; imported_at: string | null;
  sync_error: string | null; syncing: boolean; terms: CorporateTerm[]; matched_terms: number;
  sync_time: string; sync_timezone: string; next_sync_at: string | null;
  directory_policy: DirectoryPolicy;
  search_mode: SpeakerSearchMode; search_cache_ready: boolean;
  sync_progress: {status: "idle" | "queued" | "running" | "succeeded" | "error" | "interrupted";
    phase?: string; received?: number; pages?: number; scopes_done?: number; scopes_total?: number;
    current_base?: string | null; started_at?: string | null; finished_at?: string | null};
};

export async function getCorporateContext(query: string, signal?: AbortSignal): Promise<CorporateContext> {
  const response = await fetch(`/api/admin/context?q=${encodeURIComponent(query)}`, {credentials: "include", signal});
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function saveCorporateContext(enabled: boolean, use_ad: boolean): Promise<CorporateContext> {
  const response = await fetch("/api/admin/context/settings", {method: "POST", credentials: "include",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({enabled, use_ad})});
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function syncCorporateDirectory(): Promise<CorporateContext> {
  const response = await fetch("/api/admin/directory/sync", {method: "POST", credentials: "include"});
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function saveDirectoryPolicy(policy: DirectoryPolicy): Promise<CorporateContext> {
  const response = await fetch("/api/admin/directory/policy", {method: "POST", credentials: "include",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify(policy)});
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function saveSpeakerSearchMode(mode: SpeakerSearchMode): Promise<CorporateContext> {
  const response = await fetch("/api/admin/directory/search", {method: "POST", credentials: "include",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({mode})});
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function importCorporateDictionary(file: File): Promise<CorporateContext> {
  const body = new FormData();
  body.append("file", file);
  const response = await fetch("/api/admin/context/import", {method: "POST", credentials: "include", body});
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function getDirectoryCache(signal?: AbortSignal): Promise<CorporateContext> {
  const response = await fetch("/api/admin/directory/cache", {credentials: "include", signal});
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function saveDirectorySchedule(settings: {sync_ad?: boolean; sync_time?: string}): Promise<CorporateContext> {
  const response = await fetch("/api/admin/directory/settings", {method: "POST", credentials: "include",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify(settings)});
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function getHealth(signal?: AbortSignal): Promise<Health> {
  const response = await fetch("/api/health", { signal, cache: "no-store" });
  if (!response.ok && response.status !== 503) throw new Error(await parseError(response));
  const state = await response.json();
  // Readiness failures still carry the real capabilities needed by the UI.
  if (typeof state?.ok !== "boolean" || typeof state?.ffmpeg !== "boolean" ||
      typeof state?.openai?.connected !== "boolean" ||
      !Number.isFinite(state?.max_upload_bytes) || state.max_upload_bytes <= 0) {
    throw new Error("Не удалось получить состояние сервера");
  }
  return state;
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

export async function listAudit(limit = 20): Promise<AuditEvent[]> {
  const response = await fetch(`/api/admin/audit?limit=${limit}`, { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function downloadAudit(format: "csv" | "xlsx"): Promise<void> {
  const response = await fetch(`/api/admin/audit/export?format=${format}`, { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = format === "xlsx" ? "mom-zhurnal.xlsx" : "mom-zhurnal.csv";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export async function listUsers(): Promise<PlatformUser[]> {
  const response = await fetch("/api/admin/users", { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export type DirectoryPerson = {
  name: string;
  email: string;
};

export async function listDirectory(query: string): Promise<{ people: DirectoryPerson[]; ldap: boolean; error?: string }> {
  const response = await fetch(`/api/admin/directory?q=${encodeURIComponent(query)}`, { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  const data = await response.json();
  return { people: data.people ?? [], ldap: Boolean(data.ldap), error: data.error || undefined };
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

export async function getMonitoring(signal: AbortSignal): Promise<MonitoringSnapshot> {
  const response = await fetch("/api/admin/monitoring", { credentials: "include", signal });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function saveApiKey(key: string): Promise<Health> {
  const response = await fetch("/api/settings?verify=false", {
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

export async function listChatModels(): Promise<string[]> {
  const response = await fetch("/api/settings/chat-models", { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  const data = await response.json();
  return data.manual ? [] : (data.models ?? []);
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

export async function verifyApiKey(signal: AbortSignal): Promise<Health> {
  const response = await fetch("/api/settings/verify", { method: "POST", credentials: "include", signal });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function deleteApiKey(): Promise<Health> {
  const response = await fetch("/api/settings", { method: "DELETE", credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function listMeetings(signal?: AbortSignal): Promise<Meeting[]> {
  const response = await fetch("/api/meetings", { credentials: "include", signal });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function getMeeting(id: string, signal?: AbortSignal): Promise<Meeting> {
  const response = await fetch(`/api/meetings/${id}`, { credentials: "include", signal });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function createMeeting(file: File, title: string, calendarEventId?: string): Promise<Meeting> {
  const body = new FormData();
  body.append("file", file);
  if (title.trim()) body.append("title", title.trim());
  if (calendarEventId) body.append("calendar_event_id", calendarEventId);
  const response = await fetch("/api/meetings", { method: "POST", credentials: "include", body });
  if (!response.ok) throw new Error(await parseError(response));
  const created: Partial<Meeting> | null = await response.json();
  if (!created || typeof created.id !== "string" || !created.id || typeof created.status !== "string") {
    throw new Error("Сервер не создал встречу: API загрузки вернул некорректный ответ.");
  }
  return created as Meeting;
}

export async function createTranscript(file: File | null, text: string, title: string, calendarEventId?: string): Promise<Meeting> {
  const body = new FormData();
  if (file) body.append("file", file);
  else body.append("text", text);
  if (title.trim()) body.append("title", title.trim());
  if (calendarEventId) body.append("calendar_event_id", calendarEventId);
  const response = await fetch("/api/meetings/transcript", { method: "POST", credentials: "include", body });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function deleteMeeting(id: string): Promise<void> {
  const response = await fetch(`/api/meetings/${id}`, { method: "DELETE", credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
}

export async function startProtocol(id: string, recipients: ProtocolRecipient[] = []): Promise<Meeting> {
  const response = await fetch(`/api/meetings/${id}/protocol`, {
    method: "POST", credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ recipients }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function stopMeeting(id: string): Promise<Meeting> {
  const response = await fetch(`/api/meetings/${id}/stop`, { method: "POST", credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function restartMeeting(id: string): Promise<Meeting> {
  const response = await fetch(`/api/meetings/${id}/restart`, { method: "POST", credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function retryMeeting(id: string): Promise<Meeting> {
  const response = await fetch(`/api/meetings/${id}/retry`, { method: "POST", credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export type Person = {
  name: string;
  email: string;
  source: "mom" | "ldap" | "calendar";
  job_title?: string;
};

export async function listPeople(query: string, signal?: AbortSignal): Promise<{people: Person[]; ldap: boolean; error?: string; search_mode?: SpeakerSearchMode}> {
  const response = await fetch(`/api/people?q=${encodeURIComponent(query)}`, { credentials: "include", signal });
  if (!response.ok) throw new Error(await parseError(response));
  const data = await response.json();
  return {people: data.people ?? [], ldap: Boolean(data.ldap), error: data.error, search_mode: data.search_mode};
}

export async function renameSpeaker(id: string, speakerId: string, name: string, email?: string, jobTitle?: string): Promise<Meeting> {
  const response = await fetch(`/api/meetings/${id}/speakers/${encodeURIComponent(speakerId)}`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, email, job_title: jobTitle }),
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

export async function downloadMeetingEmail(meeting: Meeting): Promise<void> {
  await downloadFile(`/api/meetings/${meeting.id}/email.eml`, `${slug(meeting.title)}.eml`);
}

export async function downloadProtocol(id: string, title: string, format: "docx" | "md"): Promise<void> {
  await downloadFile(`/api/meetings/${id}/export.${format}`, `${slug(title)}.${format}`);
}

async function downloadFile(path: string, filename: string): Promise<void> {
  const response = await fetch(path, { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
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

export type PromptSettings = {
  version: number;
  protocol_prompt: string;
  name_review_prompt: string;
  name_review_enabled: boolean;
  defaults: { protocol_prompt: string; name_review_prompt: string };
};

export async function getPrompts(): Promise<PromptSettings> {
  const response = await fetch("/api/settings/prompts", { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function savePrompts(settings: PromptSettings): Promise<PromptSettings> {
  const response = await fetch("/api/settings/prompts", {
    method: "POST", credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ protocol_prompt: settings.protocol_prompt,
      name_review_prompt: settings.name_review_prompt, name_review_enabled: settings.name_review_enabled }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}


export type DirectoryScope = { mode: "all" | "selected"; bases: string[]; connection_matches: boolean };
export type DirectoryArea = { dn: string; name: string; kind: string };
export type DirectoryStructure = { items: DirectoryArea[]; next_cursor: string | null; warning: string | null };

export async function getDirectoryScope(): Promise<DirectoryScope> {
  const response = await fetch("/api/settings/ldap/scopes", { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function saveDirectoryScope(value: DirectoryScope): Promise<DirectoryScope> {
  const response = await fetch("/api/settings/ldap/scopes", { method: "POST", credentials: "include",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: value.mode, bases: value.bases }) });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function browseDirectory(baseDn = "", cursor = ""): Promise<DirectoryStructure> {
  const query = new URLSearchParams({ base_dn: baseDn, cursor });
  const response = await fetch(`/api/settings/ldap/structure?${query}`, { credentials: "include" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export type GlossaryMapping = Partial<Record<'term' | 'aliases' | 'replacements' | 'description' | 'department', string>>;
export type Glossary = {id: string; name: string; enabled: boolean; version: number; term_count: number;
  mapping: GlossaryMapping; sheet: string; filename: string; updated_at: string | null};
export type GlossaryJob = {id: string; glossary_id: string; filename: string; status: 'queued'|'running'|'succeeded'|'error';
  received: number; error: string | null; finished_at: string | null};
export type GlossaryCatalog = {glossaries: Glossary[]; terms: (CorporateTerm & {id: number; replacements: string[];
  glossary_name: string; enabled: boolean})[]; term_count: number; active_term_count: number; matched_terms: number;
  revision: number; jobs: GlossaryJob[]};
export type GlossaryPreview = {upload_id: string; columns: string[]; sample: string[][]; mapping: GlossaryMapping;
  sheets: string[]; sheet: string; name: string};

export async function getGlossaries(query = '', glossaryId = '', offset = 0, signal?: AbortSignal): Promise<GlossaryCatalog> {
  const response = await fetch(`/api/admin/glossaries?q=${encodeURIComponent(query)}&glossary_id=${encodeURIComponent(glossaryId)}&offset=${offset}`, {credentials:'include',signal});
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}
export async function previewGlossary(file: File, glossaryId: string, sheet: string): Promise<GlossaryPreview> {
  const body = new FormData(); body.append('file',file); body.append('glossary_id',glossaryId); body.append('sheet',sheet);
  const response = await fetch('/api/admin/glossaries/preview', {method:'POST',credentials:'include',body});
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}
export async function discardGlossaryPreview(id: string): Promise<void> {
  const response = await fetch(`/api/admin/glossaries/preview/${encodeURIComponent(id)}`, {method:'DELETE',credentials:'include'});
  if (!response.ok) throw new Error(await parseError(response));
}
export async function queueGlossary(preview: GlossaryPreview, name: string, mapping: GlossaryMapping): Promise<void> {
  const response = await fetch('/api/admin/glossaries/import', {method:'POST',credentials:'include',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({upload_id:preview.upload_id,name,mapping,sheet:preview.sheet})});
  if (!response.ok) throw new Error(await parseError(response));
}
export async function enableGlossary(id: string, enabled: boolean): Promise<void> {
  const response = await fetch(`/api/admin/glossaries/${encodeURIComponent(id)}/enabled`,{method:'POST',credentials:'include',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});
  if (!response.ok) throw new Error(await parseError(response));
}

export type TermVariant = {id:string;revision:number;variant:string;canonical:string;description:string;department:string;
  replace_text:number;meetings:number;state:string;examples:{id:number;meeting_id:string;title:string;excerpt:string;start:number|null;audio:boolean}[]};
export type TermProposal = {key:string;meetings:number;variants:TermVariant[]};
export type TermDiscovery = {job:{enabled:boolean;run_time:string;max_batches:number;next_run_at:string;
  status:string;batches:number;found:number;pending_meetings:number;error:string|null;started_at:string|null;finished_at:string|null;model:string|null};
  counts:Record<string,number>;proposals:TermProposal[];total:number};
export async function getTermDiscovery(state:string,offset:number,signal?:AbortSignal):Promise<TermDiscovery> {
  const response=await fetch(`/api/admin/term-discovery?state=${encodeURIComponent(state)}&offset=${offset}`,{credentials:'include',signal});
  if(!response.ok)throw new Error(await parseError(response));return response.json();
}
export async function termDiscoveryAction(action:'settings'|'scan'|'stop'|'review',payload:unknown={}) {
  const response=await fetch(`/api/admin/term-discovery/${action}`,{method:'POST',credentials:'include',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  if(!response.ok)throw new Error(await parseError(response));
}
