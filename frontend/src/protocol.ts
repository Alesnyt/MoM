import type { MeetingResult } from "./types";

const object = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === "string";
const optionalText = (v: unknown) => v === null || text(v);
const texts = (v: unknown) => Array.isArray(v) && v.every(text);

/** Protect rendering and export controls against legacy malformed results. */
export function isProtocolResult(v: unknown): v is MeetingResult {
  return object(v) && text(v.title) && text(v.summary) && optionalText(v.date_hint)
    && texts(v.participants) && texts(v.key_points) && texts(v.decisions)
    && Array.isArray(v.action_items) && v.action_items.every(item => object(item)
      && text(item.task) && optionalText(item.assignee) && optionalText(item.due)
      && ["high", "medium", "low"].includes(String(item.priority)))
    && object(v.mom) && optionalText(v.mom.next_meeting) && Array.isArray(v.mom.agenda)
    && v.mom.agenda.every(item => object(item) && text(item.topic) && text(item.discussion) && text(item.outcome));
}
