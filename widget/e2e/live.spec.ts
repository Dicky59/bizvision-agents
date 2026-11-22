// Smoke test against the REAL local API and LLM (costs a few cents). Skipped unless LIVE=1:
//   docker compose up -d db && uv run uvicorn api.main:app --port 8000   (other terminal)
//   LIVE=1 npx playwright test e2e/live.spec.ts --project=chromium
// It never submits the lead form, so no email is sent.
import { expect, test } from "@playwright/test";

test.skip(!process.env.LIVE, "set LIVE=1 to run against the real API");

test("real Finnish and English questions answer with citations", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/");
  await page.locator(".launcher").click();
  await expect(page.locator(".panel")).toContainText("Hei!");

  await page.locator(".chip").first().click();
  const first = page.locator(".msg-bot").last();
  await expect(first.locator(".body")).not.toBeEmpty({ timeout: 60_000 });
  await expect(page.locator("#bv-input")).toBeEnabled({ timeout: 60_000 });
  await expect(page.locator("[aria-busy=false]")).toBeVisible();
  await expect(first.locator(".sources a").first()).toBeVisible({ timeout: 60_000 });
  // Rendered citations are <a class="cite">; any other "[n]" left in the text is a raw marker.
  const stray = await first.locator(".body").evaluate((n) => {
    const c = n.cloneNode(true) as HTMLElement;
    c.querySelectorAll("a.cite").forEach((a) => a.remove());
    return c.textContent ?? "";
  });
  await page.screenshot({ path: "test-results/live-fi.png" });
  console.log("FI answer:", (await first.locator(".body").innerText()).slice(0, 1200));
  expect(stray).not.toMatch(/\[\d+(,\s*\d+)*\]/);

  await page.locator("#bv-input").fill("What does the free audit include?");
  await page.locator("#bv-input").press("Enter");
  const second = page.locator(".msg-bot").last();
  await expect(second.locator(".body")).not.toBeEmpty({ timeout: 60_000 });
  await expect(page.locator("[aria-busy=false]")).toBeVisible({ timeout: 60_000 });
  console.log("EN answer:", (await second.locator(".body").innerText()).slice(0, 1200));
  await page.screenshot({ path: "test-results/live-chat.png" });
});
