export type MeetingStatus =
  | "queued"
  | "extracting"
  | "transcribing"
  | "analyzing"
  | "done"
  | "error";

export type Tab = "overview" | "actions" | "mom" | "transcript";
export type AdminTab = "llm" | "asr" | "queue" | "smtp" | "ldap" | "theme" | "users";

export type ActionItem = {
  task: string;
  assignee: string | null;
  due: string | null;
  priority: "high" | "medium" | "low";
};

export type AgendaItem = {
  topic: string;
  discussion: string;
  outcome: string;
};

export type MeetingResult = {
  title: string;
  language?: string;
  participants: string[];
  date_hint: string | null;
  summary: string;
  key_points: string[];
  decisions: string[];
  action_items: ActionItem[];
  mom: {
    agenda: AgendaItem[];
    next_meeting: string | null;
  };
};

export type Speaker = {
  id: string;
  name: string;
};

export type TranscriptSegment = {
  start: number;
  end: number;
  speaker: string;
  text: string;
};

export type Meeting = {
  id: string;
  title: string;
  filename: string;
  status: MeetingStatus;
  status_message: string;
  created_at: string;
  duration_seconds: number | null;
  language: string | null;
  transcript: string | null;
  speakers?: Speaker[] | null;
  segments?: TranscriptSegment[] | null;
  diarization_note?: string | null;
  result: MeetingResult | null;
  error: string | null;
  progress?: number;
  queue_ahead?: number;
};

export type OpenAIStatus = {
  configured: boolean;
  connected: boolean;
  hint: string | null;
  auth_rejected?: boolean;
  denied_models?: string[];
  message: string;
  checked_at: string | null;
  provider?: string;
  provider_label?: string;
  base_url?: string | null;
  chat_model?: string | null;
  asr_model?: string | null;
};

export type QueueStatus = {
  active: number;
  waiting: number;
  limit: number;
};

export type SmtpStatus = {
  configured: boolean;
  host?: string | null;
  port?: number;
  user?: string | null;
  from_addr?: string | null;
  starttls?: boolean;
  has_password?: boolean;
  public_url?: string | null;
};

export type LdapStatus = {
  enabled: boolean;
  configured: boolean;
  url?: string | null;
  bind_dn?: string | null;
  has_password?: boolean;
  base_dn?: string | null;
  user_filter?: string;
  starttls?: boolean;
  tls_verify?: boolean;
  email_attr?: string;
};

export type Health = {
  ok: boolean;
  ffmpeg: boolean;
  ui?: boolean;
  theme?: "classic" | "t2";
  openai: OpenAIStatus;
  queue?: QueueStatus;
  smtp?: SmtpStatus;
  ldap?: LdapStatus;
};

export type AuthStatus = {
  configured: boolean;
  authenticated: boolean;
  username?: string | null;
  setup_token_required?: boolean;
};

export type UserSession = {
  authenticated: boolean;
  id?: string | null;
  email?: string | null;
  archive_limit?: number | null;
  meeting_count?: number | null;
};

export type PlatformUser = {
  id: string;
  email: string;
  archive_limit: number;
  auth_mode?: "local" | "ldap";
  created_at: string;
  meeting_count?: number;
};
