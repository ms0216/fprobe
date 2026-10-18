# Apache NuttX のヘッダ（取り込んだ部分）の出典

- 名前: Apache NuttX
- 版: NuttX 13.1.0
- 出典: https://github.com/apache/nuttx のタグ `nuttx-13.1.0`（コミット `2b5c0de5e933281c209f954a63b99cf10c132346`）。
  raw.githubusercontent.com から個々のファイルを取得した（スクリプト等は取得も実行もしていない）
- 取得日: 2026-10-19
- 著作権: Copyright 2020 The Apache Software Foundation（`NOTICE`）と各ファイルの先頭の著作者
- ライセンス: ほとんどは Apache License 2.0（SPDX-License-Identifier: Apache-2.0。各ファイルの先頭）。全文は同じディレクトリの
  `LICENSE`、注意書きは `NOTICE`（どちらもリポジトリのものをそのまま写した）。次の 3 つは NuttX が取り込んだ他の
  ライセンスのもので、条文は各ファイルの先頭にある（NuttX の `LICENSE` にも載っている）:
  - `include/nuttx/list.h`・`include/nuttx/list_type.h`: BSD-2-Clause（Travis Geiselbrecht、2008）
  - `include/sys/uio.h`: BSD-3-Clause（Gregory Nutt・Stavros Polymenis）
- 変更: なし（中身はそのまま）。ディレクトリだけを NuttX の build が作る形にした: `arch/arm/include/` を `include/arch/`
  に、チップ（nRF52）の `arch/arm/include/nrf52/` を `include/arch/chip/` に置いた（NuttX の build は同じ形のリンクか写しを
  作る）。`include/arch/nrf52/nrf52_irq.h` は `<arch/nrf52/nrf52_irq.h>` で含まれるので、その形で置いた
- 取り込んだのは、`../nuttx_shape.c` が含めるヘッダ（GCC の -M で数えた全部）だけ。設定 `nuttx/config.h` は NuttX の build が
  .config から作るもので、ここには無い（`../config/nuttx/config.h` に手で書いた）

| 置いた場所 | 元の場所 | SHA-256（取得したもの） |
|---|---|---|
| `include/alloca.h` | `include/alloca.h` | `0e766c52cdc18d19f13430a8f992f63a0146812db72ccb24a09ac05367e4cf2b` |
| `include/assert.h` | `include/assert.h` | `291bbd17ec0a7ffc9b31576d72b93f1f53fd585366dab76aada7cafd2fa61b61` |
| `include/dirent.h` | `include/dirent.h` | `ff6a9cdb416428f4eda1001e853d313368e17467002eac30971eb80cf66c887d` |
| `include/errno.h` | `include/errno.h` | `994c632349040373db88ea2ab2eef3a8a5818f474f7360e2f379cd8ec1758928` |
| `include/fcntl.h` | `include/fcntl.h` | `a0c7a7aedd2de3ea6b72673474e6cd38adc4d813e523c82b1dbb963048ac2309` |
| `include/inttypes.h` | `include/inttypes.h` | `5e31dfbb72d54d76f6864e02de6e678df63c79d650227f7b47ffba615ac30902` |
| `include/limits.h` | `include/limits.h` | `e726f9f108d6c4d2315db2abf0920e0acf1ecccf4dd397876454cc7b8417411a` |
| `include/mqueue.h` | `include/mqueue.h` | `c9cd2b696aaaf09743372559bcadaca54eb339e131435858a9af168032e6067a` |
| `include/nuttx/addrenv.h` | `include/nuttx/addrenv.h` | `be0ba713b22f7bbfd3c0b13b029598e70f84fd3ca2a6a7c60bb0eeb07c39a5d0` |
| `include/nuttx/atexit.h` | `include/nuttx/atexit.h` | `1b8816818c78abf3037266fda2177eae38bb5ccd738c2375db41a00a45d246cf` |
| `include/nuttx/atomic.h` | `include/nuttx/atomic.h` | `63162a8bcf85e2e54ac00dc0d6d803f651076d9490bbb72e100ff77fdfe909cf` |
| `include/nuttx/cache.h` | `include/nuttx/cache.h` | `ec48a693e5829b85ea5e442f879eccb31aacc010a768a394a78f28479d0e5266` |
| `include/nuttx/clock.h` | `include/nuttx/clock.h` | `4a25b6ba78c0a35cd7babf53408452fd06c544fad4398eaa31f1d4ef3bcf15b8` |
| `include/nuttx/compiler.h` | `include/nuttx/compiler.h` | `98dec439fabbbf7bb9ed9f5a0b5728bb6c80c8c547cf9c34b0838ab99b067d2b` |
| `include/nuttx/event.h` | `include/nuttx/event.h` | `d64866538596ddfd9be6270d01f3c254e3843259f4ea4f2ea903db9206e394fd` |
| `include/nuttx/fs/fs.h` | `include/nuttx/fs/fs.h` | `5f87ece61436814967f8aa9ed58ca862cb397a0634370f197c0311510d718b48` |
| `include/nuttx/fs/ioctl.h` | `include/nuttx/fs/ioctl.h` | `825c84b56bd875f12e613b382ec4a9f22f282cce03bdead6e5124268eb65fc31` |
| `include/nuttx/fs/uio.h` | `include/nuttx/fs/uio.h` | `e15c42df18a347c5ad9a466f52e8849b3803e7ec32f6db53350bde816f66210c` |
| `include/nuttx/init.h` | `include/nuttx/init.h` | `b2bc2bbd5f049ac4e853420e62b633a54fc22ed01da2dc4847b92eaa4dee73e0` |
| `include/nuttx/irq.h` | `include/nuttx/irq.h` | `26234d3172f50ef16099cd9e07fb5468f3d422381f89883c03ab43a63d495de5` |
| `include/nuttx/lib/math32.h` | `include/nuttx/lib/math32.h` | `269f53d81828634a2803b5a8a8ddc96c5bbf74c711d68152bf1e252f9ec90de7` |
| `include/nuttx/list.h` | `include/nuttx/list.h` | `1d934f903184be32ebfb38a4a9497e1c8be1f2a022edce4b2d93310feb76ed6b` |
| `include/nuttx/list_type.h` | `include/nuttx/list_type.h` | `f1fa167ef8ba82ff798ece798597defa96fcebca17c5119beea8c5478719cd36` |
| `include/nuttx/mm/gran.h` | `include/nuttx/mm/gran.h` | `bc9413ae9e42292756f04b868b2ec4ecbaa546abd0fc96fd614ff671b992d4ef` |
| `include/nuttx/mm/map.h` | `include/nuttx/mm/map.h` | `c2c6d0ed8fea645755a38f739f0317b3ba0225b401c706bf186f9731956c66bd` |
| `include/nuttx/mqueue.h` | `include/nuttx/mqueue.h` | `09dbc1e35eb61a81c478e37bb39a317a3c43251d04d8a60ff2a40c327580e387` |
| `include/nuttx/mutex.h` | `include/nuttx/mutex.h` | `ea946848d4b7ca1484974db01ca1abd42e10294c76265ebb7e5183700aaf9258` |
| `include/nuttx/pgalloc.h` | `include/nuttx/pgalloc.h` | `4333948f698f1b97db98f8affda94aa1753a79f5bfcdc191eee5ec2914438818` |
| `include/nuttx/queue.h` | `include/nuttx/queue.h` | `03ab7f4d8a4ea51dde711634c2b8bc9bb12d303222081855729bf0f92406cc2c` |
| `include/nuttx/sched.h` | `include/nuttx/sched.h` | `5cc14e5f63b75a7cc0586c5494e0cb2d15e5fb2cc0528eada7cffbcbfb1859d8` |
| `include/nuttx/semaphore.h` | `include/nuttx/semaphore.h` | `be7519a9deb546942ae7272672f93d9febb2f8981fbf40c944cc2e67e2b02376` |
| `include/nuttx/serial/tioctl.h` | `include/nuttx/serial/tioctl.h` | `3af54c279524abde89e5e9f2e969b1cc6badda05e79722f69903345eb37a0dc8` |
| `include/nuttx/signal.h` | `include/nuttx/signal.h` | `054c4c978d37305747aed6780995b8e284d525e3ade9440b6faf3630be575631` |
| `include/nuttx/spawn.h` | `include/nuttx/spawn.h` | `14c55116de0dba862ac2f63685e20b5e084bda8cbd1fe421e0c4c16eeab6a5ea` |
| `include/nuttx/spinlock_type.h` | `include/nuttx/spinlock_type.h` | `27ec36420caec8501a85ca1496f16301eac24b4119d43526af1548fb127e1726` |
| `include/nuttx/tls.h` | `include/nuttx/tls.h` | `673dd9d615ade84e6c226228991dfe66ef0c356a3b8ef33e98f53c907fd061dd` |
| `include/nuttx/wdog.h` | `include/nuttx/wdog.h` | `4b241bcfd40860babbf5e92bfab2664d8098774fa3d29560647222d3755b314e` |
| `include/nuttx/wqueue.h` | `include/nuttx/wqueue.h` | `6a40edb4c053c29c3fbefa60a6e972899afce95f7f3b919d764f48b98bd7adba` |
| `include/poll.h` | `include/poll.h` | `fe22c2d29d741d3f26e53eab59d9b1bbb5bfced23b3655bd8cc09718e05396ff` |
| `include/pthread.h` | `include/pthread.h` | `880429757969928ad050ae38167b15d93e543105173236f84f6135db9c324eaa` |
| `include/sched.h` | `include/sched.h` | `4227c4e6cef70d77a07122aa4141b15859bdae8c36b831df477a37342b5569b0` |
| `include/semaphore.h` | `include/semaphore.h` | `249a5905a34eb55df224f975f0be4b1aeadf8021fe1661cc70c14633bccf5d80` |
| `include/signal.h` | `include/signal.h` | `fbb4f90817659d8271c6381d707b82c2bb422fa8a1aba4629110ad28165465f6` |
| `include/spawn.h` | `include/spawn.h` | `f27289ef1187631fd12894aa570cd9b3ee23d0f4d1ed3fc20cdefd61b920b9fe` |
| `include/stdbool.h` | `include/stdbool.h` | `5b2c8cd67f7483642f4d9fae98bcbd576170ea39c98bbc7a0404cca05a8b52d2` |
| `include/stddef.h` | `include/stddef.h` | `e7f2e0992e46712d1ac35b8e94fe49abe07fd00c5561d64ac8403c6d874a2f7a` |
| `include/stdint.h` | `include/stdint.h` | `62aa65bdd064eb1e50b2468980ef00f21b85e4efe25556b73529f8151670a289` |
| `include/stdlib.h` | `include/stdlib.h` | `a6d0ec0dd127f116a057172b05e203b728709772610c40eb3a83a3e5d23e7877` |
| `include/string.h` | `include/string.h` | `b3995e2a4537158d199e6d60836dc77880fa29f7b70398fdf468a657ba73837a` |
| `include/strings.h` | `include/strings.h` | `88665c7fdf2e2841462bffcbd512737eed8919286afb150e175efe843f88458b` |
| `include/sys/poll.h` | `include/sys/poll.h` | `1e19bd9bc8c47cee1e8a27cbd7ce0149869b3d28d404d6a2b08535eac2ea1f1b` |
| `include/sys/select.h` | `include/sys/select.h` | `01a81fdc362a5776377334a74d69b4514100bc5430ce5220a8c14a260ea11c53` |
| `include/sys/time.h` | `include/sys/time.h` | `d15ef0870763350665811710a082a8e1d8aa68ffa040783965d0f6e607a6ba92` |
| `include/sys/timex.h` | `include/sys/timex.h` | `63cce3cd3fcc2a79e9600dfd367b5d56f8ac167946d2dc28e1c02d4469492002` |
| `include/sys/types.h` | `include/sys/types.h` | `e15424ee1c660e8447e3154ca81d99a3d2e5cc37cc19250516ef6aceffb797c6` |
| `include/sys/uio.h` | `include/sys/uio.h` | `30fa5069708b43bc959ba6dae0a2e9f5b0fb169e9c7bb4351b478cce4d15b7e6` |
| `include/time.h` | `include/time.h` | `33296afc542b57f457934be736de4f9565d04503f2da58c7d278b17f22c88b37` |
| `include/unistd.h` | `include/unistd.h` | `3fb0b749fd76837826a3c16d7b2d639dcfe415a2668b24a98db0c816d43d7d02` |
| `include/arch/arch.h` | `arch/arm/include/arch.h` | `005c4454608905937c84311d75602ac739744e511c33ee9dfd2382c0636efb72` |
| `include/arch/armv7-m/irq.h` | `arch/arm/include/armv7-m/irq.h` | `04a9a368ebb2564bcb37fae7aeaba0bf24ef6b88894c5ecb8c95a26a7334e91a` |
| `include/arch/armv7-m/nvicpri.h` | `arch/arm/include/armv7-m/nvicpri.h` | `4ad7ffc99eb6ef0ba949d0fb114235d8a5b194478610e7ba5fbe1b96ea913c6b` |
| `include/arch/chip/chip.h` | `arch/arm/include/nrf52/chip.h` | `5689c8a0dfee1b9c807edee1e7e8c7d96573d1f69a724e37d4225f2da33aa4b0` |
| `include/arch/chip/irq.h` | `arch/arm/include/nrf52/irq.h` | `7c9f28a2cd85a945064649844d362cd7bc686c6e47c7d614a61de46dae50ddc2` |
| `include/arch/inttypes.h` | `arch/arm/include/inttypes.h` | `fd8de0f7c1cc494c42ec62f81a0d1e3685756d0caa5b66f268d6aa2fda299a5a` |
| `include/arch/irq.h` | `arch/arm/include/irq.h` | `f2fd6aed4ad8e0ea0671c69dae30bbf2f3f773a5d87ba417f3e93b6982b2a6a0` |
| `include/arch/limits.h` | `arch/arm/include/limits.h` | `9cf2c68d5ac3ed431ea536ca4e81b36c3306ee434f54d2391d10b94a86e69e16` |
| `include/arch/nrf52/nrf52_irq.h` | `arch/arm/include/nrf52/nrf52_irq.h` | `ecc9fbb5fdb08fe8f6984acf1330554e2f0f6f9dba01a3308ebdbd93a0ad5d40` |
| `include/arch/syscall.h` | `arch/arm/include/syscall.h` | `e1d61fe7ccd3a43392a475867b368eed865a946cabadd3ebe109a258c28c88a5` |
| `include/arch/types.h` | `arch/arm/include/types.h` | `f6cb435a3df48b4db866e3bb31ae18030e0cd26c7c065133fff5e5329b5cf855` |
| `LICENSE` | `LICENSE` | `908983297ee29399fd4b9579d690823d0a2a5abca38396ee551b85a863c05754` |
| `NOTICE` | `NOTICE` | `1f6ac972692c4f94fe4b904590654aeb795ff08efd9799427c2358fb568814bb` |

リポジトリでは改行が LF に正規化されることがある（ハッシュは取得したままのファイルのもの）。

NuttX のカーネルの実装（sched/・arch/arm/src/）は含まない。読み手（`host/fprobe/rtos/nuttx.py`・`nuttx_objects.py`）が従う
カーネルの振る舞いは、同じコミットの次のファイルを読んで確かめた（取り込んではいない）:

- `sched/init/nx_start.c`: g_pidhash・g_npidhash・g_running_tasks・g_readytorun・g_idletcb（"Idle_Task"）
- `sched/sched/sched.h`: PIDHASH(pid) = pid & (g_npidhash − 1)、this_task()
- `sched/clock/clock_sched_ticks.c`: g_system_ticks
- `sched/sched/sched_sleep.c`・`sched/signal/sig_timedwait.c`: 眠り（TSTATE_SLEEPING = TSTATE_WAIT_SIG で sigwaitmask は空）と
  合図の待ち（sigwaitmask）、waitdog
- `sched/semaphore/sem_wait.c`・`sched/mqueue/mq_rcvinternal.c`・`mq_sndinternal.c`・`sched/event/event_wait.c`: waitobj と
  待ちの列（優先度順）
- `sched/wdog/wd_start.c`: waitdog の expired（絶対のティック）と func
- `arch/arm/src/armv7-m/arm_exception.S`・`arm_initialstate.c`・`exc_return.h`: xcp.regs の配列（REG_R13 は regs +
  XCPTCONTEXT_SIZE、s16〜s31 の枠は CONFIG_ARCH_FPU なら常にある）と新しいタスクの文脈（PC は start、EXC_RETURN_THREAD）
- `arch/arm/src/common/arm_checkstack.c`・`arm_internal.h`: スタックの塗り STACK_COLOR 0xdeadbeef（stack_base_ptr から）

試験用の ELF（`../nuttx_shape.elf`）は、このヘッダの構造体と変数で「止めたときの様子」を置いたもの（`../nuttx_shape.c`）。
