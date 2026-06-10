import { readFile, readdir, stat } from "node:fs/promises";
import { join } from "node:path";

const assetDir = new URL("../dist/assets/", import.meta.url);
const maxKiB = 600;
const maxBytes = maxKiB * 1024;
const oversized = [];
const forbiddenRuntime = "echarts-for-react";
const forbidden = [];

for (const name of await readdir(assetDir)) {
  if (!name.endsWith(".js")) continue;
  const path = join(assetDir.pathname, name);
  const bytes = (await stat(path)).size;
  if (bytes > maxBytes) oversized.push(`${name} (${(bytes / 1024).toFixed(1)} KiB)`);
  if ((await readFile(path, "utf8")).includes(forbiddenRuntime)) {
    forbidden.push(name);
  }
}

if (oversized.length || forbidden.length) {
  if (oversized.length) {
    console.error(`Bundle budget exceeded (${maxKiB} KiB):\n${oversized.join("\n")}`);
  }
  if (forbidden.length) {
    console.error(`Forbidden ${forbiddenRuntime} runtime found in:\n${forbidden.join("\n")}`);
  }
  process.exit(1);
}

console.log(
  `Bundle budget OK: every JavaScript chunk is <= ${maxKiB} KiB and uses the native ECharts adapter.`,
);
