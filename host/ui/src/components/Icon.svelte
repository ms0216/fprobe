<script lang="ts">
  // ツールバーのアイコン（16 × 16 の線画。VS Code のデバッグのアイコンの形に倣う）
  import type { IconName } from "../lib/commands";

  let { name, size = 16 }: { name: IconName; size?: number } = $props();

  const PATHS: Record<IconName, string> = {
    open: "M2 4h4l1.5 1.5H14v7.5H2z",
    download: "M8 2v8M4.5 6.5 8 10l3.5-3.5M3 13h10",
    reset: "M3.5 8a4.5 4.5 0 1 0 1.3-3.2M3.5 2.5v3h3",
    continue: "M4 3v10M7 3l7 5-7 5z",
    halt: "M5 3v10M11 3v10",
    stepOver: "M2.5 9a5.5 5.5 0 0 1 11 0M13.5 5v4h-4M8 12.5a1 1 0 1 0 0 .01",
    stepInto: "M8 2v8M4.5 6.5 8 10l3.5-3.5M8 13.5a1 1 0 1 0 0 .01",
    stepOut: "M8 11V3M4.5 6.5 8 3l3.5 3.5M8 13.5a1 1 0 1 0 0 .01",
    stepInstruction: "M3 3h10M3 6h6M3 9h6M3 12h10M11 5.5v5M9.5 9 11 10.5 12.5 9",
    runToCursor: "M2 8h8M7 5l3 3-3 3M13 3v10",
    breakpoint: "M8 4.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 1 0 0-7",
    connect: "M6 2v3M10 2v3M4 5h8v3a4 4 0 0 1-8 0zM8 12v2",
    disconnect: "M3 3l10 10M6 2v3M10 2v3M4 5h8v3a4 4 0 0 1-8 0zM8 12v2",
    demo: "M3 2.5h7l3 3v8H3zM10 2.5v3h3M5.5 9.5l2 1.5 3-4",
    chip: "M4.5 4.5h7v7h-7zM6.5 2v2.5M9.5 2v2.5M6.5 11.5V14M9.5 11.5V14M2 6.5h2.5M2 9.5h2.5M11.5 6.5H14M11.5 9.5H14",
    capture: "M1.5 11h2.5V5h3v6h3V5h2.5v6H14",
  };
  const filled: Partial<Record<IconName, boolean>> = { continue: true, breakpoint: true };
</script>

<svg width={size} height={size} viewBox="0 0 16 16" aria-hidden="true" class:filled={filled[name]}>
  <path d={PATHS[name]} />
</svg>

<style>
  svg { display: block; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linecap: round; stroke-linejoin: round; }
  svg.filled path { fill: currentColor; }
</style>
