/* defmt の表（.defmt の区画のシンボル）を持つ試験用の ELF（Rust の defmt が作る表と同じ形を C で作る）。
 * 名前が JSON のシンボルを 1 バイトずつ .defmt.<tag>.<n> に置き、リンカスクリプトで番地 1 からの .defmt (INFO) に集める。
 * ログの場所（DWARF）は Rust と同じく DEFMT_LOG_STATEMENT という名前の変数。 */

#define J(tag, data, n) "{\"package\":\"demo\",\"tag\":\"defmt_" tag "\",\"data\":\"" data "\",\"disambiguator\":\"" #n "\",\"crate_name\":\"demo\"}"
#define ENTRY(sec, name, json) \
    __attribute__((section(sec), used)) static const unsigned char name __asm__(json) = 0;

__attribute__((used)) const unsigned char defmt_version __asm__("_defmt_version_ = 4") = 0;
__attribute__((used)) const unsigned char defmt_encoding __asm__("_defmt_encoding_ = rzcobs") = 0;

ENTRY(".defmt.prim.1", prim_u8, J("prim", "{=u8}", 1))
ENTRY(".defmt.derived.1", derived_foo, J("derived", "Foo {{ x: {=u8} }}", 2))
ENTRY(".defmt.str.1", interned, J("str", "interned", 3))
ENTRY(".defmt.timestamp.1", timestamp, J("timestamp", "{=u32:us}", 4))

void log_points(void) {
    { ENTRY(".defmt.info.1", DEFMT_LOG_STATEMENT, J("info", "Hello {=str}, count={=u8}", 10)) }
    { ENTRY(".defmt.warn.1", DEFMT_LOG_STATEMENT, J("warn", "temperature {=i16} is high", 11)) }
    { ENTRY(".defmt.error.1", DEFMT_LOG_STATEMENT, J("error", "fault at {=u32:#010x}", 12)) }
    { ENTRY(".defmt.debug.1", DEFMT_LOG_STATEMENT, J("debug", "state {=?}", 13)) }
    { ENTRY(".defmt.println.1", DEFMT_LOG_STATEMENT, J("println", "plain {=f32}", 14)) }
}

void reset_handler(void) {
    log_points();
    for (;;) {
    }
}
