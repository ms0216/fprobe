// Linux パネルの子が使う、親の run（欄から組んだ引数で呼ぶ。欄の誤りはその欄の名前で知らせ、remember があれば欄の値を覚え、
// 呼んだ後に読み直す）の型
export type LinuxRun = (method: string, built: { params: Record<string, unknown> } | { error: string },
                        remember?: [string, unknown]) => Promise<any>;
