/* GCC が「extern の宣言 → 定義」を、型を持つ宣言の DIE と、DW_AT_specification で宣言を指す定義の DIE に分けて出す形
 * （Zephyr の _kernel と同じ）。fprobe の DWARF の試験用（host/tests/test_dwarf_info.py）。
 * 作り直し: arm-zephyr-eabi-gcc -mcpu=cortex-m4 -mthumb -g -O0 -nostdlib -Wl,-e,main -o gcc_spec.elf gcc_spec.c */
struct cpu { int id; struct cpu *self; };
struct kernel { struct cpu cpus[2]; unsigned ticks; };

extern struct kernel g_kernel;
struct kernel g_kernel = { .cpus = { { 7, 0 }, { 8, 0 } }, .ticks = 42 };

int main(void) { return g_kernel.cpus[0].id; }
