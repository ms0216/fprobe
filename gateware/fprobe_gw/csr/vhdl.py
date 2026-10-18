"""CSR 定義から VHDL を生成する: ブロックごとのレジスタバンク entity と、マップパッケージ。"""

from .. import protocol
from .model import Block, CsrMap, Field

__all__ = ["bank_entity_name", "render_bank", "render_map_package"]

HEADER = "-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。"


def bank_entity_name(block: Block) -> str:
    return f"csr_{block.name}"


def _type(width: int) -> str:
    return "std_logic" if width == 1 else f"std_logic_vector({width - 1} downto 0)"


def _literal(value: int, width: int) -> str:
    bits = format(value, f"0{width}b")
    return f"'{bits}'" if width == 1 else f'"{bits}"'


def _slice(name: str, f: Field) -> str:
    return f"{name}({f.lsb})" if f.width == 1 else f"{name}({f.lsb + f.width - 1} downto {f.lsb})"


def render_bank(block: Block) -> str:
    if block.external:
        raise ValueError(f"{block.name} は外部実装の窓")
    entity = bank_entity_name(block)
    bits = block.address_bits

    ports = []
    signals = []
    resets = []
    outputs = []
    strobes = []
    clears = []
    cases = []
    registers = {reg.name: reg for reg in block.registers}
    for reg in block.registers:
        index = reg.offset // 4
        reads, writes = [], []
        if reg.write_to:                                  # 書いた語は書き先のレジスタへ（読みはこのレジスタ）
            target = registers[reg.write_to]
            writes += [f"              {target.port_name(f)}_r <= {_slice('bus_wdata', f)};"
                       for f in target.fields if f.access in ("RW", "PULSE")]
        for f in reg.fields:
            port = reg.port_name(f)
            label = reg.name if reg.single else f"{reg.name}.{f.name}"
            if f.access == "RW":
                ports.append((f"{port} : out {_type(f.width)}", f"{label}（RW）"))
                signals.append(f"  signal {port}_r : {_type(f.width)} := {_literal(f.value, f.width)};")
                resets.append(f"        {port}_r <= {_literal(f.value, f.width)};")
                outputs.append(f"  {port} <= {port}_r;")
                reads.append(f"            {_slice('rdata', f)} <= {port}_r;")
                writes.append(f"              {port}_r <= {_slice('bus_wdata', f)};")
            elif f.access == "PULSE":
                ports.append((f"{port} : out {_type(f.width)}", f"{label}（PULSE: 1 を書いた次の周期に 1）"))
                signals.append(f"  signal {port}_r : {_type(f.width)} := {_literal(0, f.width)};")
                clears.append(f"      {port}_r <= {_literal(0, f.width)};")
                outputs.append(f"  {port} <= {port}_r;")
                writes.append(f"              {port}_r <= {_slice('bus_wdata', f)};")
            elif f.access == "RO":
                ports.append((f"{port} : in  {_type(f.width)}", f"{label}（RO）"))
                reads.append(f"            {_slice('rdata', f)} <= {port};")
            else:
                reads.append(f"            {_slice('rdata', f)} <= {_literal(f.value, f.width)};")
        if reg.read_strobe:
            ports.append((f"{reg.name}_read : out std_logic", f"{reg.name} を読んだ周期に 1"))
            strobes.append(f"  {reg.name}_read <= '1' when start and bus_we = '0' and index = {index} else '0';")
        cases.append(f"          when {index} =>   -- {reg.name}")
        cases += reads
        if writes:
            cases.append("            if bus_we = '1' then")
            cases += writes
            cases.append("            end if;")

    index_expr = f"to_integer(bus_addr({bits - 1} downto 0))" if bits else "0"
    decls = [("bus_rdata : out csr_word_t", "")] + ports
    port_block = [f"    {decl}{';' if i + 1 < len(decls) else ''}" + (f"   -- {note}" if note else "")
                  for i, (decl, note) in enumerate(decls)]
    lines = [
        HEADER,
        f"-- CSR ブロック {block.name}（0x{block.base:08X}, {block.size} バイト）: {block.doc}",
        "-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、",
        "-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rdata・bus_err は 0。",
        "library ieee;",
        "use ieee.std_logic_1164.all;",
        "use ieee.numeric_std.all;",
        "use work.csr_bus_pkg.all;",
        "",
        f"entity {entity} is",
        "  port (",
        "    clk       : in  std_logic;",
        "    rst       : in  std_logic;",
        "    bus_req   : in  std_logic;",
        "    bus_we    : in  std_logic;",
        "    bus_addr  : in  csr_addr_t;",
        "    bus_wdata : in  csr_word_t;",
        "    bus_ack   : out std_logic;",
        "    bus_err   : out std_logic;",
        *port_block,
        "  );",
        "end entity;",
        "",
        f"architecture rtl of {entity} is",
        "  signal ack   : std_logic := '0';",
        "  signal err   : std_logic := '0';",
        "  signal rdata : csr_word_t := (others => '0');",
        "  signal start : boolean;",
        f"  signal index : natural range 0 to {max(2 ** bits - 1, 0)};",
        *signals,
        "begin",
        "  start <= bus_req = '1' and ack = '0';",
        f"  index <= {index_expr};",
        *strobes,
        "",
        "  process (clk)",
        "  begin",
        "    if rising_edge(clk) then",
        "      ack   <= '0';",
        "      err   <= '0';",
        "      rdata <= (others => '0');                 -- 応答の周期以外は 0（csr_decoder が窓の応答の論理和を取る）",
        *clears,
        "      if rst = '1' then",
        *(resets or ["        null;"]),
        "      elsif start then",
        "        ack   <= '1';",
        "        case index is",
        *cases,
        "          when others =>",
        "            err <= '1';",
        "        end case;",
        "      end if;",
        "    end if;",
        "  end process;",
        "",
        "  bus_ack   <= ack;",
        "  bus_err   <= err;",
        "  bus_rdata <= rdata;",
        *outputs,
        "end architecture;",
        "",
    ]
    return "\n".join(lines)


def render_map_package(csr_map: CsrMap) -> str:
    blocks = csr_map.blocks
    windows = [f"    CSR_WIN_{b.name.upper()} => (base => {b.base // 4}, size => {b.size // 4})"
               for b in blocks]
    return "\n".join([
        HEADER,
        "-- 生成元: gateware/fprobe_gw/csr/fprobe_map.py, gateware/fprobe_gw/protocol.py",
        "library ieee;",
        "use ieee.std_logic_1164.all;",
        "use work.csr_bus_pkg.all;",
        "",
        "package csr_map_pkg is",
        "",
        "  -- CONTROL プロトコル",
        f"  constant CTRL_PROTOCOL_VERSION : natural := {protocol.PROTOCOL_VERSION};",
        f'  constant CTRL_OP_READ          : std_logic_vector(7 downto 0) := x"{protocol.OP_READ:02X}";',
        f'  constant CTRL_OP_WRITE         : std_logic_vector(7 downto 0) := x"{protocol.OP_WRITE:02X}";',
        f'  constant CTRL_OP_EXCHANGE      : std_logic_vector(7 downto 0) := x"{protocol.OP_EXCHANGE:02X}";',
        f'  constant CTRL_STATUS_OK         : std_logic_vector(7 downto 0) := x"{protocol.STATUS_OK:02X}";',
        f'  constant CTRL_STATUS_BAD_OP     : std_logic_vector(7 downto 0) := x"{protocol.STATUS_BAD_OP:02X}";',
        f'  constant CTRL_STATUS_BAD_LENGTH : std_logic_vector(7 downto 0) := x"{protocol.STATUS_BAD_LENGTH:02X}";',
        f'  constant CTRL_STATUS_BUS_ERROR  : std_logic_vector(7 downto 0) := x"{protocol.STATUS_BUS_ERROR:02X}";',
        f"  constant CTRL_REQUEST_HEADER_BYTES  : natural := {protocol.REQUEST_HEADER_BYTES};",
        f"  constant CTRL_RESPONSE_TRAILER_BYTES : natural := {protocol.RESPONSE_TRAILER_BYTES};",
        "",
        "  -- CSR の窓（csr_decoder の WINDOWS。単位はワード）",
        *[f"  constant CSR_WIN_{b.name.upper()} : natural := {i};" for i, b in enumerate(blocks)],
        f"  constant CSR_WINDOWS : csr_windows_t(0 to {len(blocks) - 1}) := (",
        ",\n".join(windows),
        "  );",
        "",
        "end package;",
        "",
    ])
