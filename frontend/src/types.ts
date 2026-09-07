export type MeetingStatus =
  | "queued"
  | "extracting"
  | "transcribing"
  | "analyzing"
  | "done"
  | "error";

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
  result: MeetingResult | null;
  error: string | null;
  progress?: number;
};

export type OpenAIStatus = {
  configured: boolean;
  connected: boolean;
  hint: string | null;
  message: string;
  checked_at: string | null;
  provider?: string;
  provider_label?: string;
  base_url?: string | null;
  chat_model?: string | null;
  asr_model?: string | null;
};

export type Health = {
  ok: boolean;
  ffmpeg: boolean;
  ui?: boolean;
  theme?: "classic" | "t2";
  openai: OpenAIStatus;
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
  created_at: string;
  meeting_count?: number;
};
