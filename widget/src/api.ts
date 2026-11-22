import { SseParser } from "./sse";
import type { ChatEvent, Lang, Source, WidgetConfig } from "./types";

/** A non-2xx answer from the API (JSON `{"error":{"code"}}`, or FastAPI's 422 `detail`). */
export class ApiFailure extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly retryAfter: number | null,
  ) {
    super(code);
  }
}

async function failure(res: Response): Promise<ApiFailure> {
  let code = `http_${res.status}`;
  try {
    const body: unknown = await res.json();
    const c = (body as { error?: { code?: unknown } } | null)?.error?.code;
    if (typeof c === "string") code = c;
    else if (res.status === 422) code = "validation";
  } catch {
    /* non-JSON body: keep the status-based code */
  }
  const ra = Number(res.headers.get("Retry-After"));
  return new ApiFailure(res.status, code, Number.isFinite(ra) && ra > 0 ? ra : null);
}

export interface Api {
  getConfig(signal?: AbortSignal): Promise<WidgetConfig>;
  streamChat(body: ChatBody, signal: AbortSignal): AsyncGenerator<ChatEvent>;
  postLead(body: LeadBody): Promise<void>;
}

export interface ChatBody {
  visitor_id: string;
  message: string;
  conversation_id?: string;
  language?: Lang;
  page_url?: string;
}

export interface LeadBody {
  name: string;
  email: string;
  phone?: string;
  message?: string;
  consent: true;
  language: Lang;
  conversation_id?: string;
  website: string; // honeypot, sent as the (empty) hidden field value
}

export function makeApi(base: string, tenant: string, fetchFn: typeof fetch = fetch): Api {
  const root = `${base.replace(/\/+$/, "")}`;
  const url = (path: string) => `${root}${path.replace("{t}", encodeURIComponent(tenant))}`;

  return {
    async getConfig(signal) {
      const res = await fetchFn(url("/v1/widget/{t}/config"), { signal });
      if (!res.ok) throw await failure(res);
      return (await res.json()) as WidgetConfig;
    },

    async *streamChat(body, signal) {
      const res = await fetchFn(url("/v1/assistant/{t}/chat"), {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(body),
        signal,
      });
      if (!res.ok) throw await failure(res);
      if (!res.body) throw new ApiFailure(res.status, "no_stream", null);
      const reader = res.body.getReader();
      const decoder = new TextDecoder(); // stream: true keeps multi-byte characters across chunks
      const parser = new SseParser();
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          for (const m of parser.push(decoder.decode(value, { stream: true }))) {
            const ev = toEvent(m.event, m.data);
            if (ev) yield ev;
          }
        }
        for (const m of parser.push(decoder.decode())) {
          const ev = toEvent(m.event, m.data);
          if (ev) yield ev;
        }
      } finally {
        parser.end();
        reader.cancel().catch(() => undefined);
      }
    },

    async postLead(body) {
      const res = await fetchFn(url("/v1/assistant/{t}/leads"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw await failure(res);
    },
  };
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** Map one SSE message to a typed event; unknown or malformed events are ignored. */
export function toEvent(event: string, data: string): ChatEvent | null {
  let d: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(data);
    if (typeof parsed !== "object" || parsed === null) return null;
    d = parsed as Record<string, unknown>;
  } catch {
    return null;
  }
  switch (event) {
    case "meta":
      return { type: "meta", conversationId: str(d.conversation_id) };
    case "token":
      return { type: "token", text: str(d.text) };
    case "sources": {
      const items = Array.isArray(d.items) ? d.items : [];
      const out: Source[] = [];
      for (const i of items) {
        const o = i as Record<string, unknown>;
        const ns = Array.isArray(o.ns) ? o.ns.filter((n): n is number => Number.isInteger(n)) : [];
        if (typeof o.url === "string") out.push({ ns, title: str(o.title) || o.url, url: o.url });
      }
      return { type: "sources", items: out };
    }
    case "lead_form":
      return { type: "lead_form", reason: str(d.reason) };
    case "done":
      return { type: "done", messageId: typeof d.message_id === "string" ? d.message_id : null };
    case "error":
      return { type: "error", code: str(d.code), message: str(d.message) };
    default:
      return null;
  }
}
