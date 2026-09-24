import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SPA calls relative /api/* and Vite proxies to the FastAPI server in dev.
// That mirrors the production shape (one origin, a reverse proxy in front of
// both) and sidesteps CORS entirely -- the app never hardcodes a backend URL.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
