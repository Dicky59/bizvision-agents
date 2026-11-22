import en from "./en.json";
import fi from "./fi.json";
import type { Lang } from "../types";

export type Strings = typeof fi;
export type StringKey = keyof Strings;

const TABLES: Record<Lang, Strings> = { fi, en };

export function strings(lang: Lang): Strings {
  return TABLES[lang];
}

/** `{name}` placeholders, filled from `vars`. */
export function fmt(template: string, vars: Record<string, string | number> = {}): string {
  return template.replace(/\{(\w+)\}/g, (_, k: string) => String(vars[k] ?? ""));
}
