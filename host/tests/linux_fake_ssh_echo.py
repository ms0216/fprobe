# 試験の偽の ssh: 受けた命令（argv の最後）をそのまま stdout に返す
import sys

sys.stdout.write(sys.argv[-1])
