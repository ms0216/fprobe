/* sram_demo: すべて SRAM（STM32F401: 0x20000000、96 KB）。先頭 32 KB を使い、スタックはその末尾。 */
MEMORY
{
    RAM (rwx) : ORIGIN = 0x20000000, LENGTH = 32K
}

SECTIONS
{
    .vectors : { KEEP(*(.vectors)) } > RAM
    .text : { *(.text*) *(.rodata*) } > RAM
    .data : { *(.data*) } > RAM
    .bss (NOLOAD) : {
        __bss_start = .;
        *(.bss*) *(COMMON)
        __bss_end = .;
    } > RAM
    __stack_top = ORIGIN(RAM) + LENGTH(RAM);
    /DISCARD/ : { *(.ARM.exidx*) *(.comment) }
}
