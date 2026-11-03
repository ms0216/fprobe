// 名前が placeholder だけの入力欄にも、名前を出し続ける（値が入ると placeholder は消えて、何の欄か分からなくなる）。
// 欄ごとに見えるラベルを持つ作り（LinuxHaltPanel の .fld）が本来の形で、これは持たない欄への保険: 名前（placeholder か
// aria-label）をツールチップ（title）にする。見せ方だけで、値・判断は持たない。Svelte に依らない

/** root の下の、名前を持たない入力欄に title を付ける。付けた数を返す */
export function nameFields(root: ParentNode): number {
  let n = 0;
  for (const e of root.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("input[placeholder], textarea[placeholder]")) {
    if (e.getAttribute("title") || e.closest("label[title]")) continue;
    const name = (e.getAttribute("aria-label") || e.getAttribute("placeholder") || "").trim();
    if (!name) continue;
    e.setAttribute("title", name);
    n += 1;
  }
  return n;
}

/** 画面全体に付ける（パネルが開く・描き直されるたびに、新しい欄にも付ける）。外すには戻り値を呼ぶ */
export function watchFieldNames(root: HTMLElement): () => void {
  nameFields(root);
  let queued = false;
  const obs = new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; nameFields(root); });
  });
  obs.observe(root, { childList: true, subtree: true });
  return () => obs.disconnect();
}

/** API のパラメータ名を、見える名前にする（timeout_s → timeout (s)・tree_depth → tree depth）。見せ方だけ。元の名前は title に残す */
export function readable(name: string): string {
  return name.replace(/_s$/, " (s)").replace(/_ns$/, " (ns)").replace(/_hz$/, " (Hz)").replace(/_/g, " ");
}
