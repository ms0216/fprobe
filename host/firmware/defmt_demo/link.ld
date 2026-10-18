/* defmt.x と同じ: .defmt は読み込まない区画（INFO）で、番地 1 から（0 は予約）。 */
SECTIONS {
  .text 0x08000000 : { *(.text*) *(.rodata*) }
  .defmt 1 (INFO) : {
    . = 1;
    *(.defmt.prim.*)
    *(.defmt.trace.*) *(.defmt.debug.*) *(.defmt.info.*) *(.defmt.warn.*) *(.defmt.error.*)
    *(.defmt.*)
  }
}
