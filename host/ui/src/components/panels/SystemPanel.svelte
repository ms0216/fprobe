<script lang="ts">
  // システム解析: MCU と FPGA を対等に並べる（どちらかが主でどちらかが従ではない）。MCU = コアの状態と止めずに読む
  // 番地（ファームウェアのメールボックス・カウンタ等。毎秒の増え方も）、FPGA = 観測の型・状態と各組の最新の値（信号の
  // 名前で）。どちらも system.status 1 回で読む。細かい前後関係は時間軸（同じ時刻の上に両方が並ぶ）で見る。
  // FPGA の条件（例: restart changed and state=BUSY）の毎秒の回数を、MCU のカウンタの増え方と横に並べる（速さが
  // そろえば関係を調べる手がかり。どの対象にも使える汎用の見方）。
  import { untrack } from "svelte";
  import { errorText } from "../../lib/api";
  import { shared } from "../../lib/context";
  import { i18n, type Key } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";
  import { haltReasonKey } from "../../lib/target";
  import {
    addressKey, countCondition, memoryRateHz, exampleView, fieldsText, flatChannels, parseAddress, parseCondition, rates,
    fpgaOnly, isSymbolName, ownWatch, sanitizeConditions, symbolWatch, sanitizeWatches, SharedSystem, termName, type Condition,
    type MemoryRead,
    type ObsItem, type Watch,
  } from "../../lib/system";

  const POLL_MS = 1000;
  const { app, conn, dock, tl } = shared();

  interface Latest { group: number; value: number; time_us: number | null; fields?: Record<string, number | string> }
  interface Status {
    time_us: number;
    mcu: { connected: boolean; state?: string; target?: string; core?: number; error?: string; memory?: Record<string, number>;
           connect?: Record<string, unknown> | null };
    fpga: { available: boolean; mode?: string; running?: boolean; stuck?: boolean; driving?: boolean; overflow_seen?: boolean;
            scans?: number; id?: number; latest?: Latest[]; gaps?: number; latest_error?: string;
            config?: Record<string, unknown> | null; auto_reopen?: boolean; reopened?: number };
  }

  const linkOpen = $derived($app.link === "open");
  let watches = $state<Watch[]>(sanitizeWatches(JSON.parse(load("system.watches", "[]") || "[]")));
  let newName = $state("");
  let newAddress = $state("");
  let addError = $state<string | null>(null);
  let status = $state<Status | null>(null);
  let prev: MemoryRead | null = null;
  let prevScans: { t_us: number; scans: number } | null = null;
  let perSecond = $state<Record<string, number>>({});
  let scansPerSecond = $state<number | null>(null);
  let error = $state<string | null>(null);
  let rec = $state<{ recording: boolean; name: string | null; events?: number; la_stream?: { running?: boolean; error?: string | null; overrun?: boolean };
                     events_errors?: { count: number; last: string | null; lost: number } } | null>(null);
  let withPins = $state(load("system.withPins", "1") === "1");       // ピン（ロジアナ）も流し続けて記録へ
  $effect(() => save("system.withPins", withPins ? "1" : "0"));
  let withMemory = $state(load("system.withMemory", "1") === "1");   // MCU のメモリも DAP サンプラで記録へ（名前つき）
  $effect(() => save("system.withMemory", withMemory ? "1" : "0"));
  let startedWatch = false;
  let memoryStopped = $state(false);
  let startedPins = $state(false);     // この画面でピンの記録を始めた（後で止まれば知らせる）
  const pinsStopped = $derived(Boolean(rec?.recording && startedPins &&
                                       (!rec.la_stream || !rec.la_stream.running || rec.la_stream.error || rec.la_stream.overrun)));   // 記録中に MCU のメモリの読みが止まった（別の道具・AI が止めた等）: 黙って欠けさせない
  // FPGA の条件でハードのクロストリガ（fpga_obs.trigger_set）: 成立した瞬間に MCU を止める（SWD 4 MHz で約 64 µs）
  const obsMatch = $derived(Boolean(($app.probe?.capabilities as Record<string, unknown> | undefined)?.obs_match));
  // orphan: デーモンの再起動の前に仕掛けた条件が FPGA に残っている（条件の文字は分からない。MCU を止め続ける）
  let haltOn = $state<{ condition: string | null; count?: number; orphan?: boolean; halt?: boolean } | null>(null);
  async function armHalt(expr: string) {
    haltOn = (await app.act("fpga_obs.trigger_set", { condition: expr, halt: true })) ?? haltOn;
  }
  async function clearHalt() {
    await app.act("fpga_obs.trigger_clear", {});
    haltOn = null;
  }
  // 条件が成立した瞬間の前後のピンの波形を捕捉する: 同じ一致の回路をロジアナのトリガへ（停止の代わり。回路は 1 つ）、
  // ロジアナはクロストリガだけで成立させる。内蔵の捕捉が無い構成（PYNQ の深いバッファ版）は深いバッファで
  let capturingOn = $state<string | null>(null);
  async function captureOn(expr: string) {
    const caps = ($app.probe?.capabilities ?? {}) as Record<string, any>;
    capturingOn = expr;
    let armed = false;
    try {
      armed = Boolean(await app.act("fpga_obs.trigger_set", { condition: expr, halt: false, la: true }));
      if (!armed) return;                            // 仕掛けられなかった: 先に仕掛けてあった停止の条件は外さない
      haltOn = null;
      // 記録中は捕捉が記録にも入る: 時間軸は記録のまま（定期の読みを待たずに今の状態を読む。開いた直後に押しても）
      const recording = Boolean((await conn.call("recording.status", {}, { quiet: true }).catch(() => null))?.recording);
      tl.setSource(recording ? "recording" : "capture");
      const depth = Number(caps.la_depth ?? 0);
      // 深いバッファは RAW（前後 4096 拍 = 25 MHz で前後 1.3 ms）: RLE は信号の変化が少ないと埋まるのに十数秒かかる
      const done = await tl.capture({ rate_hz: Number(caps.la_sample_hz ?? 100e6) / 4, pretrigger: Math.floor(depth / 2),
                                      deep: !depth, crossOnly: true, rle: false });
      // 記録中なら捕捉は記録にも入る: 記録の時間軸でトリガの前後 1 ms を見せる（ピンと FPGA の信号・MCU が同じ時間軸に並ぶ）
      // 視点は共有の状態（ui.state）で動かす（結果の直前に多い出来事の「表示」と同じ。手元だけ動かすと共有の視点で戻される）
      if (recording && done?.trigger_time !== undefined) {
        const ms = Number(caps.timebase_hz ?? 100e6) / 1000, t = done.trigger_time;
        await conn.call("ui.state.set", { view: { source: "recording", t0: Math.round(t - ms), t1: Math.round(t + ms) },
                                          cursors: { selection: t } }, { quiet: true });
      }
    } finally {
      if (armed) await app.act("fpga_obs.trigger_clear", {});
      capturingOn = null;
    }
  }
  // 条件で印・止める（recording.trigger_set）: めったに起きない不具合を前後つきで記録に残す
  interface Trigger { state: string; condition?: string; action?: string; hits?: number; last_t?: number | null; error?: string | null }
  let trigger = $state<Trigger | null>(null);
  let triggerExpr = $state(load("system.triggerExpr", "") || "");
  let triggerStop = $state(load("system.triggerStop", "0") === "1");
  let triggerPost = $state(Number(load("system.triggerPost", "1")) || 1);
  $effect(() => {
    save("system.triggerExpr", triggerExpr);
    save("system.triggerStop", triggerStop ? "1" : "0");
    save("system.triggerPost", String(triggerPost));
  });
  async function armTrigger() {
    trigger = (await app.act("recording.trigger_set", { condition: triggerExpr.trim(), action: triggerStop ? "stop" : "mark",
                                                        post_s: triggerPost })) ?? trigger;
  }
  async function clearTrigger() {
    await app.act("recording.trigger_clear", {});
    trigger = null;
  }                     // 記録のために watch.set したか（止めるときに戻す）
  let note = $state<string | null>(null);
  let conditions = $state<Condition[]>(sanitizeConditions(JSON.parse(load("system.conditions", "[]") || "[]")));
  let condRates = $state<Record<string, { count: number; perSecond: number | null }>>({});
  let newCondName = $state("");
  let newCondExpr = $state("");
  let condError = $state<string | null>(null);
  const COND_WINDOW = 5000;                     // 条件を数える窓（最後の記録の件数）

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
      prev = null;                              // 番地が変わった: 毎秒の増え方は次の読みから
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
    void poll();
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(timer);
  });

  let polling = false;
  async function poll() {
    if (polling) return;                         // 前の問い合わせが 1 秒を超えたら重ねない（毎秒の増え方が乱れる）
    polling = true;
    try {
      const s: Status = await conn.call("system.status", { addresses: watches.map((w) => w.address) }, { quiet: true });
      const cur = { t_us: s.time_us, memory: s.mcu.memory ?? {} };
      perSecond = rates(prev, cur);
      prev = cur;
      if (s.fpga.scans !== undefined) {
        scansPerSecond = prevScans && s.time_us > prevScans.t_us
          ? Math.round(((s.fpga.scans - prevScans.scans + 2 ** 32) % 2 ** 32) / ((s.time_us - prevScans.t_us) / 1e6)) : null;
        prevScans = { t_us: s.time_us, scans: s.fpga.scans };
      }
      status = s;
      rec = await conn.call("recording.status", {}, { quiet: true });
      if (obsMatch) haltOn = await conn.call("fpga_obs.trigger_status", {}, { quiet: true }).catch(() => haltOn);
      memoryStopped = !!rec?.recording && startedWatch &&
        !(await conn.call("watch.status", {}, { quiet: true }).catch(() => null))?.running;
      if (!rec?.recording) startedPins = false;
      if (!rec?.recording && startedWatch) {          // 記録が条件・AI・別の窓で止まった: 記録のための読みも止める
        startedWatch = false;
        await conn.call("watch.stop", {}, { quiet: true }).catch(() => null);
      }
      // 見張りの様子: 記録中と、見張っていた記録が止まった後（条件で止めたことを知らせる）
      if (rec?.recording || trigger) trigger = await conn.call("recording.trigger_status", {}, { quiet: true }).catch(() => null);
      if (trigger?.state === "off") trigger = null;
      error = null;
      if (conditions.length && s.fpga.driving) {
        const r = await conn.call("fpga_obs.read", { last: COND_WINDOW }, { quiet: true });
        const items: ObsItem[] = r?.items ?? [];
        condRates = Object.fromEntries(conditions.map((c) => [c.expr, countCondition(items, parseCondition(c.expr) ?? [], s.time_us)]));
      }
    } catch (e) {
      error = errorText(e);
    } finally {
      polling = false;
    }
  }

  async function addWatch() {
    let a = parseAddress(newAddress);
    if (a === null && isSymbolName(newAddress)) {   // 変数名: 読み込んだ ELF で番地に（名前が空なら変数名）
      const info = await conn.call("debug.symbol.info", { location: newAddress.trim() }, { quiet: true }).catch((e) => {
        addError = errorText(e);
        return null;
      });
      if (!info) return;
      const w = symbolWatch(newAddress, newName, info);
      if (typeof w === "string") {
        addError = $i18n(w);
        return;
      }
      a = w.address;
      newName = w.name;
    }
    if (a === null || !newName.trim()) {
      addError = $i18n("system.badAddress");
      return;
    }
    if (watches.some((w) => w.name === newName.trim() && w.address !== a)) {   // 名前は記録の段・条件で使う
      addError = $i18n("system.duplicateName");
      return;
    }
    watches = [...watches.filter((w) => w.address !== a), { name: newName.trim(), address: a }];
    newName = newAddress = "";
    addError = null;
    prev = null;
  }
  const removeWatch = (a: number) => (watches = watches.filter((w) => w.address !== a));

  function addCondition() {
    if (!parseCondition(newCondExpr)) {
      condError = $i18n("system.badCondition");
      return;
    }
    conditions = [...conditions.filter((c) => c.expr !== newCondExpr.trim()),
                  { name: newCondName.trim() || newCondExpr.trim(), expr: newCondExpr.trim() }];
    newCondName = newCondExpr = "";
    condError = null;
  }
  // 記録: MCU（DAP・メモリの標本・UART）・ピン（ロジアナ）・FPGA の観測が 1 本の時間軸に入る
  async function startRecording() {
    note = null;
    if (await app.act("recording.start", { name: "system" }) === undefined) return;
    if (withMemory && watches.length) await watchMemory();
    startedPins = withPins && await app.act("recording.la_stream.start", { rle: true }) !== undefined;   // 変化点の圧縮（遅い信号なら長く流せる）
    await poll();
  }
  // MCU のメモリを DAP サンプラで記録へ（時刻つきの値 = 時間軸の段。名前は表の名前）。サンプラは 1 つなので、ほかの
  // 道具（変数のライブ観測・タスクの時間軸等）が使っていれば取り上げずに知らせる
  async function watchMemory() {
    const holder = await conn.call("probe.sampler.status", {}, { quiet: true }).catch(() => null);
    if (holder?.user === "watch") {                 // 前の記録のために始めて残った読みなら引き継ぐ
      const running = await conn.call("watch.status", {}, { quiet: true }).catch(() => null);
      if (ownWatch(running?.watches ?? [], watches)) {
        startedWatch = true;
        return;
      }
    }
    if (holder?.user) {
      note = $i18n("system.samplerBusy", { user: holder.user });
      return;
    }
    startedWatch = await app.act("watch.set", { expressions: watches.map((w) => addressKey(w.address)),
                                                names: watches.map((w) => w.name),
                                                rate_hz: memoryRateHz(withPins, $app.device?.link) }) !== undefined;
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
    const args = Object.fromEntries(Object.entries(status?.mcu.connect ?? {}).filter(([, v]) => v !== null));
    await app.act("target.disconnect", {});
    await app.act("target.connect", args);
    prev = null;                                 // 毎秒の増え方はつなぎ直した後から
    await poll();
  }
  // 観測の回路が答えなくなった（対象の FPGA を書き直した・電源を入れ直した等）: 前と同じ設定で開き直す
  async function reopenFpga() {
    if (status?.fpga.config) await app.act("fpga_obs.open", { ...status.fpga.config, auto_reopen: Boolean(status.fpga.auto_reopen) });
    await poll();
  }
  async function showTimeline() {
    let flat: number[] = [];
    if (rec?.name) {
      tl.setSource("recording");
      // 記録の中で変化の無いピンは隠す（16 段が MCU・FPGA の段を画面の外へ押し出さないように）
      const ov = await conn.call("recording.overview", { width: 400, streams: [] }, { quiet: true }).catch(() => null);
      flat = flatChannels(ov?.waveform ?? []);
    }
    tl.arrangeForSystem(flat);
    dock.show("timeline");
  }

  // 結果の直前に多い出来事（recording.correlate）: 結果の条件の直前に、FPGA の条件の表と追加の候補のどれが来ているか
  interface Cause { condition: string; count: number; judged?: number; preceded: number | null; coverage: number | null; followed: number | null;
                    delay_s: { median: number } | null; examples: { cause_t: number; effect_t: number }[];
                    same?: string[] }   // 同じ時刻に成立する候補（自動の候補の重複をまとめた）
  interface Correlation { effect: { count: number }; causes: Cause[]; timebase_hz: number; auto?: boolean;
                          sampling: Record<string, number | null>; fpga_hidden: number;
                          warnings?: string[]; no_values?: string[]; range: [number, number]; gaps?: Gap[] }
  // データの欠け（その間の結果・候補は数えられていない）。黙って数え落とさないために一覧にし、時間軸で見られるように
  interface Gap { reason: string; t0: number; t1: number; variable?: string; count?: number }
  const GAPS_SHOWN = 5;
  let effectExpr = $state(load("system.effect", "") || "");
  let extraCauses = $state(load("system.extraCauses", "") || "");
  // 窓は使う人が入れる（preceded・coverage を左右するので、既定の値で決めない）。null = 入れていない
  const savedWindow = parseFloat(load("system.windowMs", ""));
  let windowMs = $state<number | null>(Number.isFinite(savedWindow) && savedWindow > 0 ? savedWindow : null);
  const windowOk = $derived(windowMs !== null && Number.isFinite(windowMs) && windowMs > 0);
  $effect(() => save("system.effect", effectExpr));
  $effect(() => save("system.extraCauses", extraCauses));
  $effect(() => save("system.windowMs", windowOk ? String(windowMs) : ""));
  let correlation = $state<Correlation | null>(null);
  let correlating = $state(false);
  let correlateError = $state<string | null>(null);
  const effectText = $derived(effectExpr.trim() || (watches[0] ? `${termName(watches[0].name)} changed` : ""));
  const causeList = $derived([...conditions.map((c) => c.expr),
                              ...extraCauses.split(/[\n,]/).map((x) => x.trim()).filter(Boolean)]);

  // auto = 候補を書かずに、記録の変数から自動で作って調べる（信号の変化・列挙の値・その組・ピン・ほかのメモリ）
  async function investigate(auto = false) {
    correlating = true;
    correlateError = null;
    try {
      correlation = await conn.call("recording.correlate", { effect: effectText, causes: auto ? null : causeList,
                                                             window_s: windowMs! / 1000 });
    } catch (e) {
      correlation = null;
      correlateError = errorText(e);
    } finally {
      correlating = false;
    }
  }
  async function showExample(ex: { cause_t: number; effect_t: number }) {
    tl.setSource("recording");
    await conn.call("ui.state.set", exampleView(ex, correlation?.timebase_hz ?? 100e6), { quiet: true });
    dock.show("timeline");
  }
  // 値が続いた時間（recording.durations）: 例 FPGA の state が BUSY の長さと MCU の待ち時間を比べる
  interface Durations { values: Record<string, { count: number; share: number | null;
                                                 duration_s: { min: number; median: number; p90: number; max: number } | null }>;
                        excluded?: number; gaps?: Gap[] }
  let durationVar = $state(load("system.durationVar", "") || "");
  $effect(() => save("system.durationVar", durationVar));
  let durationResult = $state<Durations | null>(null);
  let durationError = $state<string | null>(null);
  async function measureDurations() {
    durationError = null;
    try {
      durationResult = await conn.call("recording.durations", { variable: durationVar.trim() });
    } catch (e) {
      durationResult = null;
      durationError = errorText(e);
    }
  }
  const ms = (s: number) => (s * 1000).toFixed(s < 0.01 ? 3 : 2);

  // 区間ごとの集計（recording.transactions）: 記録をリクエスト等の区間に切り、開始時の値ごとに結果の割合・条件の回数
  interface TxGroup { by: string | null; count: number; effect_rate: number | null; counts: Record<string, number> }
  let txStart = $state(load("system.txStart", "") || "");
  let txBy = $state(load("system.txBy", "") || "");
  let txCounts = $state(load("system.txCounts", "") || "");
  let txEffect = $state(load("system.txEffect", "") || "");
  let txAtEnd = $state(load("system.txAtEnd", "0") === "1");
  let txLagMs = $state(Number(load("system.txLagMs", "0")) || 0);   // 結果を前の区間に寄せる（前の要求の結果が次に読まれるとき）
  $effect(() => save("system.txLagMs", String(txLagMs)));
  $effect(() => save("system.txAtEnd", txAtEnd ? "1" : "0"));
  $effect(() => { save("system.txStart", txStart); save("system.txBy", txBy); save("system.txCounts", txCounts); save("system.txEffect", txEffect); });
  let txResult = $state<{ transactions: number; groups: TxGroup[]; effect: string | null; excluded?: number; gaps?: Gap[]; excluded_by?: Record<string, number>;
                                 length_s?: { median: number } | null; effect_offset_s?: { median: number } | null;
                                 by_effect?: Record<"with" | "without", { count: number; counts: Record<string, number | null> }> | null } | null>(null);
  let txError = $state<string | null>(null);
  async function runTransactions() {
    txError = null;
    try {
      txResult = await conn.call("recording.transactions", {
        start: txStart.trim(), by: txBy.trim() || null, effect: txEffect.trim() || null, rows: 0,
        by_at: txAtEnd ? "end" : "start", effect_lag_s: Math.max(0, txLagMs) / 1000,
        counts: txCounts.split(",").map((x) => x.trim()).filter(Boolean) });
    } catch (e) {
      txResult = null;
      txError = errorText(e);
    }
  }

  // 結果の直前に多い出来事のうち FPGA の信号だけの条件は、FPGA の条件の表に足して毎秒の回数を見張れる
  const fpgaNames = $derived([...new Set((status?.fpga.latest ?? []).flatMap((l) => Object.keys(l.fields ?? {})))]);
  const addAsCondition = (expr: string) => {
    if (!conditions.some((c) => c.expr === expr)) conditions = [...conditions, { name: expr, expr }];
  };

  const pct = (x: number | null) => (x === null ? "—" : `${Math.round(x * 100)} %`);
  const resolutionMs = $derived(Math.max(0, ...Object.values(correlation?.sampling ?? {}).map((x) => (x ?? 0) * 1000)));

  const removeCondition = (expr: string) => (conditions = conditions.filter((c) => c.expr !== expr));
  const fmtRate = (x: number | null | undefined) => (x === null || x === undefined ? "—" : x >= 10 ? Math.round(x).toString() : x.toFixed(1));

  const mcuOk = $derived(status?.mcu.connected && !status.mcu.error);
  const fpgaOk = $derived(status?.fpga.available && status.fpga.running && !status.fpga.stuck);
  const modeKey = (m?: string) => (m === "snapshot" ? "system.mode.snapshot" : "system.mode.stream");
</script>

<div class="pane system">
  <div class="pane-bar">
    <strong>{$i18n("panel.system")}</strong>
    <span class="muted">{$i18n("system.tip")}</span>
    <span class="spacer"></span>
    {#if rec?.recording}
      <span class="badge ok">● {$i18n("system.recording", { n: (rec.events ?? 0).toLocaleString() })}</span>
      <button onclick={stopRecording}>{$i18n("system.stopRecording")}</button>
    {:else}
      <label class="small" title={$i18n("system.withMemoryTip")}><input type="checkbox" class="check" bind:checked={withMemory} />{$i18n("system.withMemory")}</label>
      <label class="small" title={$i18n("system.withPinsTip")}><input type="checkbox" class="check" bind:checked={withPins} />{$i18n("system.withPins")}</label>
      <button title={$i18n("system.startRecordingTip")} disabled={!linkOpen} onclick={startRecording}>{$i18n("system.startRecording")}</button>
    {/if}
    <button title={$i18n("system.timelineTip")} onclick={showTimeline}>{$i18n("system.timeline")}</button>
  </div>
  {#if error}<div class="note bad">{error}</div>{/if}
  {#if rec?.recording}
    <div class="trigger-bar" title={$i18n("system.triggerTip")}>
      <span class="small">{$i18n("system.trigger")}</span>
      <input class="mono" placeholder="errors changed" bind:value={triggerExpr} disabled={trigger?.state === "armed" || trigger?.state === "fired"} />
      <label class="small"><input type="checkbox" class="check" bind:checked={triggerStop} />{$i18n("system.triggerStop")}</label>
      {#if triggerStop}<label class="small">{$i18n("system.triggerPost")}<input class="post" type="number" min="0" step="0.5" bind:value={triggerPost} /></label>{/if}
      {#if trigger?.state === "armed" || trigger?.state === "fired"}
        <span class="small" class:ok={trigger.state === "fired"}>{$i18n("system.triggerHits", { n: trigger.hits ?? 0 })}</span>
        <button onclick={clearTrigger}>{$i18n("system.triggerClear")}</button>
      {:else}
        <button disabled={!triggerExpr.trim()} onclick={armTrigger}>{$i18n("system.triggerArm")}</button>
      {/if}
    </div>
  {/if}
  {#if trigger?.state === "error"}<div class="note bad">{trigger.error}</div>{/if}
  {#if trigger?.state === "stopped"}<div class="note">{$i18n("system.triggerStopped", { n: trigger.hits ?? 0 })}</div>{/if}
  {#if note}<div class="note">{note}</div>{/if}
  {#if memoryStopped}<div class="note bad">{$i18n("system.memoryStopped")}</div>{/if}
  {#if pinsStopped}<div class="note bad" title={rec?.la_stream?.error ?? ""}>{$i18n("system.pinsStopped", { why: rec?.la_stream?.error ? ` (${rec.la_stream.error})` : rec?.la_stream?.overrun ? " (overrun)" : "" })}</div>{/if}
  {#if rec?.recording && rec.events_errors}
    <div class="note" title={rec.events_errors.last ?? ""}>{$i18n("system.eventsErrors", { n: rec.events_errors.count, lost: rec.events_errors.lost.toLocaleString() })}</div>
  {/if}

  <div class="peers">
    <!-- MCU -->
    <section class="peer" aria-label="MCU">
      <header>
        <span class="dot" class:ok={mcuOk} class:bad={status?.mcu.error}></span>
        <strong>MCU</strong>
        {#if status?.mcu.connected}
          <span class="mono">{status.mcu.state ?? "?"}</span>
          {#if haltReasonKey(status.mcu)}<span class="muted">{$i18n(haltReasonKey(status.mcu)!)}</span>{/if}
          {#if status.mcu.state === "HALTED"}
            <!-- 止まった MCU の中（ELF が無くても使えるもの）: 呼び出し履歴・逆アセンブル・レジスタ -->
            <button title={$i18n("system.inspectTip")}
                    onclick={() => ["registers", "disassembly", "callStack"].forEach((id) => dock.show(id))}>{$i18n("system.inspect")}</button>
          {/if}
          {#if status.mcu.target}<span class="muted">{status.mcu.target}{status.mcu.core !== undefined ? ` core ${status.mcu.core}` : ""}</span>{/if}
        {:else}
          <span class="muted">{$i18n("system.notConnected")}</span>
        {/if}
      </header>
      {#if status?.mcu.error}
        <div class="note bad">{status.mcu.error}
          {#if status.mcu.connect}<button title={$i18n("system.reconnectTip")} onclick={reconnectMcu}>{$i18n("system.reconnect")}</button>{/if}
        </div>
      {/if}
      <div class="sub-title" title={$i18n("system.memoryTip")}>{$i18n("system.memory")}</div>
      {#if !watches.length}<div class="muted small">{$i18n("system.memoryHint")}</div>{/if}
      <table class="grid">
        <thead><tr><th>{$i18n("system.name")}</th><th>{$i18n("system.address")}</th><th>{$i18n("system.value")}</th>
          <th>{$i18n("system.rate")}</th><th></th></tr></thead>
        <tbody>
          {#each watches as w (w.address)}
            {@const v = status?.mcu.memory?.[addressKey(w.address)]}
            <tr>
              <td>{w.name}</td>
              <td class="mono muted">{addressKey(w.address)}</td>
              <td class="mono">{v === undefined ? "—" : `${v} (0x${v.toString(16).toUpperCase()})`}</td>
              <td class="mono">{perSecond[addressKey(w.address)] ?? "—"}</td>
              <td class="nowrap"><button class="x" title={$i18n("fpgaObs.remove")} aria-label={$i18n("fpgaObs.remove")} onclick={() => removeWatch(w.address)}>✕</button></td>
            </tr>
          {/each}
          <tr>
            <td><input placeholder={$i18n("system.name")} bind:value={newName} /></td>
            <td><input class="mono" placeholder="0x20040800 / g_errors" bind:value={newAddress}
                       onkeydown={(e) => e.key === "Enter" && addWatch()} /></td>
            <td colspan="3"><button onclick={addWatch}>{$i18n("system.addWatch")}</button></td>
          </tr>
        </tbody>
      </table>
      {#if addError}<div class="note bad">{addError}</div>{/if}
    </section>

    <!-- FPGA -->
    <section class="peer" aria-label="FPGA">
      <header>
        <span class="dot" class:ok={fpgaOk} class:bad={status?.fpga.stuck} class:warn={status?.fpga.overflow_seen}></span>
        <strong>FPGA</strong>
        {#if status?.fpga.available}
          {#if status.fpga.driving}
            <span>{$i18n(modeKey(status.fpga.mode))}</span>
            <span class="mono muted">{$i18n("system.scansPerSecond")} {scansPerSecond ?? "—"}</span>
            {#if status.fpga.reopened}<span class="muted" title={$i18n("fpgaObs.autoReopenTip")}>{$i18n("fpgaObs.reopened", { n: status.fpga.reopened })}</span>{/if}
          {:else}
            <span class="muted">{$i18n("system.fpgaIdle")}</span>
            <button onclick={() => dock.show("fpgaObs")}>{$i18n("panel.fpgaObs")}</button>
          {/if}
        {:else if status}
          <span class="muted">{$i18n("system.noFpga")}</span>
        {/if}
      </header>
      {#if status?.fpga.stuck}
        <div class="note bad">{$i18n("fpgaObs.state.stuck")}
          {#if status.fpga.config}<button title={$i18n("system.reopenTip")} onclick={reopenFpga}>{$i18n("system.reopen")}</button>{/if}
        </div>
      {/if}
      <div class="sub-title">{status?.fpga.driving ? $i18n("system.latest") : $i18n("system.lastSeen")}</div>
      {#if status?.fpga.latest?.length}
        <table class="grid">
          <thead><tr><th>{$i18n("fpgaObs.group")}</th><th>{$i18n("system.value")}</th><th></th></tr></thead>
          <tbody>
            {#each status.fpga.latest as l (l.group)}
              <tr class:stale={!status.fpga.driving}>
                <td class="mono">{l.group}</td><td class="mono">{fieldsText(l.fields, l.value)}</td>
                <td class="muted nowrap">{l.time_us !== null ? $i18n("system.ago", { s: Math.max(0, (status.time_us - l.time_us) / 1e6).toFixed(1) }) : ""}</td>
              </tr>
            {/each}
          </tbody>
        </table>
        <div class="muted small">{$i18n("system.gaps")} {status.fpga.gaps ?? 0}</div>
      {:else if status?.fpga.available}
        <div class="muted small">{status.fpga.latest_error ? $i18n("system.noValues") : $i18n("system.noChange")}</div>
      {/if}
      <div class="sub-title" title={$i18n("system.conditionsTip")}>{$i18n("system.conditions")}</div>
      {#if obsMatch && haltOn?.orphan}
        <div class="note">{$i18n(haltOn.halt ? "system.haltOrphan" : "system.laOrphan", { n: haltOn.count ?? 0 })}
          <button class="armed" onclick={clearHalt}>{$i18n("system.haltDisarm")}</button></div>
      {/if}
      {#if !conditions.length}<div class="muted small">{$i18n("system.conditionsHint")}</div>{/if}
      <table class="grid">
        <thead><tr><th>{$i18n("system.name")}</th><th>{$i18n("system.condition")}</th><th class="nowrap">{$i18n("system.rate")}</th><th></th></tr></thead>
        <tbody>
          {#each conditions as c (c.expr)}
            <tr>
              <td>{c.name}</td><td class="mono">{c.expr}</td>
              <td class="mono">{fmtRate(condRates[c.expr]?.perSecond)}</td>
              <td class="nowrap">
                {#if obsMatch}
                  <button title={$i18n(rec?.la_stream ? "system.captureOnStreamingTip" : "system.captureOnTip")}
                          disabled={capturingOn !== null || Boolean(rec?.la_stream)}
                          onclick={() => captureOn(c.expr)}>{capturingOn === c.expr ? $i18n("system.capturingOn") : $i18n("system.captureOn")}</button>
                {/if}
                {#if obsMatch && haltOn?.condition === c.expr && haltOn.halt !== false}
                  <button class="armed" title={$i18n("system.haltArmedTip")} onclick={clearHalt}>{haltOn.count ? $i18n("system.haltFired", { n: haltOn.count }) : $i18n("system.haltArmed")}</button>
                {:else if obsMatch}
                  <button title={$i18n("system.haltOnTip")} disabled={capturingOn !== null}
                          onclick={() => armHalt(c.expr)}>{$i18n("system.haltOn")}</button>
                {/if}
                <button class="x" title={$i18n("fpgaObs.remove")} aria-label={$i18n("fpgaObs.remove")} onclick={() => removeCondition(c.expr)}>✕</button></td>
            </tr>
          {/each}
          <tr>
            <td><input placeholder={$i18n("system.name")} bind:value={newCondName} /></td>
            <td><input class="mono" placeholder="restart changed and state=BUSY" bind:value={newCondExpr}
                       onkeydown={(e) => e.key === "Enter" && addCondition()} /></td>
            <td colspan="2"><button onclick={addCondition}>{$i18n("system.addWatch")}</button></td>
          </tr>
        </tbody>
      </table>
      {#if condError}<div class="note bad">{condError}</div>{/if}
    </section>
  </div>

  <!-- 結果の直前に多い出来事: 記録の中で、結果（MCU のメモリの変化等）の直前にどの条件が来ているか -->
  <section class="causes" aria-label={$i18n("system.causes")}>
    <div class="sub-title" title={$i18n("system.causesTip")}>{$i18n("system.causes")}</div>
    {#if !rec?.name}<div class="muted small">{$i18n("system.causesHint")}</div>{/if}
    <div class="cause-form">
      <label>{$i18n("system.effect")}
        <input class="mono" placeholder={effectText || "errors changed"} bind:value={effectExpr} /></label>
      <label>{$i18n("system.extraCauses")}
        <input class="mono" placeholder="in0 rose, addr=RESULT and write rose" bind:value={extraCauses} /></label>
      <label class="narrow">{$i18n("system.window")}
        <input type="number" min="0.01" step="any" placeholder="ms" bind:value={windowMs} /></label>
      <button disabled={!linkOpen || correlating || !effectText || !causeList.length || !windowOk} onclick={() => investigate()}>
        {correlating ? $i18n("system.investigating") : $i18n("system.investigate")}</button>
      <button title={$i18n("system.autoInvestigateTip")} disabled={!linkOpen || correlating || !effectText || !windowOk}
              onclick={() => investigate(true)}>{$i18n("system.autoInvestigate")}</button>
    </div>
    {#if correlateError}<div class="note bad">{correlateError}</div>{/if}
    {#if correlation}
      <div class="muted small">{$i18n("system.causeNote", { n: correlation.effect.count, ms: resolutionMs.toFixed(resolutionMs < 1 ? 3 : 1),
                                                           hidden: correlation.fpga_hidden })}</div>
      {#each correlation.warnings ?? [] as w (w)}<div class="note">{w}</div>{/each}
      {#if correlation.gaps?.length}
        {@const hz = correlation.timebase_hz}
        <ul class="gap-list small">
          {#each correlation.gaps.slice(0, GAPS_SHOWN) as g, i (i)}
            <li><span>{$i18n(`system.gap.${g.reason}` as Key)}{g.variable ? ` (${g.variable})` : ""}{g.count ? ` ×${g.count}` : ""}</span>
              <span class="mono muted">{((g.t0 - correlation.range[0]) / hz).toFixed(3)} s{g.t1 > g.t0 ? ` +${((g.t1 - g.t0) / hz * 1000).toFixed(1)} ms` : ""}</span>
              <button onclick={() => showExample({ cause_t: g.t0, effect_t: g.t1 })}>{$i18n("system.showExample")}</button></li>
          {/each}
          {#if correlation.gaps.length > GAPS_SHOWN}<li class="muted">{$i18n("system.gapsMore", { n: correlation.gaps.length - GAPS_SHOWN })}</li>{/if}
        </ul>
      {/if}
      {#if correlation.no_values?.length}
        <div class="note" title={$i18n("system.noValuesTip")}>{$i18n("system.noValues2", { names: correlation.no_values.join(", ") })}</div>
      {/if}
      <!-- 候補ごとに 2 行: 名前・式・回数・見る / 数（狭い右の列でも式が折り返さない） -->
      <ol class="cause-list">
        {#each correlation.causes as c (c.condition)}
          {@const named = conditions.find((x) => x.expr === c.condition && x.name !== x.expr)}
          <li>
            <div class="cause-head">
              <span class="cause-name" title={[c.condition, ...(c.same ?? [])].join(" / ")}>{#if named}<strong>{named.name}</strong> <span class="mono muted">{c.condition}</span>{:else}<span class="mono">{c.condition}</span>{/if}
                <span class="muted">({c.count})</span>
                {#if c.same?.length}<span class="muted small">{$i18n("system.sameAs", { list: c.same.join(" / ") })}</span>{/if}</span>
              {#if fpgaOnly(c.condition, fpgaNames) && !conditions.some((x) => x.expr === c.condition)}
                <button title={$i18n("system.addAsConditionTip")} onclick={() => addAsCondition(c.condition)}>{$i18n("system.addAsCondition")}</button>
              {/if}
              {#if c.examples.length}<button onclick={() => showExample(c.examples[0])}>{$i18n("system.showExample")}</button>{/if}
            </div>
            <div class="cause-stats mono">
              <span title={$i18n("system.precededTip")}>{$i18n("system.preceded")} <b>{pct(c.preceded)}</b>{#if c.judged !== undefined && c.judged < correlation.effect.count}<span class="muted" title={$i18n("system.judgedTip")}> {$i18n("system.judged", { n: c.judged, of: correlation.effect.count })}</span>{/if}</span>
              <span title={$i18n("system.coverageTip")}>{$i18n("system.coverage")} <b>{pct(c.coverage)}</b></span>
              <span title={$i18n("system.followedTip")}>{$i18n("system.followed")} <b>{pct(c.followed)}</b></span>
              <span>{$i18n("system.delay")} <b>{c.delay_s ? `${(c.delay_s.median * 1000).toFixed(2)} ms` : "—"}</b></span>
            </div>
          </li>
        {/each}
      </ol>
    {/if}

    <div class="sub-title" title={$i18n("system.durationsTip")}>{$i18n("system.durations")}</div>
    <div class="cause-form">
      <label>{$i18n("system.variable")}
        <input class="mono" placeholder="state" bind:value={durationVar}
               onkeydown={(e) => e.key === "Enter" && durationVar.trim() && measureDurations()} /></label>
      <button disabled={!linkOpen || !durationVar.trim()} onclick={measureDurations}>{$i18n("system.measure")}</button>
    </div>
    {#if durationError}<div class="note bad">{durationError}</div>{/if}
    {#if durationResult}
      {#if durationResult.excluded}<div class="note" title={$i18n("system.excludedTip")}>{$i18n("system.durExcluded", { n: durationResult.excluded, gaps: durationResult.gaps?.length ?? 0 })}</div>{/if}
      <table class="grid">
        <thead><tr><th>{$i18n("system.value")}</th><th class="nowrap">{$i18n("system.count")}</th>
          <th class="nowrap">{$i18n("system.share")}</th><th class="nowrap">{$i18n("system.durationMs")}</th></tr></thead>
        <tbody>
          {#each Object.entries(durationResult.values) as [value, d] (value)}
            <tr>
              <td class="mono">{value}</td><td class="mono">{d.count}</td><td class="mono">{pct(d.share)}</td>
              <td class="mono">{d.duration_s ? `${ms(d.duration_s.min)} / ${ms(d.duration_s.median)} / ${ms(d.duration_s.p90)} / ${ms(d.duration_s.max)}` : "—"}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}

    <div class="sub-title" title={$i18n("system.transactionsTip")}>{$i18n("system.transactions")}</div>
    <div class="cause-form">
      <label>{$i18n("system.txStart")}<input class="mono" placeholder="state changed and state=BUSY" bind:value={txStart} /></label>
      <label>{$i18n("system.txBy")}<input class="mono" placeholder="left_hi" bind:value={txBy} /></label>
      <label class="check nowrap" title={$i18n("system.txAtEndTip")}><input type="checkbox" bind:checked={txAtEnd} /> {$i18n("system.txAtEnd")}</label>
      <label>{$i18n("system.txCounts")}<input class="mono" placeholder="restart changed and state=BUSY" bind:value={txCounts} /></label>
      <label>{$i18n("system.effect")}<input class="mono" placeholder="errors changed" bind:value={txEffect}
               onkeydown={(e) => e.key === "Enter" && txStart.trim() && runTransactions()} /></label>
      <label class="narrow" title={$i18n("system.txLagTip")}>{$i18n("system.txLag")}<input type="number" min="0" step="0.01" class="mono" bind:value={txLagMs} /></label>
      <button disabled={!linkOpen || !txStart.trim()} onclick={runTransactions}>{$i18n("system.txRun")}</button>
    </div>
    {#if txError}<div class="note bad">{txError}</div>{/if}
    {#if txResult}
      <div class="muted small">{$i18n("system.txTotal", { n: txResult.transactions.toLocaleString() })}</div>
      {#if txResult.excluded}<div class="note" title={$i18n("system.excludedTip")}>{$i18n("system.txExcluded", { n: txResult.excluded, gaps: txResult.gaps?.length ?? 0 })}
        <span class="mono">{Object.entries(txResult.excluded_by ?? {}).map(([k, v]) => `${k} ${v}`).join(" / ")}</span></div>{/if}
      {#if txResult.effect_offset_s && txResult.length_s}
        <div class="muted small" title={$i18n("system.txOffsetTip")}>{$i18n("system.txOffset", { at: (txResult.effect_offset_s.median * 1000).toFixed(3), len: (txResult.length_s.median * 1000).toFixed(3) })}</div>
      {/if}
      <table class="grid">
        <thead><tr><th>{txBy || "—"}</th><th class="nowrap">{$i18n("system.txCount")}</th>
          {#if txResult.effect}<th class="nowrap">{$i18n("system.txEffectRate")}</th>{/if}
          {#each Object.keys(txResult.groups[0]?.counts ?? {}) as c (c)}<th class="mono">{c}</th>{/each}</tr></thead>
        <tbody>
          {#each txResult.groups as g (g.by)}
            <tr>
              <td class="mono">{g.by ?? "?"}</td><td class="mono">{g.count}</td>
              {#if txResult.effect}<td class="mono">{pct(g.effect_rate)}</td>{/if}
              {#each Object.values(g.counts) as v, k (k)}<td class="mono">{v.toFixed(2)}</td>{/each}
            </tr>
          {/each}
        </tbody>
      </table>
      <!-- 結果が起きた区間と起きなかった区間を並べる（違いを事実として見せる。判断はしない） -->
      {#if txResult.by_effect && Object.keys(txResult.by_effect.with.counts).length}
        {@const be = txResult.by_effect}
        <table class="grid" title={$i18n("system.txByEffectTip")}>
          <thead><tr><th></th><th class="nowrap">{$i18n("system.txCount")}</th>
            {#each Object.keys(be.with.counts) as c (c)}<th class="mono">{c}</th>{/each}</tr></thead>
          <tbody>
            {#each [["with", be.with], ["without", be.without]] as const as [k, row] (k)}
              <tr><td class="nowrap">{$i18n(k === "with" ? "system.txWithEffect" : "system.txWithoutEffect")}</td>
                <td class="mono">{row.count}</td>
                {#each Object.values(row.counts) as v, j (j)}<td class="mono">{v === null ? "—" : v.toFixed(2)}</td>{/each}</tr>
            {/each}
          </tbody>
        </table>
      {/if}
    {/if}
  </section>
</div>

<style>
  /* パネルの幅で並べ方を変える（ドックの中では窓の幅でなくパネルの幅が効く）: 広ければ左右、狭ければ上下 */
  .system { container-type: inline-size; }
  .peers { display: grid; grid-template-columns: 1fr; gap: 8px; padding: 8px; }
  @container (min-width: 900px) { .peers { grid-template-columns: 1fr 1fr; } }
  .peer { border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; min-width: 0; }
  .peer header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
  .badge { font-size: 11px; border: 1px solid currentColor; border-radius: 8px; padding: 0 6px; }
  .ok { color: var(--ok); }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--line); }
  .dot.ok { background: var(--ok); }
  .dot.warn { background: var(--warn); }
  .dot.bad { background: var(--bad); }
  .sub-title { font-weight: 600; font-size: 12px; margin: 6px 0 2px; }
  .note { padding: 3px 8px; font-size: 12px; }
  .bad { color: var(--bad); }
  .small { font-size: 12px; padding: 2px 0; }
  .check { width: auto; min-width: 0; margin-right: 4px; }
  input { width: 100%; min-width: 5em; box-sizing: border-box; }
  .grid { width: 100%; table-layout: auto; }
  .grid th { white-space: nowrap; text-align: left; }
  .nowrap, .nowrap button { white-space: nowrap; }
  .stale td { opacity: 0.6; }
  .causes { margin: 0 8px 8px; border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; }
  .cause-form { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: end; font-size: 12px; }
  .cause-form label { display: flex; flex-direction: column; flex: 1 1 14em; }
  .cause-form label.narrow { flex: 0 0 6em; }
  .cause-form label.check { flex: 0 0 auto; flex-direction: row; align-items: center; gap: 4px; padding-bottom: 3px; }
  .cause-form label.check input { width: auto; flex: 0 0 auto; margin: 0; }
  .trigger-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; padding: 4px 8px; }
  .trigger-bar input.mono { flex: 1 1 12em; width: auto; }
  .trigger-bar input.post { width: 4em; min-width: 0; }
  .armed { border-color: var(--bad); color: var(--bad); }
  button.x { padding: 0 6px; }
  .cause-list { list-style: none; margin: 4px 0 0; padding: 0; }
  .gap-list { list-style: none; margin: 2px 0; padding: 0; }
  .gap-list li { display: flex; align-items: center; gap: 8px; }
  .gap-list li > span:first-child { flex: 1; }
  .cause-list li { border-top: 1px solid var(--line); padding: 4px 0; }
  .cause-head { display: flex; align-items: center; gap: 8px; }
  .cause-name { flex: 1; min-width: 0; overflow-wrap: anywhere; }
  .cause-stats { display: flex; flex-wrap: wrap; gap: 2px 14px; font-size: 12px; color: var(--muted); }
  .cause-stats b { color: var(--text); font-weight: 600; }
</style>
