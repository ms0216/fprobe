<script lang="ts">
  // 説明の文を、標準では出さずに「ⓘ」にする（マウスを乗せる・押す・キーで開く）。標準で画面を占める文章を減らす。
  // title は乗せたとき、押すと下に出る（タッチの端末でも読める）。内容は呼ぶ側の文のまま（見せ方だけ）
  let { text }: { text: string } = $props();
  let open = $state(false);
</script>

<span class="hint-wrap">
  <button type="button" class="hint" title={text} aria-label={text} aria-expanded={open}
          onclick={() => (open = !open)} onkeydown={(e) => e.key === "Escape" && (open = false)}>ⓘ</button>
  {#if open}<span class="pop" role="note">{text}</span>{/if}
</span>

<style>
  .hint-wrap { position: relative; display: inline-block; }
  .hint { border: none; background: none; padding: 0 3px; cursor: help; opacity: 0.6; font-size: 13px; line-height: 1; color: inherit; }
  .hint:hover, .hint:focus-visible, .hint[aria-expanded="true"] { opacity: 1; }
  .pop { position: absolute; z-index: 40; left: 0; top: 100%; width: max-content; max-width: 360px; padding: 6px 8px;
         background: var(--panel); border: 1px solid var(--line); border-radius: 6px; font-size: 12px; font-weight: normal;
         white-space: normal; box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18); }
</style>
