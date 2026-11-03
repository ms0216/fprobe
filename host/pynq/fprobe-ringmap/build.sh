#!/bin/bash
# PYNQ の上で fprobe_ringmap.ko を作る（root は要らない）。使い方: ./build.sh [作業の置き場所（既定 /tmp/fprobe-ringmap-build）]
# 終わったら作業の置き場所は消してよい（約 110 MB。.ko は $WORK/mod にある）。
#
# PYNQ の /lib/modules/$(uname -r)/build（Yocto の kernel-devsrc）は、そのままでは外部のモジュールが作れない（2026-11-02 に確かめた）:
#   - 生成物（arch/arm/include/generated・scripts の fixdep と modpost）が無い。
#   - 最上位の Kbuild と、その生成物の元（kernel/bounds.c・arch/arm/kernel/asm-offsets.c）が無い。生成物
#     （include/generated/bounds.h・asm-offsets.h・timeconst.h）はカーネルのビルドのものが入っている。
#     このまま modules_prepare すると、prepare0 の「$(build)=. prepare」が最上位の Makefile を自分に読み直して
#     際限なく入れ子になる。
#   - カーネルのビルドは GCC 12.2、PYNQ の gcc は 11.2（GCC 12 だけの指定は cc-compat.sh が外す）。
# /lib/modules の中は変えず、次をする:
#   1. ビルドの木を作業の置き場所へ写す。最上位の Kbuild が無ければ、何もしない prepare だけの Kbuild を置く
#      （生成物はカーネルのビルドのものをそのまま使う）。
#   2. PATH の先頭の gcc を cc-compat.sh にして modules_prepare（arch の生成物と、fixdep・modpost 等の道具を作る）。
#      CC_VERSION_TEXT は記録（include/config/auto.conf.cmd）の文字列を渡すが、それでも構成は作り直された（SYNC。
#      理由は調べていない）。作り直された構成は GCC_VERSION・AS_VERSION・LD_VERSION・CC_CAN_LINK(_STATIC)・
#      CC_HAS_AUTO_VAR_INIT_PATTERN・GCC_PLUGINS の注釈だけが違った（2026-11-02）。
#   3. 構成のファイル（.config・include/config/auto.conf(.cmd)・include/generated/autoconf.h・rustc_cfg）と版の名前
#      （include/config/kernel.release・include/generated/utsrelease.h。modules_prepare が git の無い木で書き直す。
#      モジュールの vermagic になる）を元に戻す。外部のモジュールのビルド（M=）は構成を作り直さないので、
#      モジュールはカーネルと同じ構成・同じ生成物（asm-offsets.h 等）で作られる。
#   4. モジュールを作る。ソース 4 つ（fprobe_ringmap.c・Makefile・build.sh・cc-compat.sh）の sha256（16 進 64 文字 + 改行を
#      この順に 4 行。sha256sum の名前の欄は環境で「  名前」「 *名前」と違うので使わない）の sha256 を FPROBE_RINGMAP_SRC
#      で渡し、モジュールの modinfo の fprobe_src に入れる（据え付けの道具 fprobe_gw pynq ringmap が、据え付けてあるものと
#      同じソースかを読む。同じ計算は fprobe_gw.pynq_setup.ringmap_source_digest）。
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${1:-/tmp/fprobe-ringmap-build}"
SRC="/lib/modules/$(uname -r)/build"
VERTEXT="$(sed -n 's/^ifneq "$(CC_VERSION_TEXT)" "\(.*\)"$/\1/p' "$SRC/include/config/auto.conf.cmd")"
[ -n "$VERTEXT" ] || { echo "build.sh: $SRC/include/config/auto.conf.cmd に CC_VERSION_TEXT が無い" >&2; exit 1; }

mkdir -p "$WORK/bin"
[ -d "$WORK/kbuild" ] || cp -a "$SRC/." "$WORK/kbuild"
[ -e "$WORK/kbuild/Kbuild" ] || printf '# build.sh が置いた（元の木に無い。生成物はカーネルのビルドのものを使う）\nprepare: ;\n' > "$WORK/kbuild/Kbuild"
install -m 755 "$HERE/cc-compat.sh" "$WORK/bin/gcc"
export PATH="$WORK/bin:$PATH"
KMAKE=(make -C "$WORK/kbuild" "CC_VERSION_TEXT=$VERTEXT")

"${KMAKE[@]}" modules_prepare
for f in .config include/config/auto.conf include/config/auto.conf.cmd include/generated/autoconf.h include/generated/rustc_cfg \
	include/config/kernel.release include/generated/utsrelease.h; do
	if [ -e "$SRC/$f" ]; then
		cp -p "$SRC/$f" "$WORK/kbuild/$f"
	fi
done
mkdir -p "$WORK/mod"
cp "$HERE/fprobe_ringmap.c" "$HERE/Makefile" "$WORK/mod/"
SRC_SHA="$(cd "$HERE" && sha256sum fprobe_ringmap.c Makefile build.sh cc-compat.sh | cut -c1-64 | sha256sum | cut -c1-64)"
"${KMAKE[@]}" M="$WORK/mod" "FPROBE_RINGMAP_SRC=$SRC_SHA" modules
echo "build.sh: $WORK/mod/fprobe_ringmap.ko（fprobe_src $SRC_SHA）"
