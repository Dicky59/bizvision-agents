import { describe, expect, it } from "vitest";
import { SseParser } from "../src/sse";

const frame = (event: string, data: object) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

describe("SseParser", () => {
  it("parses whole frames", () => {
    const p = new SseParser();
    const out = p.push(frame("meta", { conversation_id: "x" }) + frame("token", { text: "Hei" }));
    expect(out.map((m) => m.event)).toEqual(["meta", "token"]);
    expect(JSON.parse(out[1]?.data ?? "{}")).toEqual({ text: "Hei" });
  });

  it("handles a frame split at every possible byte boundary", () => {
    const whole = frame("token", { text: "Kyllä, tarjoamme [1]." }) + frame("done", {});
    for (let i = 1; i < whole.length; i++) {
      const p = new SseParser();
      const out = [...p.push(whole.slice(0, i)), ...p.push(whole.slice(i))];
      expect(out.map((m) => m.event)).toEqual(["token", "done"]);
    }
  });

  it("supports CRLF and CR line endings, including CRLF split across chunks", () => {
    const p = new SseParser();
    const out = [
      ...p.push("event: token\r"),
      ...p.push('\ndata: {"text":"a"}\r'),
      ...p.push("\n\r"),
      ...p.push("\n"),
    ];
    expect(out).toEqual([{ event: "token", data: '{"text":"a"}' }]);
    // a lone trailing CR is held back (it may be half a CRLF) until the next chunk decides
    const cr = new SseParser();
    expect(cr.push("event: x\rdata: 1\r\r")).toEqual([]);
    expect(cr.push("event: y\r")).toEqual([{ event: "x", data: "1" }]);
  });

  it("joins multi-line data, ignores comments and unknown fields", () => {
    const p = new SseParser();
    const out = p.push(": keep-alive\nid: 7\ndata: a\ndata: b\n\n");
    expect(out).toEqual([{ event: "message", data: "a\nb" }]);
  });

  it("drops a final unterminated event", () => {
    const p = new SseParser();
    expect(p.push("event: token\ndata: {}")).toEqual([]);
    p.end();
    expect(p.push("\n\n")).toEqual([]);
  });
});
