export type CalendarPerson = { name: string; email: string; kind?: "required" | "optional" | "resource"; response?: string };
export type CalendarEvent = {
  id: string; title: string; start: string; end: string; location: string; all_day: boolean;
  cancelled: boolean; private: boolean; agenda?: string; organizer?: CalendarPerson | null;
  invitees?: CalendarPerson[]; resources?: CalendarPerson[]; uid?: string;
};
export type CalendarContext = Omit<CalendarEvent, "id"> & { source: "exchange"; selected_at: string };
export type CalendarStatus = { enabled: boolean; connected: boolean; username?: string; expires_at?: string };
export type ExchangeSettings = { enabled: boolean; url: string; auth_mode: "ntlm" | "basic"; revision?: string };
