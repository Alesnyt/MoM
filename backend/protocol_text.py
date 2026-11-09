from __future__ import annotations

import re
import unicodedata


def _key(text: str) -> str:
    text = unicodedata.normalize("NFC", text).strip()
    text = re.sub(r"^(?:[•*–—-]\s+|\d+[.)]\s+)", "", text)
    return " ".join(text.rstrip(".!").casefold().split())


def _unique_lines(items: list, seen: set[str]) -> list:
    lines = []
    for item in items:
        if not isinstance(item, str):
            lines.append(item)  # Legacy malformed fields still go through validation.
            continue
        key = _key(item)
        if key and key not in seen:
            seen.add(key)
            lines.append(item.strip())
    return lines


def deduplicate(result: dict) -> dict:
    """Keep the authoritative item once, without merging similar but distinct facts."""
    result = dict(result)
    action_keys: set[tuple[str, str, str]] = set()
    actions = result.get("action_items")
    if isinstance(actions, list):
        unique = []
        for item in actions:
            if not isinstance(item, dict) or not isinstance(item.get("task"), str):
                unique.append(item)
                continue
            assignee, due = item.get("assignee"), item.get("due")
            if (assignee is not None and not isinstance(assignee, str)) or (due is not None and not isinstance(due, str)):
                unique.append(item)
                continue
            signature = (_key(item["task"]), _key(assignee or ""), _key(due or ""))
            if signature not in action_keys:
                action_keys.add(signature)
                unique.append(item)
        result["action_items"] = unique

    seen: set[str] = set()
    if isinstance(result.get("decisions"), list):
        result["decisions"] = _unique_lines(result["decisions"], seen)
    seen.update(task for task, _, _ in action_keys)
    if isinstance(result.get("key_points"), list):
        result["key_points"] = _unique_lines(result["key_points"], seen)
    return result
