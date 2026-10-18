/* etm_demo: ETM の命令トレースの解読（fprobe.trace）の試験用の小さなプログラム（Cortex-M33、Thumb-2）。
 * 実行はしない。試験（host/tests/test_trace_reconstruct.py）は、この ELF の上を走ったことにしたトレースを手で組み、
 * 解読器が命令の流れを再構成できるかを確かめる。分岐の種類をひととおり含む:
 *   直接の呼び出し（bl）・条件分岐（bne）・間接の呼び出し（blx r1）・表による分岐（tbb）・戻り（bx lr・pop {pc}）・
 *   IT ブロックの中の条件付きの戻り（bxeq lr）・無条件分岐（b）。irq_handler は例外の入口の試験用。
 * 作り直し: python host/firmware/etm_demo/build.py
 */
    .syntax unified
    .cpu cortex-m33
    .thumb

    .text
    .global reset_handler
    .type reset_handler, %function
    .thumb_func
reset_handler:
    movs    r0, #2
loop:
    bl      leaf
    subs    r0, r0, #1
    bne     loop
    ldr     r1, =indirect
    blx     r1
    movs    r0, #1
    bl      dispatch
    movs    r0, #0
    bl      cond_ret
    b       reset_handler
    .ltorg
    .size reset_handler, . - reset_handler

    .global leaf
    .type leaf, %function
    .thumb_func
leaf:
    adds    r2, r2, #1
    bx      lr
    .size leaf, . - leaf

    .global indirect
    .type indirect, %function
    .thumb_func
indirect:
    nop
    bx      lr
    .size indirect, . - indirect

    .global dispatch
    .type dispatch, %function
    .thumb_func
dispatch:
    cmp     r0, #2
    bhi     9f
    tbb     [pc, r0]
1:  .byte   (c0 - 1b) / 2, (c1 - 1b) / 2, (c2 - 1b) / 2
    .align  1
c0: movs    r3, #10
    bx      lr
c1: movs    r3, #11
    bx      lr
c2: movs    r3, #12
9:  bx      lr
    .size dispatch, . - dispatch

    .global cond_ret
    .type cond_ret, %function
    .thumb_func
cond_ret:
    cmp     r0, #0
    it      eq
    bxeq    lr
    movs    r0, #0
    bx      lr
    .size cond_ret, . - cond_ret

    .global irq_handler
    .type irq_handler, %function
    .thumb_func
irq_handler:
    push    {r4, lr}
    adds    r4, r4, #1
    pop     {r4, pc}
    .size irq_handler, . - irq_handler
