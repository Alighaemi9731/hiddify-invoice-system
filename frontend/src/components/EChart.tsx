import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import type { EChartsType } from "echarts/core";
import { BarChart, PieChart } from "echarts/charts";
import { GridComponent, TooltipComponent, LegendComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

// Register only what the dashboard charts use — pulls in a fraction of full `echarts`,
// roughly halving the (lazy) dashboard chunk.
echarts.use([
  BarChart, PieChart,
  GridComponent, TooltipComponent, LegendComponent,
  CanvasRenderer,
]);

export default function EChart({
  option, height = 300, ariaLabel,
}: { option: any; height?: number; ariaLabel?: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<EChartsType | null>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const chart = echarts.init(container, undefined, { renderer: "canvas" });
    chartRef.current = chart;
    const resizeObserver = new ResizeObserver(() => chart.resize());
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    chartRef.current?.setOption(option, { notMerge: true, lazyUpdate: true });
  }, [option]);

  // The canvas is opaque to screen readers, so expose a text label describing the chart.
  return (
    <div
      ref={containerRef}
      role="img"
      aria-label={ariaLabel || "نمودار"}
      style={{ height, width: "100%" }}
    />
  );
}
