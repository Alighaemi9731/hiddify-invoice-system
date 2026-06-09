import { readdir, stat } from "node:fs/promises";
import { join } from "node:path";

const assetDir = new URL("../dist/assets/", import.meta.url);
const maxBytes = 500 * 1024;
const oversized = [];

for (const name of await readdir(assetDir)) {
  if (!name.endsWith(".js")) continue;
  const bytes = (await stat(join(assetDir.pathname, name))).size;
  if (bytes > maxBytes) oversized.push(`${name} (${(bytes / 1024).toFixed(1)} KiB)`);
}

if (oversized.length) {
  console.error(`Bundle budget exceeded (500 KiB):\n${oversized.join("\n")}`);
  process.exit(1);
}

console.log("Bundle budget OK: every JavaScript chunk is <= 500 KiB.");
