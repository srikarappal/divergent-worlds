import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const experimentdUrl = process.env.RLWORLD_API_URL ?? "http://localhost:8080";
const fleetdUrl = process.env.RLWORLD_FLEET_URL ?? "http://localhost:8700";

// The console calls /api/...; experimentd serves the same routes at its root.
function stripApiPrefix(path: string): string {
  return path.replace(/^\/api/, "");
}

// Takeover screens connect to /fleet/v1/takeover/...; fleetd relays them at its root.
function stripFleetPrefix(path: string): string {
  return path.replace(/^\/fleet/, "");
}

export default defineConfig({
  plugins: [react()],
  server: {
    fs: { allow: [".."] },
    proxy: {
      "/api": { target: experimentdUrl, rewrite: stripApiPrefix },
      "/fleet": { target: fleetdUrl, ws: true, rewrite: stripFleetPrefix },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
  },
});
