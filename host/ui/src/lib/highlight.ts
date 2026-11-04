// ソースの構文の色（C / C++ とアセンブラのソースだけ。キーワード・型・文字列・数・コメント・前処理）と、逆アセンブルの
// 1 命令の色（ニーモニック・レジスタ・即値・記号・コメント）。画面の見せ方だけで、意味の判断（どれが関数か・変数か・
// 命令が何をするか）はしない。依存を増やさない小さな字句解析。Svelte に依らない

export type TokenKind = "kw" | "type" | "str" | "num" | "cm" | "pp" | "pl" | "reg" | "sym";
export interface Token { t: TokenKind; s: string }

const KEYWORDS = new Set([
  "auto", "break", "case", "const", "continue", "default", "do", "else", "enum", "extern", "for", "goto", "if", "inline",
  "register", "restrict", "return", "sizeof", "static", "struct", "switch", "typedef", "union", "volatile", "while",
  "class", "namespace", "template", "typename", "public", "private", "protected", "virtual", "new", "delete", "this",
  "try", "catch", "throw", "using", "operator", "constexpr", "nullptr", "true", "false", "asm", "__asm__", "__attribute__",
  "noinline", "_Bool", "static_assert", "alignas", "alignof", "and", "or", "not",
]);
const TYPES = new Set(["void", "char", "short", "int", "long", "float", "double", "signed", "unsigned", "bool", "size_t",
  "ssize_t", "ptrdiff_t", "intptr_t", "uintptr_t", "wchar_t", "FILE"]);
const FIXED_WIDTH = /^u?int(8|16|32|64)_t$/;
const NUMBER = /^(0[xX][0-9a-fA-F]+|0[bB][01]+|\d+(\.\d*)?([eE][+-]?\d+)?)[uUlLfF]*/;
const IDENT = /^[A-Za-z_][A-Za-z0-9_]*/;

/** 行の並びを、トークンの並びの並びにする。/* ... *​/ の複数行は、行をまたいで続ける */
export function highlightLines(lines: string[]): Token[][] {
  let inBlock = false;
  return lines.map((line) => {
    const out: Token[] = [];
    const push = (t: TokenKind, s: string) => {
      if (!s) return;
      const last = out[out.length - 1];
      if (last && last.t === t) last.s += s;
      else out.push({ t, s });
    };
    let i = 0;
    if (!inBlock && /^\s*#/.test(line) && !/^\s*#\s*$/.test(line)) {           // 前処理の行（#include・#define 等）
      const m = /\/\/|\/\*/.exec(line);
      const cut = m ? m.index : line.length;
      push("pp", line.slice(0, cut));
      i = cut;
    }
    while (i < line.length) {
      const rest = line.slice(i);
      if (inBlock) {
        const end = rest.indexOf("*/");
        if (end < 0) { push("cm", rest); i = line.length; }
        else { push("cm", rest.slice(0, end + 2)); i += end + 2; inBlock = false; }
      } else if (rest.startsWith("/*")) {
        inBlock = true;
        push("cm", "/*");
        i += 2;
      } else if (rest.startsWith("//")) {
        push("cm", rest);
        i = line.length;
      } else if (rest[0] === '"' || rest[0] === "'") {
        const q = rest[0];
        let j = 1;
        while (j < rest.length && rest[j] !== q) j += rest[j] === "\\" ? 2 : 1;
        push("str", rest.slice(0, Math.min(j + 1, rest.length)));
        i += Math.min(j + 1, rest.length);
      } else {
        const id = IDENT.exec(rest);
        const num = !id ? NUMBER.exec(rest) : null;
        if (id) {
          const w = id[0];
          push(KEYWORDS.has(w) ? "kw" : TYPES.has(w) || FIXED_WIDTH.test(w) ? "type" : "pl", w);
          i += w.length;
        } else if (num) {
          push("num", num[0]);
          i += num[0].length;
        } else {
          push("pl", rest[0]);
          i += 1;
        }
      }
    }
    return out;
  });
}

// 逆アセンブル（debug.disassemble の mnemonic・operands）の色。レジスタの名前は ARM（r0〜r15・sp・lr・pc・VFP の s/d/q・
// 特殊レジスタ）と RISC-V（x0〜x31・ABI の名前）の綴りだけを見る（綴りの見分けで、命令の意味は読まない）
const REGISTER = new RegExp("^(r([0-9]|1[0-5])|sp|lr|pc|ip|fp|sb|sl|apsr|cpsr|spsr|fpscr|xpsr|ipsr|epsr|primask|basepri"
  + "|basepri_max|faultmask|control|msp|psp|[sdq]([0-9]|[12][0-9]|3[01])|x([0-9]|[12][0-9]|3[01])|zero|ra|gp|tp|t[0-6]"
  + "|a[0-7]|s1[01])$", "i");
const SHIFTS = new Set(["lsl", "lsr", "asr", "ror", "rrx", "uxtw", "sxtw", "uxtb", "sxtb", "uxth", "sxth"]);
const ASM_NUMBER = /^#?-?(0[xX][0-9a-fA-F]+|\d+)/;

/** 1 命令を、トークンの並びにする（ニーモニック・空白・オペランド。つなぐと `${mnemonic} ${operands}` に戻る） */
export function highlightAsm(mnemonic: string, operands: string): Token[] {
  const out: Token[] = [];
  const push = (t: TokenKind, s: string) => {
    if (!s) return;
    const last = out[out.length - 1];
    if (last && last.t === t) last.s += s;
    else out.push({ t, s });
  };
  push("kw", mnemonic);
  push("pl", " ");
  let i = 0;
  while (i < operands.length) {
    const rest = operands.slice(i);
    if (rest[0] === ";" || rest[0] === "@" || rest.startsWith("//")) {        // 逆アセンブラの添え書き（番地・記号）
      push("cm", rest);
      break;
    }
    if (rest[0] === "<") {                                                     // <記号+オフセット>
      const end = rest.indexOf(">");
      const n = end < 0 ? rest.length : end + 1;
      push("sym", rest.slice(0, n));
      i += n;
      continue;
    }
    const id = IDENT.exec(rest);
    if (id) {
      const w = id[0];
      push(REGISTER.test(w) ? "reg" : SHIFTS.has(w.toLowerCase()) ? "kw" : "sym", w);
      i += w.length;
      continue;
    }
    const num = ASM_NUMBER.exec(rest);
    if (num) {
      push("num", num[0]);
      i += num[0].length;
      continue;
    }
    push("pl", rest[0]);
    i += 1;
  }
  return out;
}
