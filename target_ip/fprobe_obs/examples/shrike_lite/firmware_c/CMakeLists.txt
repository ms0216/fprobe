# SPDX-License-Identifier: Apache-2.0
# Shrike-Lite の C のデモ（RP2040、Zephyr）。FPGA の回路（FPGA_IMAGE = fpga_mcu.bin）を埋め込み、起動時に SPI で送る。
# ビルドは build.py --firmware から（ZephyrToolchain.build。-DFPGA_IMAGE=<fpga_mcu.bin の絶対パス>）。
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(shrike_c_demo)

if(NOT DEFINED FPGA_IMAGE)
  message(FATAL_ERROR "FPGA_IMAGE（FPGA の回路 fpga_mcu.bin）を -DFPGA_IMAGE= で渡す")
endif()

target_sources(app PRIVATE src/main.c src/fpga_load.c src/accel.c src/scenario.c)
generate_inc_file_for_target(app ${FPGA_IMAGE} ${ZEPHYR_BINARY_DIR}/include/generated/fpga_image.inc)
