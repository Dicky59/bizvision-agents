/** Tenant colours must not be able to break contrast: derive readable text colours from them. */

type Rgb = [number, number, number];

export const DEFAULT_COLOR = "#1f6f8b";

export function parseHex(c: string): Rgb | null {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(c.trim());
  if (!m) return null;
  let h = m[1] ?? "";
  if (h.length === 3) h = [...h].map((x) => x + x).join("");
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as Rgb;
}

export function toHex([r, g, b]: Rgb): string {
  return "#" + [r, g, b].map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");
}

function luminance([r, g, b]: Rgb): number {
  const f = (v: number) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

export function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

const WHITE: Rgb = [255, 255, 255];
const INK: Rgb = [17, 17, 17];

/** A valid 6-digit colour, or the default. */
export function normalizeColor(c: string): string {
  const rgb = parseHex(c);
  return rgb ? toHex(rgb) : DEFAULT_COLOR;
}

/** Text colour for use on top of `bg` (button label on the primary colour). */
export function onColor(bg: string): string {
  const rgb = parseHex(bg) ?? (parseHex(DEFAULT_COLOR) as Rgb);
  return contrast(rgb, WHITE) >= contrast(rgb, INK) ? "#ffffff" : "#111111";
}

/** The colour itself if it reads on white at AA (4.5:1), otherwise a darkened variant. */
export function readableOnWhite(c: string): string {
  let rgb = parseHex(c) ?? (parseHex(DEFAULT_COLOR) as Rgb);
  for (let i = 0; i < 30 && contrast(rgb, WHITE) < 4.5; i++) {
    rgb = rgb.map((v) => v * 0.9) as Rgb;
  }
  return toHex(rgb);
}
