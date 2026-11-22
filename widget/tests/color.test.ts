import { describe, expect, it } from "vitest";
import {
  DEFAULT_COLOR,
  contrast,
  normalizeColor,
  onColor,
  parseHex,
  readableOnWhite,
} from "../src/color";

describe("color helpers", () => {
  it("parses 3 and 6 digit hex, rejects the rest", () => {
    expect(parseHex("#fff")).toEqual([255, 255, 255]);
    expect(parseHex("#1f6f8b")).toEqual([31, 111, 139]);
    expect(parseHex("red")).toBeNull();
    expect(parseHex("url(x)")).toBeNull();
  });

  it("falls back to the default for invalid tenant colours (no CSS injection)", () => {
    expect(normalizeColor("red; background:url(//evil)")).toBe(DEFAULT_COLOR);
    expect(normalizeColor("#ABC")).toBe("#aabbcc");
  });

  it("picks a label colour that reads on the background", () => {
    for (const bg of ["#1f6f8b", "#ffff00", "#000000", "#ffffff", "#ff6600", "#7fdbff"]) {
      const fg = onColor(bg);
      expect(contrast(parseHex(bg)!, parseHex(fg)!)).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("darkens a light colour until it reads on white at AA", () => {
    const out = readableOnWhite("#ffe08a");
    expect(contrast(parseHex(out)!, [255, 255, 255])).toBeGreaterThanOrEqual(4.5);
    expect(readableOnWhite("#1f6f8b")).toBe("#1f6f8b");
  });
});
