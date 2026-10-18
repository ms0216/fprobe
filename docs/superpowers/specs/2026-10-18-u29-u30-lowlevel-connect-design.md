# U29 接続の診断と低レベル操作・U30 接続の手順の上書きとロック解除（設計）

2026-10-18。対象の機能 ID: SEG-01・SEG-12・SEG-18〜21・IAR-35・OOCD-11〜13・BMP-02（U29）、
CUBE-20・MCUX-11・PYOCD-04・PYOCD-08（U30）。台帳は `docs/reports/2026-10-18-gap-analysis.md`。

## 層

| 層 | モジュール | 責務 | 機器 |
|---|---|---|---|
| 識別子 | `fprobe/coresight_id.py` | DPIDR・IDCODE・TARGETID・AP の IDR・CPUID の解読、ROM テーブルの走査（class 1 / class 9）、部品の判別（STM32 の DEV_ID・nRF52 の FICR・RP2040 / RP2350 の SYSINFO → pyOCD のターゲット名の候補） | 読む関数だけ |
| JTAG | `fprobe/jtag_chain.py` | TAP リセット、IR / DR のシフト、チェーンの走査（IDCODE・IR 長の推定）、生のビット列 | `JtagWire`（DAP_JTAG_Sequence 1 つ分） |
| DP / AP | `fprobe/raw_dap.py` | 線の選択（JTAG→SWD・休眠・TARGETSEL・SWD→JTAG）、DP（DPBANKSEL）・AP（ADIv5 / ADIv6 の SELECT）・MEM-AP、ACK の種類 | pyOCD の DAPAccessCMSISDAP |
| 診断 | `fprobe/diagnose.py` | 電圧 → ピン → SWD → JTAG → DP → AP・ROM → コア → 部品 → 保護、段ごとの status と日本語、verdict、suggested_connect | RawDap |
| 道具 | `fprobe/probe_lowlevel.py`（mixin） | ピン・DP / AP・JTAG・速度試験・診断・target_connect の auto | Session.dap_access / pyOCD |
| 手順 | `fprobe/connect_scripts.py` + `fprobe/data/connect_scripts/*.py` | pyOCD のユーザスクリプト（デリゲート）の検査（ast。実行しない）と内蔵の手順 | なし |
| オプションバイト | `fprobe/stm32_option_bytes.py` | 系列（F4 の OPTCR、L4・G4・G0 の OPTR）、名前での解読、変更の計画（確認の約束）、書き込みの手順 | read32 / write32 |
| 道具 | `fprobe/probe_connect.py`（mixin） | script_check・script_list・option_bytes_read / write・target_unlock | pyOCD / RawDap |

## 二つの経路（低レベルの道具）

- **接続中**（pyOCD のセッションあり）: DP / AP は pyOCD の `dp` を通す（pyOCD が SELECT を覚えているため、横から
  SELECT を書くと食い違う）。ピンは読むだけ。線を動かす道具（JTAG・ピンの書き込み・診断）は断る。
- **未接続**: `Session.dap_access()` で CMSIS-DAP の層だけを開く（pyOCD のターゲットを作らない）。`target_connect` の
  前に必ず閉じる（同じ DAP の接続口を pyOCD に渡す）。

## 安全の約束（U30）

- RDP Level 2（0xCC）は書かない（永久にデバッグが塞がる）。
- RDP を下げる（1 → 0 = 全消去）には `confirm_mass_erase`、上げる（0 → 1）には `confirm_protect`。無ければ
  ValueError で断り、**何も書かない**（halt もしない）。`target_unlock` も同じ（nRF の ERASEALL も）。
- 名前の無いビットは今の値を保つ。
- 実機試験ではオプションバイトは読むだけ。書き込み・解除は FLASH の模型（`host/tests/stm32_flash_fake.py`）で確かめる。

## 判断

- 部品の判別は「読める識別子」だけに頼る（DBGMCU・FICR・SYSINFO）。読み出しのフォールトは「その種類ではない」とみなし、
  誤りの印を消して次を試す。pyOCD に実在するターゲット名だけを候補にし、1 つに決まるときだけ suggested_target。
- JTAG の走査は IR に 0 を入れたまま Update-IR を通らない（0 = EXTEST の装置がある）。IR の合計長は「0 を詰めてから 1 を
  詰める」で測り、最後の IR はすべて 1（BYPASS）。装置ごとの IR 長は既知の表 → Capture-IR の "01" の境目、決まらなければ
  `guessed` と明示する。
- fprobe の DAP_SWJ_Pins が返すのは SWCLK・SWDIO・nRESET だけ（`not_reported` で明示。ゲートウェアは変えない）。
- 接続の手順は pyOCD の作法そのまま（独自の言語を作らない）。手順の名前と引数は pyOCD の `TargetDelegateInterface` から
  取るので、pyOCD が増やせば検査も追従する。

## cJTAG（IEEE 1149.7）の調査（実装しない）

- 2 線（TCKC・TMSC）。TMSC は双方向で、fprobe の SWDIO のピン（双方向）をそのまま使える。
- 起動: TCKC を High に保ったまま TMSC を何度か反転する「エスケープ」→ 活性化の符号（OAC・EC・CP）→ OScan1 等の形式。
  OScan1 は JTAG の 1 ビットを TCKC の 3 クロック（nTDI・TMS を出し、TDO を受ける）で送る。
- 今のゲートウェアのままでも試作はできる: エスケープは DAP_SWJ_Pins（SWCLK = 1 のまま SWDIO を反転）、OScan1 は
  DAP_SWD_Sequence（2 ビット出す + 1 ビット受ける を繰り返す）。ただし 1 ビットごとに区切りが要り遅い（推定 数 kbit/s）。
- 実用には `cjtag_phy`（ゲートウェア）と CMSIS-DAP のベンダーコマンドが要る。手持ちのターゲット（STM32・nRF・RP2040 /
  RP2350・Raspberry Pi）は cJTAG を必要としないので、需要が出るまで実装しない。

## OpenOCD 経由（OOCD-11〜13）

fprobe は標準の CMSIS-DAP v2（DAP_JTAG_Sequence を持つ）なので、OpenOCD の `svf`・`ipdbg`・`pld` はそのまま動く見込み。
SVF は実機試験（`gateware/tests/hil/test_lowlevel_hw.py`）で STM32 のチェーンの IDCODE を読んで確かめる。IPDBG・PLD は
相手の FPGA（Tang Nano 等）の配線が要るので手順だけを残す（skip）。
