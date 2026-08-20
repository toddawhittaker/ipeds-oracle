import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, proxy /api to the FastAPI backend on :8000.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
    },
    // The documentation screenshot run serves the app under this name so the
    // /keys shot shows a deployment-shaped MCP endpoint instead of the dev
    // port (see frontend/playwright.docs.config.js). Vite blocks unknown Host
    // headers by default. Dev-server only — it has no effect on the build or on
    // anything the container serves.
    allowedHosts: ["ipeds.example.edu"],
  },
  build: { outDir: "dist" },
});
