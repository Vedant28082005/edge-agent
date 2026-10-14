/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Production: same-origin behind `tailscale serve`, so the app uses relative URLs only.
// Dev: proxy the gateway paths (incl. WebSocket) to the local gateway.
const target = "http://127.0.0.1:8000";
const paths = ["/auth", "/devices", "/scenes", "/confirmations", "/chat", "/voice", "/images", "/memory", "/system", "/admin"];

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      ...Object.fromEntries(paths.map((p) => [p, { target, changeOrigin: false }])),
      "/ws": { target, ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: false },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
