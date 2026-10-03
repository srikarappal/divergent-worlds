import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const experimentdUrl = process.env.RLWORLD_API_URL ?? "http://localhost:8080";

// The console calls /api/...; experimentd serves the same routes at its root.
function stripApiPrefix(path: string): string {
  return path.replace(/^\/api/, "");
}

export default defineConfig({
  plugins: [react()],
  server: {
    fs: { allow: [".."] },
    proxy: { "/api": { target: experimentdUrl, rewrite: stripApiPrefix } },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
  },
});
