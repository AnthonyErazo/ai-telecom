import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/ui/",
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/salud": "http://127.0.0.1:8000",
      "/dev": "http://127.0.0.1:8000",
      "/v1": "http://127.0.0.1:8000"
    }
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts"
  }
});
