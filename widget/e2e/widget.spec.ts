import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { ANSWER, CONVERSATION, CORS, errorResponse, isMobile, mockApi, sse } from "./mock";

const LEAD_EVENTS = sse([
  ["meta", { conversation_id: CONVERSATION }],
  ["token", { text: "Haluatko, että otamme yhteyttä?" }],
  ["lead_form", { reason: "wants_human" }],
  ["done", { message_id: null }],
]);

async function open(page: Page, path = "/") {
  await page.goto(path);
  await page.locator(".launcher").click();
  await expect(page.locator(".panel")).toBeVisible();
}

async function ask(page: Page, text: string) {
  await page.locator("#bv-input").fill(text);
  await page.locator("#bv-input").press("Enter");
}

const sseResponse = (body: string) => (route: import("@playwright/test").Route) =>
  route.fulfill({ status: 200, headers: { ...CORS, "content-type": "text/event-stream" }, body });

test("greets, streams an answer with bold text, citations and a source list", async ({ page }) => {
  const mock = await mockApi(page);
  await open(page);
  await expect(page.locator(".panel")).toContainText("Hei! Kysy minulta palveluistamme.");
  await expect(page.locator(".chip")).toHaveCount(2);

  await page.locator(".chip").first().click();
  const bot = page.locator(".msg-bot").last();
  await expect(bot.locator("strong")).toHaveText("SEO-palvelua");
  await expect(bot.locator("a.cite")).toHaveCount(1);
  await expect(bot.locator("a.cite")).toHaveAttribute("href", "https://bizvision.fi/seo");
  await expect(bot.locator("a.cite")).toHaveText("[1]");
  await expect(bot).not.toContainText("[3]"); // unmatched marker (FAQ doc) is removed
  await expect(bot.locator(".sources a")).toHaveText("[1] SEO-palvelut");
  await expect(page.locator(".chip")).toHaveCount(0);

  const sent = mock.chatBodies[0] as Record<string, unknown>;
  expect(sent.message).toBe("Mitä palveluita tarjoatte?");
  expect(String(sent.visitor_id)).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
  expect(String(sent.page_url)).toMatch(/^http:\/\/localhost:5173\//);
  expect(sent.conversation_id).toBeUndefined();
});

test("sends the conversation id on the next turn", async ({ page }) => {
  const mock = await mockApi(page);
  await open(page);
  await ask(page, "Ensimmäinen");
  await expect(page.locator(".msg-bot")).toHaveCount(2);
  await ask(page, "Toinen");
  await expect(page.locator(".msg-bot")).toHaveCount(3);
  expect((mock.chatBodies[1] as Record<string, unknown>).conversation_id).toBe(CONVERSATION);
});

test("host page CSS does not leak into the widget", async ({ page }) => {
  await mockApi(page);
  await open(page);
  const font = await page.locator(".panel").evaluate((n) => getComputedStyle(n).fontFamily);
  expect(font).not.toMatch(/comic/i);
  const spacing = await page
    .locator(".msg-bot")
    .first()
    .evaluate((n) => getComputedStyle(n).letterSpacing);
  expect(spacing).toBe("normal"); // host rule `* { letter-spacing: 4px !important }` must not inherit in
  const border = await page.locator("#bv-input").evaluate((n) => getComputedStyle(n).borderStyle);
  expect(border).not.toContain("dotted");
});

test("the visible thread survives a reload in the same tab", async ({ page }) => {
  await mockApi(page);
  await open(page);
  await ask(page, "Kerro palveluistanne");
  await expect(page.locator(".msg-bot").last()).toContainText("SEO-palvelua");
  await page.reload();
  await page.locator(".launcher").click();
  await expect(page.locator(".msg-user .body")).toHaveText("Kerro palveluistanne");
  await expect(page.locator(".msg-bot").last()).toContainText("SEO-palvelua");
  await expect(page.locator(".chip")).toHaveCount(0);
});

test("model output cannot inject markup or run script", async ({ page }) => {
  const evil = sse([
    ["meta", { conversation_id: CONVERSATION }],
    [
      "token",
      { text: '<img src=x onerror="window.__pwned=1"> [klik](javascript:window.__pwned=1)' },
    ],
    ["done", { message_id: null }],
  ]);
  await mockApi(page, { chat: sseResponse(evil) });
  await open(page);
  await ask(page, "hei");
  await expect(page.locator(".msg-bot").last()).toContainText("<img src=x");
  expect(await page.locator(".msg-bot img").count()).toBe(0);
  expect(await page.locator(".msg-bot a[href^='javascript']").count()).toBe(0);
  expect(
    await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned),
  ).toBeUndefined();
});

test("rate limit shows minutes and retry resends the question once", async ({ page }) => {
  const mock = await mockApi(page, {
    chat: errorResponse(429, "rate_limited", { "retry-after": "120" }),
  });
  await open(page);
  await ask(page, "Hei");
  await expect(page.locator(".alert")).toContainText("2 min");
  mock.chat = sseResponse(ANSWER);
  await page.locator(".alert .btn-link").click();
  await expect(page.locator(".alert")).toBeHidden();
  await expect(page.locator(".msg-user")).toHaveCount(1);
  await expect(page.locator(".msg-bot").last()).toContainText("SEO-palvelua");
});

test("forbidden and server error events show a calm message", async ({ page }) => {
  const mock = await mockApi(page, { chat: errorResponse(403, "forbidden") });
  await open(page);
  await ask(page, "Hei");
  await expect(page.locator(".alert")).toContainText("ei ole juuri nyt käytettävissä");
  mock.chat = sseResponse(
    sse([
      ["meta", { conversation_id: CONVERSATION }],
      ["error", { code: "internal", message: "Pahoittelut, jokin meni pieleen." }],
    ]),
  );
  await page.locator(".alert .btn-link").click();
  await expect(page.locator(".alert")).toContainText("Pahoittelut, jokin meni pieleen.");
});

test("lead form: validation, honeypot, consent link, successful submit", async ({ page }) => {
  const mock = await mockApi(page, { chat: sseResponse(LEAD_EVENTS) });
  await open(page);
  await ask(page, "Haluan puhua ihmiselle");
  const form = page.locator("form.lead");
  await expect(form).toBeVisible();
  await expect(page.locator("#bv-lf-name")).toBeFocused();
  await expect(form.locator("a")).toHaveAttribute("href", "https://bizvision.fi/tietosuoja");
  await expect(form.locator("input[name=website]")).toHaveAttribute("tabindex", "-1");

  await form.locator("button[type=submit]").click();
  await expect(page.locator("#bv-lf-name-err")).not.toBeEmpty();
  await expect(page.locator("#bv-lf-email-err")).not.toBeEmpty();
  await expect(page.locator("#bv-lf-consent-err")).not.toBeEmpty();
  expect(mock.leadBodies).toHaveLength(0);

  await page.locator("#bv-lf-name").fill("Matti Meikäläinen");
  await page.locator("#bv-lf-email").fill("matti@example.com");
  await page.locator("#bv-lf-message").fill("Soittakaa");
  await form.locator("button[type=submit]").click();
  await expect(page.locator("#bv-lf-consent-err")).not.toBeEmpty(); // consent still missing
  expect(mock.leadBodies).toHaveLength(0);

  await page.locator("#bv-lf-consent").check();
  await form.locator("button[type=submit]").click();
  await expect(page.locator(".msg-bot").last()).toContainText("Kiitos!");
  await expect(form).toBeHidden();
  expect(mock.leadBodies).toEqual([
    {
      name: "Matti Meikäläinen",
      email: "matti@example.com",
      message: "Soittakaa",
      consent: true,
      language: "fi",
      conversation_id: CONVERSATION,
      website: "",
    },
  ]);
});

test("lead form shows a message when the API refuses (429)", async ({ page }) => {
  await mockApi(page, { chat: sseResponse(LEAD_EVENTS), lead: errorResponse(429, "rate_limited") });
  await open(page);
  await ask(page, "Haluan puhua ihmiselle");
  await page.locator("#bv-lf-name").fill("A");
  await page.locator("#bv-lf-email").fill("a@example.com");
  await page.locator("#bv-lf-consent").check();
  await page.locator("form.lead button[type=submit]").click();
  await expect(page.locator(".form-err")).toContainText("Liian monta");
  await expect(page.locator("form.lead")).toBeVisible();
});

test("Escape closes the panel and returns focus to the launcher", async ({ page }) => {
  await mockApi(page);
  await open(page);
  await page.locator("#bv-input").focus();
  await page.keyboard.press("Escape");
  await expect(page.locator(".panel")).toBeHidden();
  await expect(page.locator(".launcher")).toBeFocused();
  await expect(page.locator(".launcher")).toHaveAttribute("aria-expanded", "false");
});

test("desktop: the panel is non-modal, Tab can reach the page", async ({ page }) => {
  test.skip(isMobile(page), "desktop layout only");
  await mockApi(page);
  await open(page);
  await expect(page.locator(".panel")).toHaveAttribute("aria-modal", "false");
  await page.locator("#bv-input").focus();
  let left = false;
  for (let i = 0; i < 8 && !left; i++) {
    await page.keyboard.press("Tab");
    left = await page.evaluate(() => document.activeElement?.id !== "bizvision-widget");
  }
  expect(left).toBe(true);
});

test("mobile: full-screen panel is modal and traps focus", async ({ page }) => {
  test.skip(!isMobile(page), "mobile layout only");
  await mockApi(page);
  await open(page);
  await expect(page.locator(".panel")).toHaveAttribute("aria-modal", "true");
  const vp = page.viewportSize();
  const box = await page.locator(".panel").boundingBox();
  expect(box).not.toBeNull();
  expect(Math.round(box!.width)).toBe(vp!.width);
  expect(Math.abs(box!.height - vp!.height)).toBeLessThanOrEqual(2);
  await expect(page.locator(".launcher")).toBeHidden();

  await page.locator("#bv-input").focus();
  for (let i = 0; i < 12; i++) {
    await page.keyboard.press("Tab");
    const inside = await page.evaluate(() => {
      const host = document.getElementById("bizvision-widget");
      return host?.shadowRoot?.activeElement?.closest(".panel") != null;
    });
    expect(inside).toBe(true);
  }
});

test("English page: language from the page, bottom-left position", async ({ page }) => {
  test.skip(isMobile(page), "position check is for desktop");
  await mockApi(page);
  await open(page, "/en.html");
  await expect(page.locator(".panel")).toContainText("Hi! Ask me about our services.");
  await expect(page.locator(".chip").first()).toHaveText("What services do you offer?");
  const box = await page.locator(".launcher").boundingBox();
  expect(box!.x).toBeLessThan(100);
});

test("no widget and no crash when the API is unreachable", async ({ page }) => {
  await page.route("http://localhost:8000/**", (route) => route.abort());
  await page.goto("/");
  await page.waitForTimeout(500);
  expect(await page.locator("#bizvision-widget").count()).toBe(0);
});

test("a11y: no axe violations (chat view and lead form)", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" }); // scan settled colours, not a fade-in frame
  await mockApi(page, { chat: sseResponse(LEAD_EVENTS) });
  await open(page);
  const scan = () =>
    new AxeBuilder({ page })
      .include("#bizvision-widget")
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
  expect((await scan()).violations).toEqual([]);
  await ask(page, "Haluan puhua ihmiselle");
  await expect(page.locator("form.lead")).toBeVisible();
  expect((await scan()).violations).toEqual([]);
});
