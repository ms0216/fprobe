/* stack_demo: STM32F401（Flash 512 KB・SRAM 96 KB）の STM32CubeIDE の形に倣う。スタックは SRAM の末尾から下へ。 */
MEMORY
{
    FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 512K
    RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 96K
}

_estack = ORIGIN(RAM) + LENGTH(RAM);
_Min_Heap_Size = 0x200;
_Min_Stack_Size = 0x400;

SECTIONS
{
    .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
    .text : { *(.text*) } > FLASH
    .rodata : { *(.rodata*) } > FLASH
    _sidata = LOADADDR(.data);
    .data : {
        _sdata = .;
        *(.data*)
        _edata = .;
    } > RAM AT > FLASH
    .bss (NOLOAD) : {
        _sbss = .;
        *(.bss*) *(COMMON)
        _ebss = .;
    } > RAM
    ._user_heap_stack (NOLOAD) : {
        . = ALIGN(8);
        end = .;
        . = . + _Min_Heap_Size;
        . = . + _Min_Stack_Size;
        . = ALIGN(8);
    } > RAM
    /DISCARD/ : { *(.ARM.exidx*) *(.comment) }
}
