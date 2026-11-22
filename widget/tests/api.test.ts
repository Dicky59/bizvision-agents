import { describe, expect, it } from "vitest";
import { ApiFailure, makeApi, toEvent } from "../src/api";
import type { ChatEvent } from "../src/types";

const enc = new TextEncoder();

function sseResponse(chunks: Uint8Array[], init: ResponseInit = {}): Response {
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      for (const ch of chunks) c.enqueue(ch);
      c.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
    ...init,
  });
}

async function collect(gen: AsyncGenerator<ChatEvent>): Promise<ChatEvent[]> {
  const out: ChatEvent[] = [];
  for await (const e of gen) out.push(e);
  return out;
}

const body = { visitor_id: "visitor-0001", message: "Hei" };

describe("streamChat", () => {
  it("yields typed events and keeps multi-byte characters split across chunks intact", async () => {
    const raw = enc.encode(
      'event: meta\ndata: {"conversation_id":"c1"}\n\n' +
        'event: token\ndata: {"text":"Kyllä ääkköset"}\n\n' +
        'event: sources\ndata: {"items":[{"ns":[1],"title":"SEO","url":"https://a.fi/s"}]}\n\n' +
        'event: done\ndata: {"message_id":"m1"}\n\n',
    );
    const cut = raw.indexOf(0xc3) + 1; // split inside "ä" (0xC3 0xA4)
    const api = makeApi("https://api.test", "acme", async () =>
      sseResponse([raw.slice(0, cut), raw.slice(cut)]),
    );
    const events = await collect(api.streamChat(body, new AbortController().signal));
    expect(events.map((e) => e.type)).toEqual(["meta", "token", "sources", "done"]);
    expect(events[1]).toEqual({ type: "token", text: "Kyllä ääkköset" });
    expect(events[2]).toEqual({
      type: "sources",
      items: [{ ns: [1], title: "SEO", url: "https://a.fi/s" }],
    });
  });

  it("sends the request as the API expects", async () => {
    let seen: { url: string; init: RequestInit } | null = null;
    const api = makeApi("https://api.test/", "ac me", async (url, init) => {
      seen = { url: String(url), init: init ?? {} };
      return sseResponse([]);
    });
    await collect(api.streamChat({ ...body, language: "fi" }, new AbortController().signal));
    expect(seen).not.toBeNull();
    const s = seen as unknown as { url: string; init: RequestInit };
    expect(s.url).toBe("https://api.test/v1/assistant/ac%20me/chat");
    expect(s.init.method).toBe("POST");
    expect(JSON.parse(String(s.init.body))).toEqual({ ...body, language: "fi" });
  });

  it("throws ApiFailure with code and Retry-After for JSON errors", async () => {
    const api = makeApi("https://api.test", "acme", async () =>
      Response.json(
        { error: { code: "rate_limited" } },
        { status: 429, headers: { "Retry-After": "42" } },
      ),
    );
    const err = await collect(api.streamChat(body, new AbortController().signal)).catch((e) => e);
    expect(err).toBeInstanceOf(ApiFailure);
    expect(err).toMatchObject({ status: 429, code: "rate_limited", retryAfter: 42 });
  });

  it("maps FastAPI 422 and non-JSON errors to stable codes", async () => {
    const a422 = makeApi("https://x", "t", async () =>
      Response.json({ detail: [] }, { status: 422 }),
    );
    expect(
      await a422
        .postLead({ name: "a", email: "b", consent: true, language: "fi", website: "" })
        .catch((e) => e),
    ).toMatchObject({ code: "validation" });
    const a502 = makeApi("https://x", "t", async () => new Response("<html>", { status: 502 }));
    expect(await a502.getConfig().catch((e) => e)).toMatchObject({ status: 502, code: "http_502" });
  });
});

describe("toEvent", () => {
  it("ignores unknown events and malformed payloads", () => {
    expect(toEvent("ping", "{}")).toBeNull();
    expect(toEvent("token", "not json")).toBeNull();
    expect(toEvent("token", "null")).toBeNull();
  });

  it("handles error and lead_form events", () => {
    expect(toEvent("error", '{"code":"internal","message":"Pahoittelut"}')).toEqual({
      type: "error",
      code: "internal",
      message: "Pahoittelut",
    });
    expect(toEvent("lead_form", '{"reason":"wants_human"}')).toEqual({
      type: "lead_form",
      reason: "wants_human",
    });
    expect(toEvent("done", '{"message_id":null}')).toEqual({ type: "done", messageId: null });
  });

  it("drops sources without a url", () => {
    expect(
      toEvent("sources", '{"items":[{"title":"x"},{"title":"y","url":"https://a.fi","ns":[2]}]}'),
    ).toEqual({
      type: "sources",
      items: [{ ns: [2], title: "y", url: "https://a.fi" }],
    });
  });
});
