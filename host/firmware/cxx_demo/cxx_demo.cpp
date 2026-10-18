// fprobe の C++ の DWARF（名前空間・クラスのメンバー関数・多重定義・テンプレート）の試験用ファームウェア。
// dwarf_demo と同じ配置（SRAM に読み込んで実行できる。リンカスクリプトは dwarf_demo/link.ld）。
// 作り方: python host/firmware/cxx_demo/build.py（zig c++。-O0、DWARF 5。試験のためにリポジトリに置く）。
#include <stdint.h>

extern "C" uint32_t __stack_top;

volatile int sink;

namespace motor {

struct Pid {
    int kp;
    int step(int error) {
        return kp * error;              // メンバー関数（this は隠れた引数）
    }
};

int scale(int v) {
    return v * 2;                       // 多重定義 1
}

int scale(int v, int k) {
    return v * k;                       // 多重定義 2
}

template <typename T> T twice(T v) {
    return v + v;
}

}  // namespace motor

motor::Pid pid = {3};

extern "C" __attribute__((noreturn)) void reset_handler(void) {
    for (;;) {
        sink = pid.step(sink) + motor::scale(sink) + motor::scale(sink, 3) + motor::twice<int>(sink);
    }
}

extern "C" __attribute__((noreturn)) void default_handler(void) {
    for (;;) {
    }
}

__attribute__((section(".vectors"), used)) const void *const vectors[16] = {
    &__stack_top, (void *)reset_handler, (void *)default_handler, (void *)default_handler,
    (void *)default_handler, (void *)default_handler, (void *)default_handler, 0, 0, 0, 0,
    (void *)default_handler, (void *)default_handler, 0, (void *)default_handler, (void *)default_handler,
};
