export type Appearance = "auto" | "light" | "dark";

const preferenceKey = "mom.appearance";

export function loadAppearance(): Appearance {
  try {
    const saved = window.localStorage.getItem(preferenceKey);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    // The appearance still works for this session if storage is unavailable.
  }
  return "auto";
}

export function saveAppearance(value: Appearance): void {
  try {
    window.localStorage.setItem(preferenceKey, value);
  } catch {
    // State remains available until the page reloads.
  }
}

export function resolveAppearance(value: Appearance, now: Date): "light" | "dark" {
  if (value !== "auto") return value;
  const hour = now.getHours();
  return hour >= 7 && hour < 19 ? "light" : "dark";
}
