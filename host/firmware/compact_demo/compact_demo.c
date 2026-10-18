/* Compact logging の試験用の ELF。
 * - 番地のログ（Tracealyzer の compact print・fprobe の compact と同じ考え）: 書式の文字列は .rodata に置き、
 *   ターゲットは文字列の番地と引数の語だけを送る。
 * - Memfault の compact log: 書式は読み込まない区画 log_fmt に "引数の数;ファイル;行;書式" で置き、ターゲットは
 *   区画の先頭からの位置と引数を CBOR で送る（先頭に 8 バイトの見出し "LOGf"・版 1）。 */

typedef void (*sink_t)(const char *fmt, unsigned a, unsigned b);
volatile sink_t sink;

const char fmt_temp[] = "temp=%d.%02u C";
const char fmt_state[] = "motor %s -> %s";
const char fmt_wide[] = "big %lld %f";
const char name_idle[] = "idle";
const char name_run[] = "run";

struct log_fmt_hdr { unsigned magic; unsigned char version, reserved[3]; };
__attribute__((section(".log_fmt_hdr"), used)) const struct log_fmt_hdr memfault_log_fmt_hdr = {0x66474F4C, 1, {0, 0, 0}};
__attribute__((section("log_fmt"), used)) static const char mf_boot[] = "1;app/main.c;21;boot count %d";
__attribute__((section("log_fmt"), used)) static const char mf_sensor[] = "3;app/sensor.c;87;%s: %d (%f)";
__attribute__((section("log_fmt"), used)) static const char mf_none[] = "0;app/main.c;30;ready";

void reset_handler(void) {
    if (sink) {
        sink(fmt_temp, 21, 5);
        sink(fmt_state, (unsigned)name_idle, (unsigned)name_run);
        sink(fmt_wide, 0, 0);
    }
    for (;;) {
    }
}
