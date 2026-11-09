import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { version } from "./package.json";

function gitRevision(): string {
  try {
    return execFileSync("git", ["rev-parse", "--verify", "HEAD"], {
      cwd: fileURLToPath(new URL(".", import.meta.url)),
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    return "";
  }
}

const revision = process.env.MOM_REVISION?.trim() || gitRevision();
const buildVersion = /^[0-9a-f]{7,64}$/i.test(revision) ? `v${version} · ${revision.slice(0, 7)}` : `v${version}`;

export default defineConfig({
  plugins: [react(), {
    name: "mom-build-version",
    transformIndexHtml: (html) => html.replace("__MOM_BUILD_VERSION__", buildVersion),
  }],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
  build: {
    outDir: "../backend/static",
    emptyOutDir: true,
  },
});
