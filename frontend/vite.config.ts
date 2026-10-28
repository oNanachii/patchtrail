import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// FastAPI serves web/index.html at "/" and mounts web/ at "/assets", so the build puts
// hashed files directly in web/ and references them as /assets/<file>.
// `npm run dev` proxies /api to a loopback backend (real API or scripts/design_harness.py).
const api = process.env.PATCHTRAIL_API ?? "http://127.0.0.1:8874";

export default defineConfig(({ command }) => ({
  base: command === "build" ? "/assets/" : "/",
  plugins: [react()],
  build: {
    outDir: "../web",
    emptyOutDir: true,
    assetsDir: "",
    // The API's CSP has no data: font source, so never inline assets.
    assetsInlineLimit: 0,
    target: "es2022",
    sourcemap: false,
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: api, changeOrigin: false } },
  },
}));
