/* formats_demo: すべて SRAM（STM32F401: 0x20000000、96 KB）。先頭 32 KB を使い、スタックはその末尾。
   .defmt は defmt.x と同じく読み込まない区画（INFO）で番地 1 から（番号 0 は予約）。 */
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
    .defmt 1 (INFO) : {
        . = 1;
        KEEP(*(.defmt.prim.*))
        KEEP(*(.defmt.trace.*)) KEEP(*(.defmt.debug.*)) KEEP(*(.defmt.info.*)) KEEP(*(.defmt.warn.*))
        KEEP(*(.defmt.error.*))
        KEEP(*(.defmt.*))
    }
    /DISCARD/ : { *(.ARM.exidx*) *(.comment) }
}
