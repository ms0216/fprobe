// 画面の設定をブラウザに覚える（使えない環境では既定値のまま動く）
export function load(key: string, fallback: string): string {
  try {
    return localStorage.getItem(`fprobe.${key}`) ?? fallback;
  } catch {
    return fallback;
  }
}

export function save(key: string, value: string): void {
  try {
    localStorage.setItem(`fprobe.${key}`, value);
  } catch {
    /* 覚えられなくても動く */
  }
}
