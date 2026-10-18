<script lang="ts">
  // 時刻の欄: 記録（または捕捉）の始めからの秒（lib/time.ts。全ビューで同じ 0・同じ桁）を出し、クリックで時間軸の
  // その時刻へ（Timeline を前に出し、選択カーソルを置く。区間なら区間が見えるように）。
  // - to: 飛ぶ先（無ければただの文字）。at: 出す時刻（省略で to）。fallback: 0 の位置が分からないときの 0 の位置
  //   （記録の外で取った結果等）。分からなければ元の単位の値（t = …）か "—"。origin: いつもこの 0 で測る（開いている
  //   記録とは別のデータ。lib/time.ts の originFor）。
  // - children があればそれを出し（Activity の時計等）、始めからの秒はツールチップに。
  // 行のダブルクリックで飛ぶビューも、時刻の欄はこれで示す
  import type { Snippet } from "svelte";
  import { shared } from "../../lib/context";
  import { i18n } from "../../lib/i18n";
  import { TABLE_DIGITS, formatSeconds, sinceStart, type Origin, type TimeRef } from "../../lib/time";
  import type { Reveal } from "../../lib/timeline/state";

  let { to, at, fallback, origin, children }:
    { to: Reveal | null | undefined; at?: TimeRef | null; fallback?: Origin; origin?: Origin; children?: Snippet } = $props();
  const { reveal, tl } = shared();
  const origins = tl.origins;

  const text = $derived.by(() => {
    const r = at ?? to;
    const o = r ? (origin ?? $origins[r.source] ?? fallback ?? null) : null;
    const s = r && o ? sinceStart({ ...$origins, [r.source]: o }, r) : null;
    if (s !== null) return formatSeconds(s, o!.tickS, TABLE_DIGITS);
    return r?.t !== undefined ? `t = ${r.t}` : "—";
  });
  const title = $derived(children ? `${text} · ${$i18n("timeline.revealTip")}` : $i18n("timeline.revealTip"));
</script>

{#if to}
  <button class="link time mono" {title}
          onclick={(e) => { e.stopPropagation(); reveal(to); }}
          ondblclick={(e) => e.stopPropagation()}>{#if children}{@render children()}{:else}{text}{/if}</button>
{:else}
  <span class="time mono" title={children ? text : undefined}>{#if children}{@render children()}{:else}{text}{/if}</span>
{/if}

<style>
  .time { white-space: nowrap; font-variant-numeric: tabular-nums; }
  button.time:hover { text-decoration: underline; }
</style>
