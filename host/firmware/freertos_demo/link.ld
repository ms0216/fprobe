/* freertos_demo: すべて SRAM（STM32F401: 0x20000000、96 KB）。先頭 64 KB を使い、スタック（MSP）はその末尾。
   FreeRTOS のヒープ（heap_4 の ucHeap、20 KB）は .bss。タスクのスタックと TCB はヒープから取る。 */
MEMORY
{
    RAM (rwx) : ORIGIN = 0x20000000, LENGTH = 64K
}

SECTIONS
{
    .vectors : { KEEP(*(.vectors)) } > RAM
    .text : { *(.text*) *(.rodata*) } > RAM
    .data : { *(.data*) } > RAM
    .bss (NOLOAD) : {
        . = ALIGN(4);
        __bss_start = .;
        *(.bss*) *(COMMON)
        . = ALIGN(4);
        __bss_end = .;
    } > RAM
    __stack_top = ORIGIN(RAM) + LENGTH(RAM);
    /DISCARD/ : { *(.ARM.exidx*) *(.comment) }
}
