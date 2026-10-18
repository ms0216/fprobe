# F2（一部）逆アセンブル・SVD・メモリの表示 実装計画

仕様: [2026-10-17-f2-disasm-svd-design.md](../specs/2026-10-17-f2-disasm-svd-design.md)。各段は試験を先に書き、
`.venv/Scripts/python -m pytest host -q -p no:cacheprovider` が通ってから commit する。

1. **試験用の ELF**: `host/firmware/disasm_demo/`（Cortex-M0+、リテラルプールあり）。`build.py` で作り、ELF を置く。
2. **memview.py**: `MemoryReader` / `RegisterAccess` の約束、`BytesMemory`、`TargetMemory`（pyOCD）、`ElfImage`（区画と
   `CodeMap`）、`hexdump` / `format_hexdump` / `diff`。試験 `test_memview.py`。
3. **disasm.py**: `Instruction` / `Listing` / `Disassembler`（`range`・`function`・`around`・`from_elf`）、リテラルの推定、
   混在表示 `Listing.text`。`pyproject.toml` の `debug` に `capstone>=5` を足す。試験 `test_disasm.py`。
4. **SVD の同梱**: `host/fprobe/data/svd/STM32F401.svd`（ST、Apache-2.0）、`README.md`（出典・版・ライセンス・置き場所の決まり）、
   `LICENSE-Apache-2.0.txt`。`pyproject.toml` に package-data。
5. **svd.py**: 読み込み（継承・derivedFrom・dim・クラスタ・列挙・bitRange）、`decode`/`encode`、`find`、`read_register` /
   `write_fields`、`find_svd` / `load_device`。試験 `test_svd.py`（手書き `host/tests/data/mini.svd` と STM32F401）。
6. 全試験を通し、仕様の「組み込みの提案」を実装に合わせて見直す。
