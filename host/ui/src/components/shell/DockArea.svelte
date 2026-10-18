<script lang="ts">
  // ドッキングの場所（dockview）。パネルは lib/dock.ts が mount する
  import "../../vendor/dockview.css";
  import { Dock } from "../../lib/dock";
  import { PANELS } from "../panels/registry";

  let { context, ready }: { context: Map<any, any>; ready: (dock: Dock) => void } = $props();
  let box: HTMLDivElement;
  let dock: Dock | null = null;
  const scheme = matchMedia("(prefers-color-scheme: dark)");
  const recolor = () => dock?.setTheme(scheme.matches);

  $effect(() => {
    dock = new Dock(box, PANELS, context, scheme.matches);
    ready(dock);
    scheme.addEventListener("change", recolor);
    return () => {
      scheme.removeEventListener("change", recolor);
      dock?.dispose();
      dock = null;
    };
  });
</script>

<div class="fprobe-dock" bind:this={box}></div>

<style>
  .fprobe-dock { width: 100%; height: 100%; min-height: 0; }
</style>
