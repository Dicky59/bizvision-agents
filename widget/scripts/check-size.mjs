// Fails the build when the gzipped bundle exceeds the budget (spec: < 40 KB gzipped).
import { readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";

const BUDGET = 40 * 1024;
const size = gzipSync(readFileSync(new URL("../dist/widget.js", import.meta.url)), {
  level: 9,
}).length;
console.log(`widget.js gzipped: ${(size / 1024).toFixed(1)} KB (budget ${BUDGET / 1024} KB)`);
if (size > BUDGET) {
  console.error("Bundle size budget exceeded");
  process.exit(1);
}
