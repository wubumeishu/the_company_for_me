import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5180,
    // 后端引擎 8790；WS 直连 ws://127.0.0.1:8790（本地优先，无网关）
  },
});
