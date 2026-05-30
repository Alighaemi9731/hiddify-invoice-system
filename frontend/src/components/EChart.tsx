import ReactECharts from "echarts-for-react";

export default function EChart({ option, height = 300 }: { option: any; height?: number }) {
  return (
    <ReactECharts
      option={option}
      notMerge
      lazyUpdate
      style={{ height, width: "100%" }}
      opts={{ renderer: "canvas" }}
    />
  );
}
