/** Storage that never throws: private windows and blocked site data fall back to memory. */

import type { ChatMessage } from "./types";

export interface Store {
  get(key: string): string | null;
  set(key: string, value: string): void;
  remove(key: string): void;
}

export function makeStore(backend: () => Storage): Store {
  const memory = new Map<string, string>();
  return {
    get(key) {
      try {
        const v = backend().getItem(key);
        if (v !== null) return v;
      } catch {
        /* fall through to memory */
      }
      return memory.get(key) ?? null;
    },
    set(key, value) {
      memory.set(key, value);
      try {
        backend().setItem(key, value);
      } catch {
        /* the memory copy is enough */
      }
    },
    remove(key) {
      memory.delete(key);
      try {
        backend().removeItem(key);
      } catch {
        /* ignore */
      }
    },
  };
}

export const VISITOR_RE = /^[A-Za-z0-9_-]{8,64}$/;
const ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-";

export function newVisitorId(): string {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => ID_ALPHABET[b % 64]).join("");
}

/** Random and anonymous, per browser (localStorage): the same visitor keeps the same id. */
export function visitorId(store: Store): string {
  const key = "bv:visitor";
  const existing = store.get(key);
  if (existing && VISITOR_RE.test(existing)) return existing;
  const id = newVisitorId();
  store.set(key, id);
  return id;
}

export const MAX_THREAD = 30;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * The visible thread and conversation id live in sessionStorage: they survive navigation within
 * the tab and are gone when the tab closes (ADR-007).
 */
export class Session {
  constructor(
    private store: Store,
    private tenant: string,
  ) {}

  private key(name: string): string {
    return `bv:${this.tenant}:${name}`;
  }

  conversationId(): string | null {
    const v = this.store.get(this.key("conv"));
    return v && UUID_RE.test(v) ? v : null;
  }

  setConversationId(id: string): void {
    this.store.set(this.key("conv"), id);
  }

  thread(): ChatMessage[] {
    const raw = this.store.get(this.key("thread"));
    if (!raw) return [];
    try {
      const data: unknown = JSON.parse(raw);
      if (!Array.isArray(data)) return [];
      return data.filter(isMessage).slice(-MAX_THREAD);
    } catch {
      return [];
    }
  }

  saveThread(messages: ChatMessage[]): void {
    const settled = messages
      .slice(-MAX_THREAD)
      .map(({ role, text, sources }) => ({ role, text, sources }));
    this.store.set(this.key("thread"), JSON.stringify(settled));
  }

  clear(): void {
    this.store.remove(this.key("conv"));
    this.store.remove(this.key("thread"));
  }
}

function isMessage(m: unknown): m is ChatMessage {
  if (typeof m !== "object" || m === null) return false;
  const o = m as Record<string, unknown>;
  return (
    (o.role === "user" || o.role === "assistant") &&
    typeof o.text === "string" &&
    Array.isArray(o.sources) &&
    o.sources.every((s) => {
      const x = s as Record<string, unknown> | null;
      return (
        typeof x === "object" &&
        x !== null &&
        typeof x.url === "string" &&
        typeof x.title === "string" &&
        Array.isArray(x.ns)
      );
    })
  );
}

export const localStore = makeStore(() => window.localStorage);
export const sessionStore = makeStore(() => window.sessionStorage);
