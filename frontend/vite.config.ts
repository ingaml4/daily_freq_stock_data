import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const backend = process.env.EMSCAN_DEV_BACKEND || "http://127.0.0.1:8080";

export default defineConfig({
  base: "/ui/",
  plugins: [vue()],
  server: {
    proxy: {
      "/api": backend,
      "/logout": backend,
      "/summary.md": backend,
      "/tiers.csv": backend,
    },
  },
  build: { target: "es2022" },
});
