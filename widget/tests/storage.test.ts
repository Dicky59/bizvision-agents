import { describe, expect, it } from "vitest";
import { MAX_THREAD, Session, VISITOR_RE, makeStore, visitorId } from "../src/storage";
import type { ChatMessage } from "../src/types";

function broken(): Storage {
  throw new Error("SecurityError");
}

function fake(): Storage {
  const m = new Map<string, string>();
  return {
    getItem: (k) => m.get(k) ?? null,
    setItem: (k, v) => void m.set(k, v),
    removeItem: (k) => void m.delete(k),
    clear: () => m.clear(),
    key: () => null,
    length: 0,
  };
}

describe("storage fallbacks", () => {
  it("works in memory when the backend throws", () => {
    const s = makeStore(broken);
    s.set("a", "1");
    expect(s.get("a")).toBe("1");
    s.remove("a");
    expect(s.get("a")).toBeNull();
  });

  it("keeps one visitor id per store and it satisfies the API pattern", () => {
    const s = makeStore(broken);
    const id = visitorId(s);
    expect(VISITOR_RE.test(id)).toBe(true);
    expect(visitorId(s)).toBe(id);
  });

  it("replaces a corrupted visitor id", () => {
    const backend = fake();
    backend.setItem("bv:visitor", "short");
    const id = visitorId(makeStore(() => backend));
    expect(id).not.toBe("short");
    expect(VISITOR_RE.test(id)).toBe(true);
  });
});

describe("Session", () => {
  const msg = (i: number): ChatMessage => ({
    role: i % 2 ? "assistant" : "user",
    text: `m${i}`,
    sources: [],
  });

  it("round-trips and caps the thread at the last messages", () => {
    const sess = new Session(makeStore(fake), "acme");
    sess.saveThread(Array.from({ length: MAX_THREAD + 10 }, (_, i) => msg(i)));
    const t = sess.thread();
    expect(t).toHaveLength(MAX_THREAD);
    expect(t.at(-1)?.text).toBe(`m${MAX_THREAD + 9}`);
  });

  it("does not persist transient error state", () => {
    const sess = new Session(makeStore(fake), "acme");
    sess.saveThread([{ role: "assistant", text: "x", sources: [], error: "boom" }]);
    expect(sess.thread()[0]).not.toHaveProperty("error");
  });

  it("ignores garbage and keeps tenants apart", () => {
    const backend = fake();
    const store = makeStore(() => backend);
    backend.setItem("bv:acme:thread", "{not json");
    backend.setItem("bv:acme:conv", "not-a-uuid");
    const a = new Session(store, "acme");
    expect(a.thread()).toEqual([]);
    expect(a.conversationId()).toBeNull();
    a.setConversationId("0b9f6f7c-2f55-4c4c-9d0e-1c6c4f9a1b11");
    expect(new Session(store, "other").conversationId()).toBeNull();
    expect(a.conversationId()).toBe("0b9f6f7c-2f55-4c4c-9d0e-1c6c4f9a1b11");
    a.clear();
    expect(a.conversationId()).toBeNull();
  });

  it("drops malformed messages from a tampered thread", () => {
    const backend = fake();
    backend.setItem(
      "bv:acme:thread",
      JSON.stringify([
        { role: "system", text: "x", sources: [] },
        { role: "user", text: "ok", sources: [] },
        5,
      ]),
    );
    expect(
      new Session(
        makeStore(() => backend),
        "acme",
      )
        .thread()
        .map((m) => m.text),
    ).toEqual(["ok"]);
  });
});
