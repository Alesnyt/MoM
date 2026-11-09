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

export async function copyForWord(protocol: HTMLElement): Promise<void> {
  const copy = protocol.cloneNode(true) as HTMLElement;
  for (const cell of copy.querySelectorAll<HTMLElement>("th, td")) {
    cell.style.cssText = "border:1px solid #aaa;padding:5pt;vertical-align:top;text-align:left";
  }
  const html = `<meta charset="utf-8"><div style="font-family:Arial,sans-serif;color:#000">${copy.innerHTML}</div>`;
  await navigator.clipboard.write([new ClipboardItem({
    "text/html": new Blob([html], { type: "text/html" }),
    "text/plain": new Blob([protocol.innerText], { type: "text/plain" }),
  })]);
}
