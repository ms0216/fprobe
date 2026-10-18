import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// 開発中（npm run dev）は /api をデーモン（既定 7820）へ流し、画面と同じ生成元に見せる（デーモンの Origin の検査を通る）
const daemon = process.env.FPROBE_DAEMON ?? "ws://127.0.0.1:7820";

export default defineConfig({
  plugins: [svelte()],
  server: { proxy: { "/api": { target: daemon, ws: true } }, fs: { allow: [".", "../api"] } },
  test: { include: ["tests/**/*.test.ts"], globalSetup: ["tests/global-setup.ts"] },
});
