/** Incremental Server-Sent Events parser (we POST, so EventSource is not an option). */

export interface SseMessage {
  event: string;
  data: string;
}

export class SseParser {
  private buf = "";
  private event = "";
  private data: string[] = [];

  /** Feed a decoded text chunk; returns the messages completed by it. */
  push(chunk: string): SseMessage[] {
    this.buf += chunk;
    const out: SseMessage[] = [];
    // A trailing CR may be the first half of CRLF: hold it back until more input arrives.
    const hold = this.buf.endsWith("\r") ? 1 : 0;
    const text = this.buf.slice(0, this.buf.length - hold);
    const parts = text.split(/\r\n|\n|\r/);
    this.buf = (parts.pop() ?? "") + this.buf.slice(this.buf.length - hold);
    for (const line of parts) this.line(line, out);
    return out;
  }

  /** Stream ended: a final unterminated event is discarded, per the SSE specification. */
  end(): void {
    this.buf = "";
    this.event = "";
    this.data = [];
  }

  private line(line: string, out: SseMessage[]): void {
    if (line === "") {
      if (this.data.length > 0) {
        out.push({ event: this.event || "message", data: this.data.join("\n") });
      }
      this.event = "";
      this.data = [];
      return;
    }
    if (line.startsWith(":")) return; // comment / keep-alive
    const i = line.indexOf(":");
    const field = i === -1 ? line : line.slice(0, i);
    let value = i === -1 ? "" : line.slice(i + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.event = value;
    else if (field === "data") this.data.push(value);
  }
}
