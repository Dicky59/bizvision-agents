/** Tiny DOM helpers. Text always goes through textContent / text nodes, never markup. */

type Attrs = Record<string, string | boolean | undefined>;

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  ...children: (Node | string)[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === false) continue;
    node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) node.append(c);
  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";

/** A decorative 24x24 icon from path data (aria-hidden: the button carries the label). */
export function icon(...paths: string[]): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "24");
  svg.setAttribute("height", "24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  for (const d of paths) {
    const p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("d", d);
    svg.appendChild(p);
  }
  return svg;
}

export const ICONS = {
  bubble: () =>
    icon("M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-3.6-.8L3 21l1.9-5.1A8.4 8.4 0 1 1 21 11.5z"),
  close: () => icon("M6 6l12 12", "M18 6L6 18"),
  send: () => icon("M22 2L11 13", "M22 2l-7 20-4-9-9-4 20-7z"),
  stop: () => icon("M7 7h10v10H7z"),
  restart: () => icon("M3 12a9 9 0 1 0 3-6.7", "M3 4v5h5"),
};

const FOCUSABLE =
  'button:not([disabled]), a[href], input:not([disabled]):not([tabindex="-1"]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Visible, tabbable elements inside `root` (hidden subtrees are skipped). */
export function focusables(root: Element): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (n) => n.closest("[hidden]") === null,
  );
}
