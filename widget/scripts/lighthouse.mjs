// Lighthouse accessibility audit of the widget with the panel open (spec M6: score >= 95).
// Needs the dev server (npm run dev) and the API on :8000; run with: npm run a11y:lighthouse
import { chromium } from "@playwright/test";
import { launch } from "chrome-launcher";
import lighthouse from "lighthouse";

const URL_ = process.env.WIDGET_PAGE ?? "http://localhost:5173/a11y.html";
const THRESHOLD = 95;

const chrome = await launch({
  chromePath: chromium.executablePath(),
  chromeFlags: ["--headless=new", "--no-sandbox"],
});
try {
  const result = await lighthouse(URL_, {
    port: chrome.port,
    onlyCategories: ["accessibility"],
    output: "json",
    logLevel: "error",
  });
  const lhr = result.lhr;
  const score = Math.round((lhr.categories.accessibility.score ?? 0) * 100);
  console.log(`Lighthouse accessibility: ${score} (threshold ${THRESHOLD}) for ${URL_}`);
  for (const a of Object.values(lhr.audits)) {
    if (a.score !== null && a.score < 1 && a.scoreDisplayMode !== "notApplicable") {
      console.log(`  - ${a.id}: ${a.title}`);
    }
  }
  if (score < THRESHOLD) process.exitCode = 1;
} finally {
  await chrome.kill();
}
