<script lang="ts">
  import { intervalNow } from "../common/interval";
  import SystemAnalysis from "./SystemAnalysis.svelte";
  import SystemFpga from "./system/SystemFpga.svelte";
  import SystemMcu from "./system/SystemMcu.svelte";
  import SystemRecordBar from "./system/SystemRecordBar.svelte";
  import SystemTriggerBar from "./system/SystemTriggerBar.svelte";
  // システム解析: MCU と FPGA を対等に並べる（どちらかが主でどちらかが従ではない）。MCU = コアの状態と止めずに読む
  // 番地（ファームウェアのメールボックス・カウンタ等。読んだ値と読んだ時刻）、FPGA = 観測の型・状態と各組の最新の値
  // （信号の名前で）。どちらも system.status 1 回で読む。細かい前後関係は時間軸（同じ時刻の上に両方が並ぶ）で見る。
  // FPGA の条件（例: restart changed and state=BUSY）の回数は、使う人が入れた範囲（最後の N 件）でデーモンが数えた
  // 事実（fpga_obs.count: 回数・数えた範囲）をそのまま描く。画面では数えない・毎秒を計算しない・推さない。
  // 構成: ここは状態の読み取り（poll）・記録の開始と停止・表と条件の共有。MCU 側 / FPGA 側 / ツールバー / 見張りの欄は panels/system/
  import { untrack } from "svelte";
  import { errorText, type Result } from "../../lib/api";
  import { shared } from "../../lib/context";
  import { i18n } from "../../lib/i18n";
  import { suggestedOf } from "../../lib/apispec";
  import { load, save } from "../../lib/prefs";
  import {
    addressKey, parseCount, parseRate, readConditionCounts, sanitizeConditions, sanitizeWatches, SharedSystem,
    pinsStreamStart, memoryStopped as isMemoryStopped,
    type Condition, type ConditionCount, type HaltState, type RecordingTrigger, type Watch,
  } from "../../lib/system";

  const POLL_MS = 1000;
  const { app, conn, dock, tl } = shared();

  /** system.status の応答（仕様から生成した型） */
  type Status = Result<"system.status">;
  /** MCU: つないで読めた形（state を持つ）・つないでいない・読めなかった（error）の 3 つ（API の応答の形のまま） */
  type McuRead = Extract<Status["mcu"], { state: unknown }>;
  type FpgaOpen = Extract<Status["fpga"], { available: true }>;

  const linkOpen = $derived($app.link === "open");
  let watches = $state<Watch[]>(sanitizeWatches(JSON.parse(load("system.watches", "[]") || "[]")));
  let status = $state<Status | null>(null);
  const mcu = $derived<McuRead | null>(status && "state" in status.mcu ? status.mcu : null);
  const mcuError = $derived(status && "error" in status.mcu ? status.mcu.error : null);
  const fpga = $derived<FpgaOpen | null>(status?.fpga.available ? status.fpga : null);
  let error = $state<string | null>(null);
  let rec = $state<Result<"recording.status"> | null>(null);
  let withPins = $state(load("system.withPins", "1") === "1");       // ピン（ロジアナ）も流し続けて記録へ
  $effect(() => save("system.withPins", withPins ? "1" : "0"));
  // 形式の最初の値は、前に選んだもの、無ければ仕様の既定（recording.la_stream.start の rle）。欄に見えて書き換えられる
  const suggestedFormat = suggestedOf("recording.la_stream.start", "rle") === true ? "rle" : suggestedOf("recording.la_stream.start", "rle") === false ? "raw" : "";
  let pinsFormat = $state<"" | "rle" | "raw">((["rle", "raw"].includes(load("system.pinsFormat", "")) ? load("system.pinsFormat", "")
                                                                                                    : suggestedFormat) as "" | "rle" | "raw");
  $effect(() => save("system.pinsFormat", pinsFormat));
  // RAW のとき: サンプルレート（MS/s）とチャンネル数も使う人が選ぶ（既定の速さで始めると、16 ch は 50 MB/s の壁を超えて溢れる）
  let pinsRateText = $state(load("system.pinsRateMs", "") || "");
  $effect(() => save("system.pinsRateMs", pinsRateText));
  let pinsChannels = $state(load("system.pinsChannels", "") || "");
  $effect(() => save("system.pinsChannels", pinsChannels));
  const pinsStart = $derived(pinsStreamStart(pinsFormat, pinsRateText, pinsChannels));
  let withMemory = $state(load("system.withMemory", "1") === "1");   // MCU のメモリも DAP サンプラで記録へ（名前つき）
  $effect(() => save("system.withMemory", withMemory ? "1" : "0"));
  // 記録のメモリの読みの速さ（回/秒）: 使う人が入れる（時刻の分解能・リンクの帯域に効くので既定の値で決めない）
  let memoryRateText = $state(load("system.memoryRateHz", "") || String(suggestedOf("watch.set", "rate_hz") ?? ""));   // 最初の値は仕様の既定（見えて書き換えられる）
  $effect(() => save("system.memoryRateHz", memoryRateText));
  const memoryRate = $derived(parseRate(memoryRateText));
  const needsRate = $derived(withMemory && watches.length > 0 && memoryRate === null);
  let startedWatch = false;                  // 記録のために watch.set したか（止めるときに戻す）
  let memoryStopped = $state(false);
  let startedPins = $state(false);     // この画面でピンの記録を始めた（後で止まれば知らせる）
  const pinsStopped = $derived(Boolean(rec?.recording && startedPins &&
                                       (!rec.la_stream || !rec.la_stream.running || rec.la_stream.error || rec.la_stream.overrun)));   // 記録中に MCU のメモリの読みが止まった（別の道具・AI が止めた等）: 黙って欠けさせない
  const obsMatch = $derived(Boolean(($app.probe?.capabilities as Record<string, unknown> | undefined)?.obs_match));
  // orphan: デーモンの再起動の前に仕掛けた条件が FPGA に残っている（条件の文字は分からない。MCU を止め続ける）
  let haltOn = $state<HaltState | null>(null);
  // 条件で印・止める（recording.trigger_set）: めったに起きない不具合を前後つきで記録に残す。状態はここで読む
  let trigger = $state<RecordingTrigger | null>(null);
  let note = $state<string | null>(null);
  let conditions = $state<Condition[]>(sanitizeConditions(JSON.parse(load("system.conditions", "[]") || "[]")));
  let condCounts = $state<Record<string, ConditionCount>>({});
  // 条件を数える範囲（最後の N 件）: 使う人が入れる（空なら数えない。暗黙の窓を持たない）
  let condLastText = $state(load("system.condLast", "") || "");
  $effect(() => save("system.condLast", condLastText));
  const condLast = $derived(parseCount(condLastText));

  // 表と条件: ブラウザに覚え（古いデーモン・つながる前）、デーモンの画面の状態で別の窓・別の PC・AI と分け合う
  let sharedSystem: SharedSystem | null = null;
  let sharedReady = false;                      // load の前に手元の表を上げて、デーモンの表を上書きしない
  $effect(() => {
    save("system.watches", JSON.stringify(watches));
    save("system.conditions", JSON.stringify(conditions));
    if (sharedReady) void sharedSystem?.save($state.snapshot(watches), $state.snapshot(conditions));
  });
  $effect(() => {
    if (!linkOpen) return;
    const s = new SharedSystem(conn, (w, c) => {
      watches = w;
      conditions = c;
    });
    sharedSystem = s;
    // 手元の表は読むだけ（依存にしない: 表が届くたびに作り直して受け取り直す繰り返しにならないように）
    void untrack(() => s.load($state.snapshot(watches), $state.snapshot(conditions))).then(() => {
      if (sharedSystem === s) sharedReady = true;     // つなぎ直しの途中で終わった古い load は、新しい方を準備済みにしない
    });
    return () => {
      sharedReady = false;
      sharedSystem = null;
      s.dispose();
    };
  });

  $effect(() => {
    if (!linkOpen) return;
    return intervalNow(() => poll(), POLL_MS);
  });

  let polling = false;
  async function poll() {
    if (polling) return;                         // 前の問い合わせが 1 秒を超えたら重ねない
    polling = true;
    try {
      const s: Status = await conn.call("system.status", { addresses: watches.map((w) => w.address) }, { quiet: true });
      status = s;
      rec = await conn.call("recording.status", {}, { quiet: true });
      if (obsMatch) haltOn = await conn.call("fpga_obs.trigger_status", {}, { quiet: true }).catch(() => haltOn);
      memoryStopped = isMemoryStopped(rec, startedWatch);   // recording.status の watch_running（デーモンの事実）
      if (!rec?.recording) startedPins = false;
      if (!rec?.recording && startedWatch) {          // 記録が条件・AI・別の窓で止まった: 記録のための読みも止める
        startedWatch = false;
        await conn.call("watch.stop", {}, { quiet: true }).catch(() => null);
      }
      // 見張りの様子: 記録中と、見張っていた記録が止まった後（条件で止めたことを知らせる）
      if (rec?.recording || trigger) trigger = await conn.call("recording.trigger_status", {}, { quiet: true }).catch(() => null);
      if (trigger?.state === "off") trigger = null;
      error = null;
      condCounts = s.fpga.available && s.fpga.driving ? (await readConditionCounts(conn, conditions.map((c) => c.expr), condLast)) ?? {} : {};
    } catch (e) {
      error = errorText(e);
    } finally {
      polling = false;
    }
  }

  // 記録: MCU（DAP・メモリの標本・UART）・ピン（ロジアナ）・FPGA の観測が 1 本の時間軸に入る
  async function startRecording() {
    note = null;
    if (await app.act("recording.start", { name: "system" }) === undefined) return;
    if (withMemory && watches.length) await watchMemory();
    // ピンの形式（RLE = 変化点だけ / RAW）は使う人が選んだもの（画面で決めない）
    startedPins = withPins && pinsStart !== null && await app.act("recording.la_stream.start", pinsStart) !== undefined;
    await poll();
  }
  // MCU のメモリを DAP サンプラで記録へ（時刻つきの値 = 時間軸の段。名前は表の名前）。サンプラは 1 つなので、ほかの
  // 道具・利用者が使っていれば取り上げずに、使っている道具と始めた者（デーモンの事実）を知らせる（自分のものかを推さない。
  // 止めるのは利用者の操作）
  async function watchMemory() {
    const holder = await conn.call("probe.sampler.status", {}, { quiet: true }).catch(() => null);
    if (holder?.user) {
      note = holder.client?.name ? $i18n("system.samplerBusyBy", { user: holder.user, who: holder.client.name })
                                 : $i18n("system.samplerBusy", { user: holder.user });
      return;
    }
    if (memoryRate === null) return;               // 始める前に欄で確かめている（needsRate）
    startedWatch = await app.act("watch.set", { expressions: watches.map((w) => addressKey(w.address)),
                                                names: watches.map((w) => w.name),
                                                rate_hz: memoryRate }) !== undefined;
    if (!startedWatch) note = $i18n("system.memoryNotRecorded");   // ピン・FPGA は記録している（記録は続ける）
  }
  async function stopRecording() {
    if (rec?.la_stream) await app.act("recording.la_stream.stop", {});
    if (startedWatch) await app.act("watch.stop", {});
    startedWatch = false;
    startedPins = false;
    await app.act("recording.stop", {});
    await poll();
  }
  // MCU の SWD がはずれた（対象のリセット・書き直しで複数ドロップの選択が外れた等）: 前と同じ設定でつなぎ直す
  async function reconnectMcu() {
    const last = status?.mcu.connect;
    if (!last) return;
    const { target, frequency_hz, ...rest } = last;                 // 前の接続の設定（target・frequency_hz は必ずある）
    const args = { target, frequency_hz, ...Object.fromEntries(Object.entries(rest).filter(([, v]) => v !== null)) };
    await app.act("target.disconnect", {});
    await app.act("target.connect", args);
    await poll();
  }
  // 観測の回路が答えなくなった（対象の FPGA を書き直した・電源を入れ直した等）: 前と同じ設定で開き直す
  async function reopenFpga() {
    if (fpga?.config) await app.act("fpga_obs.open", { ...fpga.config, auto_reopen: fpga.auto_reopen });
    await poll();
  }
  async function showTimeline() {
    let flat: number[] = [];
    if (rec?.name) {
      tl.setSource("recording");
      // 記録の中で変化の無いピン（API の unchanged_channels）は隠す（16 段が MCU・FPGA の段を画面の外へ押し出さないように）。
      // 隠した数は時間軸に出し、ワンクリックで戻せる（tl.autoHidden）
      const ov = await conn.call("recording.overview", { width: 400, streams: [] }, { quiet: true }).catch(() => null);
      flat = Array.isArray(ov?.unchanged_channels) ? ov.unchanged_channels : [];
    }
    tl.arrangeForSystem(flat);
    dock.show("timeline");
  }
</script>

<div class="pane system">
  <SystemRecordBar {rec} {linkOpen} {needsRate} pinsStartMissing={pinsStart === null} bind:withMemory bind:memoryRateText bind:withPins
                   bind:pinsFormat bind:pinsRateText bind:pinsChannels onStart={startRecording} onStop={stopRecording}
                   onTimeline={showTimeline} />
  {#if error}<div class="note bad">{error}</div>{/if}
  {#if rec?.recording}<SystemTriggerBar bind:trigger />{/if}
  {#if trigger?.state === "error"}<div class="note bad">{trigger.error}</div>{/if}
  {#if trigger?.state === "stopped"}<div class="note">{$i18n("system.triggerStopped", { n: trigger.hits ?? 0 })}</div>{/if}
  {#if note}<div class="note">{note}</div>{/if}
  {#if memoryStopped}<div class="note bad">{$i18n("system.memoryStopped")}</div>{/if}
  {#if pinsStopped}<div class="note bad" title={rec?.la_stream?.error ?? ""}>{$i18n("system.pinsStopped", { why: rec?.la_stream?.error ? ` (${rec.la_stream.error})` : rec?.la_stream?.overrun ? " (overrun)" : "" })}</div>{/if}
  {#if rec?.recording && rec.events_errors}
    <div class="note" title={rec.events_errors.last ?? ""}>{$i18n("system.eventsErrors", { n: rec.events_errors.count, lost: rec.events_errors.lost.toLocaleString() })}</div>
  {/if}

  <div class="peers">
    <SystemMcu {status} {mcu} {mcuError} {error} {linkOpen} bind:watches {poll} onReconnect={reconnectMcu} />
    <SystemFpga {status} {fpga} {rec} {obsMatch} bind:conditions {condCounts} bind:condLastText bind:haltOn onReopen={reopenFpga} />
  </div>

  <!-- 結果の直前に多い出来事: 記録の中で、結果（MCU のメモリの変化等）の直前にどの条件が来ているか -->
  <SystemAnalysis recName={rec?.name} {linkOpen} bind:conditions />
</div>

<style>
  /* パネルの幅で並べ方を変える（ドックの中では窓の幅でなくパネルの幅が効く）: 広ければ左右、狭ければ上下 */
  .system { container-type: inline-size; }
  .peers { display: grid; grid-template-columns: 1fr; gap: 8px; padding: 8px; }
  @container (min-width: 900px) { .peers { grid-template-columns: 1fr 1fr; } }
  .note { padding: 3px 8px; font-size: 12px; }
  .bad { color: var(--bad); }
</style>
