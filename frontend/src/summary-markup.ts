import MarkdownIt from "markdown-it";

// Keep this vocabulary in sync with backend/summary_format.py. Everything else is text.
export const summaryMarkdown = new MarkdownIt("zero", { html: false, breaks: true })
  .enable(["list", "heading", "emphasis", "newline", "escape"]);
