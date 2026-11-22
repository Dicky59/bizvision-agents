import type { Page, Route } from "@playwright/test";

export const API = "http://localhost:8000";
export const CONVERSATION = "0b9f6f7c-2f55-4c4c-9d0e-1c6c4f9a1b11";

export const CONFIG = {
  display_name: "BizVision",
  name: "BizVision Oy",
  languages: ["fi", "en"],
  default_language: "fi",
  greeting: { fi: "Hei! Kysy minulta palveluistamme.", en: "Hi! Ask me about our services." },
  starter_questions: {
    fi: ["Mitä palveluita tarjoatte?", "Miten projekti etenee?"],
    en: ["What services do you offer?", "How does a project work?"],
  },
  launcher_label: { fi: "Kysy meiltä", en: "Ask us" },
  primary_color: "#1D7A82",
  position: "bottom-right",
  privacy_notice_url: {
    fi: "https://www.bizvision.fi/tietosuoja",
    en: "https://www.bizvision.fi/en/privacy",
  },
  privacy_notice_version: "2026-11",
  max_message_chars: 1500,
};

export function sse(events: [string, object][]): string {
  return events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join("");
}

export const ANSWER = sse([
  ["meta", { conversation_id: CONVERSATION }],
  ["token", { text: "Tarjoamme **SEO-palvelua** [1] " }],
  ["token", { text: "ja automaatiota [3]." }],
  ["sources", { items: [{ ns: [1], title: "SEO-palvelut", url: "https://bizvision.fi/seo" }] }],
  ["done", { message_id: "9d3b5d7e-1111-4222-8333-444455556666" }],
]);

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "*",
  "access-control-allow-methods": "GET, POST, OPTIONS",
  "access-control-expose-headers": "Retry-After", // as the real API does
};

export interface Mock {
  chatBodies: unknown[];
  leadBodies: unknown[];
  /** Replace how the next chat requests are answered. */
  chat: (route: Route) => Promise<void>;
  lead: (route: Route) => Promise<void>;
}

export async function mockApi(
  page: Page,
  overrides: Partial<Pick<Mock, "chat" | "lead">> = {},
): Promise<Mock> {
  const mock: Mock = {
    chatBodies: [],
    leadBodies: [],
    chat:
      overrides.chat ??
      ((route) =>
        route.fulfill({
          status: 200,
          headers: { ...CORS, "content-type": "text/event-stream" },
          body: ANSWER,
        })),
    lead:
      overrides.lead ??
      ((route) =>
        route.fulfill({
          status: 201,
          headers: CORS,
          contentType: "application/json",
          body: '{"status":"received"}',
        })),
  };
  await page.route(`${API}/**`, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    if (url.pathname.endsWith("/config")) {
      return route.fulfill({
        status: 200,
        headers: CORS,
        contentType: "application/json",
        body: JSON.stringify(CONFIG),
      });
    }
    if (url.pathname.endsWith("/chat")) {
      mock.chatBodies.push(req.postDataJSON());
      return mock.chat(route);
    }
    if (url.pathname.endsWith("/leads")) {
      mock.leadBodies.push(req.postDataJSON());
      return mock.lead(route);
    }
    return route.fulfill({ status: 404, headers: CORS, body: "{}" });
  });
  return mock;
}

export function errorResponse(status: number, code: string, extra: Record<string, string> = {}) {
  return (route: Route) =>
    route.fulfill({
      status,
      headers: { ...CORS, ...extra },
      contentType: "application/json",
      body: JSON.stringify({ error: { code } }),
    });
}

export const isMobile = (page: Page) => (page.viewportSize()?.width ?? 1000) <= 480;
export { CORS };
