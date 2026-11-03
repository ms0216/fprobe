# kbuild の外部モジュールの記述。PYNQ では直接 make せず ./build.sh で作る（ビルドの木の生成物と gcc の版の違い。build.sh の頭）。
# 読み込みは sudo insmod fprobe_ringmap.ko、外すのは sudo rmmod fprobe_ringmap。起動時の読み込みの据え付けは
# python -m fprobe_gw pynq ringmap（外すのは unringmap）
obj-m := fprobe_ringmap.o
ccflags-y := -Werror
# build.sh が渡すソースの sha256（モジュールの modinfo の fprobe_src になる）
ifdef FPROBE_RINGMAP_SRC
ccflags-y += -DFPROBE_RINGMAP_SRC=$(FPROBE_RINGMAP_SRC)
endif
