function escapeHtml(value: string): string {
  return value.replace(/[&<>"]/g, (char) => {
    if (char === "&") return "&amp;";
    if (char === "<") return "&lt;";
    if (char === ">") return "&gt;";
    return "&quot;";
  });
}

function insideField(node: EventTarget | Node | null): boolean {
  const element = node instanceof Element ? node : node instanceof Node ? node.parentElement : null;
  return Boolean(element?.closest("input, textarea, select, [contenteditable='true']"));
}

export function plainCopy(event: ClipboardEvent): void {
  if (insideField(event.target)) return;
  const selection = document.getSelection();
  if (!selection || selection.isCollapsed || insideField(selection.anchorNode)) return;
  const text = selection.toString();
  if (!text.trim() || !event.clipboardData) return;
  event.clipboardData.setData("text/plain", text);
  event.clipboardData.setData(
    "text/html",
    `<meta charset="utf-8"><pre style="color:#1c1710;background:transparent;white-space:pre-wrap">${escapeHtml(text)}</pre>`,
  );
  event.preventDefault();
}

export function installPlainCopy(): void {
  document.addEventListener("copy", plainCopy);
}
