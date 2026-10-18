import { resolve } from "node:path";
import { defineConfig } from "vite";

// 性能の測定のページ（bench/index.html）だけを bench/dist に作る。データ（bench/data）は run.py が配る。
export default defineConfig({
  root: import.meta.dirname,
  base: "./",
  publicDir: false,
  build: { outDir: resolve(import.meta.dirname, "dist"), emptyOutDir: true, target: "es2022" },
});
