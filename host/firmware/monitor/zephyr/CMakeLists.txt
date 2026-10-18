# fprobe のモニタ（ベクタ 12 のハンドラと箱。この上のディレクトリの fprobe_monitor.c）を Zephyr のビルドに加える。
# 足し方: -DEXTRA_ZEPHYR_MODULES=<このディレクトリ>（ZMK は -DZMK_EXTRA_MODULES）と -DEXTRA_CONF_FILE=<ここ>/fprobe_monitor.conf。
if(CONFIG_FPROBE_MONITOR)
  set(FPROBE_MONITOR_DIR ${CMAKE_CURRENT_LIST_DIR}/..)
  zephyr_library_named(fprobe_monitor)
  zephyr_include_directories(${FPROBE_MONITOR_DIR})
  zephyr_library_sources(${FPROBE_MONITOR_DIR}/fprobe_monitor.c src/fprobe_monitor_zephyr.c)
  # Zephyr のベクタ表の 12 番は z_arm_debug_monitor（fault_s.S の弱い定義）を指す: 強い定義で置き換える
  zephyr_library_compile_definitions(FPROBE_MONITOR_HANDLER=z_arm_debug_monitor
                                     FPROBE_MONITOR_STACK_SIZE=${CONFIG_FPROBE_MONITOR_STACK_SIZE})
endif()
