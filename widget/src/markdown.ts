/**
 * Safe Markdown subset for assistant answers: paragraphs, **bold**, lists, links and the
 * inline [n] citation markers (ADR-005).
 *
 * Output is built from DOM nodes with textContent only: model output never becomes markup.
 * Only http(s) link targets are rendered as links. Markers that match no source are removed
 * (FAQ and file documents have no URL), so raw "[1]" never shows to the visitor.
 */

import type { Source } from "./types";

export interface RenderOptions {
  /** While streaming, an incomplete trailing marker such as "[1" is hidden until it completes. */
  streaming?: boolean;
}

const INLINE = /\*\*([^*\n]+?)\*\*|\[([^\]\n]+)\]\(([^)\s]*)\)|(\s?)\[(\d+(?:\s*,\s*\d+)*)\]/g;

export function safeHttpUrl(raw: string): string | null {
  try {
    const u = new URL(raw);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

function link(doc: Document, href: string, text: string, cls?: string): HTMLAnchorElement {
  const a = doc.createElement("a");
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.textContent = text;
  if (cls) a.className = cls;
  return a;
}

function sourceIndex(sources: Source[], n: number): number {
  return sources.findIndex((s) => s.ns.includes(n));
}

function inline(doc: Document, parent: Node, text: string, sources: Source[]): void {
  let last = 0;
  let lastCite = -1; // source index of the previous citation and where its marker ended
  let lastCiteEnd = -1;
  INLINE.lastIndex = 0;
  for (let m = INLINE.exec(text); m !== null; m = INLINE.exec(text)) {
    if (m.index > last) parent.appendChild(doc.createTextNode(text.slice(last, m.index)));
    last = m.index + m[0].length;
    if (m[1] !== undefined) {
      const b = doc.createElement("strong");
      b.textContent = m[1];
      parent.appendChild(b);
    } else if (m[2] !== undefined) {
      const href = safeHttpUrl(m[3] ?? "");
      if (href) parent.appendChild(link(doc, href, m[2]));
      else parent.appendChild(doc.createTextNode(m[2])); // unsafe target: keep the label only
    } else {
      const lead = m[4] ?? "";
      const ids = (m[5] ?? "").split(",").map((s) => Number(s.trim()));
      const hits = [...new Set(ids.map((n) => sourceIndex(sources, n)).filter((i) => i >= 0))];
      hits.forEach((i, k) => {
        const s = sources[i];
        if (!s) return;
        const href = safeHttpUrl(s.url);
        if (!href) return;
        // "[1][1]" (two chunks of one page, cited back to back) shows a single [1].
        if (lead === "" && k === 0 && m.index === lastCiteEnd && i === lastCite) return;
        if (k === 0 && lead) parent.appendChild(doc.createTextNode(lead));
        const a = link(doc, href, `[${i + 1}]`, "cite");
        a.setAttribute("aria-label", s.title);
        parent.appendChild(a);
        lastCite = i;
        lastCiteEnd = last;
      });
    }
  }
  if (last < text.length) parent.appendChild(doc.createTextNode(text.slice(last)));
}

type Block = { kind: "p"; lines: string[] } | { kind: "ul" | "ol"; items: string[] };

function blocks(text: string): Block[] {
  const out: Block[] = [];
  let open = false; // the last block in `out` can still take more lines
  for (const raw of text.replace(/\r\n?/g, "\n").split("\n")) {
    const line = raw.trimEnd();
    const ul = /^\s*[-*]\s+(.*)$/.exec(line);
    const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    const last = open ? out[out.length - 1] : undefined;
    if (line.trim() === "") {
      open = false;
    } else if (ul || ol) {
      const kind = ul ? "ul" : "ol";
      const item = (ul ?? ol)?.[1] ?? "";
      if (last && last.kind === kind) last.items.push(item);
      else out.push({ kind, items: [item] });
      open = true;
    } else if (last && last.kind === "p") {
      last.lines.push(line.trim());
    } else {
      out.push({ kind: "p", lines: [line.trim()] });
      open = true;
    }
  }
  return out;
}

export function renderMarkdown(
  doc: Document,
  text: string,
  sources: Source[],
  opts: RenderOptions = {},
): DocumentFragment {
  const src = opts.streaming ? text.replace(/\s?\[[\d,\s]*$/, "") : text;
  const frag = doc.createDocumentFragment();
  for (const b of blocks(src)) {
    if (b.kind === "p") {
      const p = doc.createElement("p");
      b.lines.forEach((line, i) => {
        if (i > 0) p.appendChild(doc.createElement("br"));
        inline(doc, p, line, sources);
      });
      frag.appendChild(p);
    } else {
      const list = doc.createElement(b.kind);
      for (const item of b.items) {
        const li = doc.createElement("li");
        inline(doc, li, item, sources);
        list.appendChild(li);
      }
      frag.appendChild(list);
    }
  }
  return frag;
}
