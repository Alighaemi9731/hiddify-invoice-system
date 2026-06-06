import ReactEChartsCore from "echarts-for-react/lib/core";
import * as echarts from "echarts/core";
import { BarChart, PieChart, LineChart } from "echarts/charts";
import {
  GridComponent, TooltipComponent, LegendComponent, TitleComponent, DatasetComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

// Register only what the dashboard charts use — pulls in a fraction of full `echarts`,
// roughly halving the (lazy) dashboard chunk.
echarts.use([
  BarChart, PieChart, LineChart,
  GridComponent, TooltipComponent, LegendComponent, TitleComponent, DatasetComponent,
  CanvasRenderer,
]);

export default function EChart({
  option, height = 300, ariaLabel,
}: { option: any; height?: number; ariaLabel?: string }) {
  // The canvas is opaque to screen readers, so expose a text label describing the chart.
  return (
    <div role="img" aria-label={ariaLabel || "نمودار"}>
      <ReactEChartsCore
        echarts={echarts}
        option={option}
        notMerge
        lazyUpdate
        style={{ height, width: "100%" }}
        opts={{ renderer: "canvas" }}
      />
    </div>
  );
}
