// 結合試験の準備: 偽の機器のデーモン（host/tests/fake_daemon.py）を起動し、ポートを試験へ渡す
import { spawn, type ChildProcess } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import type { TestProject } from "vitest/node";

function python(): string {
  if (process.env.FPROBE_PYTHON) return process.env.FPROBE_PYTHON;
  const win = resolve(import.meta.dirname, "../../../.venv/Scripts/python.exe");
  return existsSync(win) ? win : resolve(import.meta.dirname, "../../../.venv/bin/python");
}

let child: ChildProcess | undefined;

export async function setup(project: TestProject) {
  child = spawn(python(), ["-X", "utf8", resolve(import.meta.dirname, "../../tests/fake_daemon.py")],
                { stdio: ["ignore", "pipe", "inherit"] });
  let timer: ReturnType<typeof setTimeout> | undefined;
  const port = await new Promise<number>((ok, ng) => {
    let text = "";
    child!.stdout!.on("data", (b: Buffer) => {
      text += String(b);
      const m = /PORT (\d+)/.exec(text);
      if (m) ok(Number(m[1]));
    });
    child!.on("exit", (code: number | null) => ng(new Error(`fake_daemon が終わった（${code}）`)));
    timer = setTimeout(() => ng(new Error("fake_daemon が起動しない")), 20000);
  }).finally(() => clearTimeout(timer));               // 待ちの時計を残すと、試験の終わりが 10 秒遅れる
  project.provide("daemonPort", port);
}

export async function teardown() {
  child?.kill();
}

declare module "vitest" {
  export interface ProvidedContext {
    daemonPort: number;
  }
}
