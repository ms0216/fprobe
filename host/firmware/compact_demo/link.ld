SECTIONS {
  .text 0x08000000 : { *(.text*) }
  .rodata : { *(.rodata*) }
  .bss 0x20000000 : { *(.bss*) *(COMMON) }
  /* Memfault の例と同じ: 読み込まない区画（INFO）。見出しが先頭。 */
  log_fmt 0xF0000000 (INFO) : { KEEP(*(.log_fmt_hdr)) KEEP(*(log_fmt)) }
}
