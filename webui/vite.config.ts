import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The brain (FastAPI) serves the built app from webui/dist and proxies media itself, so the UI is always
// same-origin in production. In dev we proxy API/WS/media to the brain on :8200.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "./",
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8200",
      "/whep": "http://127.0.0.1:8200",
      "/hls": "http://127.0.0.1:8200",
      "/ws": { target: "ws://127.0.0.1:8200", ws: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
