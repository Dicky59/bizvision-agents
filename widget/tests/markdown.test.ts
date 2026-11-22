import { describe, expect, it } from "vitest";
import { renderMarkdown, safeHttpUrl } from "../src/markdown";
import type { Source } from "../src/types";

const SRC: Source[] = [
  { ns: [1, 2], title: "SEO", url: "https://acme.fi/seo" },
  { ns: [4], title: "Hinnat", url: "https://acme.fi/hinnat" },
];

function html(text: string, sources: Source[] = SRC, streaming = false): string {
  const box = document.createElement("div");
  box.appendChild(renderMarkdown(document, text, sources, { streaming }));
  return box.innerHTML; // reading is fine; only writing markup is banned
}

describe("renderMarkdown", () => {
  it("renders paragraphs, bold and lists", () => {
    expect(html("Hei **maailma**\n\n- a\n- b\n\n1. x\n2. y")).toBe(
      "<p>Hei <strong>maailma</strong></p><ul><li>a</li><li>b</li></ul><ol><li>x</li><li>y</li></ol>",
    );
  });

  it("escapes HTML in model output", () => {
    const out = html('<img src=x onerror="alert(1)"> <script>alert(1)</script>');
    expect(out).not.toContain("<img");
    expect(out).not.toContain("<script");
    expect(out).toContain("&lt;img");
  });

  it("only links http(s) targets", () => {
    const out = html("[ok](https://a.fi/x) [bad](javascript:alert(1)) [data](data:text/html,x)");
    expect(out).toContain('href="https://a.fi/x"');
    expect(out).toContain('rel="noopener noreferrer"');
    expect(out).not.toContain("javascript:");
    expect(out).not.toContain("data:text");
    expect(out).toContain("bad");
  });

  it("maps [n] markers to numbered source links, several ids to one page share a number", () => {
    const out = html("Kyllä [1] ja [2] sekä [4].");
    expect(out.match(/class="cite"/g)).toHaveLength(3);
    expect(out).toContain('href="https://acme.fi/seo"');
    expect(out).toContain(">[1]</a>");
    expect(out).toContain(">[2]</a>"); // [4] is the second source
    expect(out).toContain('aria-label="Hinnat"');
  });

  it("removes markers that match no source, with the space before them", () => {
    expect(html("FAQ-vastaus [3].")).toBe("<p>FAQ-vastaus.</p>");
    expect(html("A [9, 10] B")).toBe("<p>A B</p>");
    expect(html("Ei lähteitä [1].", [])).toBe("<p>Ei lähteitä.</p>");
  });

  it("hides an incomplete trailing marker while streaming", () => {
    expect(html("Vastaus [", SRC, true)).toBe("<p>Vastaus</p>");
    expect(html("Vastaus [1, ", SRC, true)).toBe("<p>Vastaus</p>");
    expect(html("Vastaus [1", SRC, true)).toBe("<p>Vastaus</p>");
  });

  it("never lets a source with an unsafe url become a link", () => {
    const evil: Source[] = [{ ns: [1], title: "x", url: "javascript:alert(1)" }];
    expect(html("Teksti [1].", evil)).not.toContain("href");
  });

  it("does not treat marker-like text inside a link label as a citation", () => {
    const out = html("[1](https://a.fi)");
    expect(out).toContain('href="https://a.fi/"');
    expect(out).not.toContain('class="cite"');
  });
});

describe("safeHttpUrl", () => {
  it("accepts http(s) only", () => {
    expect(safeHttpUrl("https://a.fi")).toBe("https://a.fi/");
    expect(safeHttpUrl("http://a.fi")).toBe("http://a.fi/");
    expect(safeHttpUrl("ftp://a.fi")).toBeNull();
    expect(safeHttpUrl("//a.fi")).toBeNull();
    expect(safeHttpUrl("")).toBeNull();
  });
});
