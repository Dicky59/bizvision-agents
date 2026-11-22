/**
 * Entry point: finds its own <script data-tenant="..."> tag, loads the tenant config and mounts the
 * widget inside a Shadow DOM. Fails silently (one console warning, no details) when the API is
 * unreachable or the origin is not allowed: a broken widget must never break the host page.
 */

import { makeApi } from "./api";
import { localStore, Session, sessionStore, visitorId } from "./storage";
import css from "./styles.css?inline";
import type { Lang } from "./types";
import { strings } from "./i18n";
import { mountApp } from "./ui/app";

declare global {
  interface Window {
    __bizvisionWidget?: boolean;
  }
}

function findScript(): HTMLScriptElement | null {
  const current = document.currentScript;
  if (current instanceof HTMLScriptElement && current.dataset.tenant) return current;
  return document.querySelector<HTMLScriptElement>("script[data-tenant]");
}

function pickLang(requested: string | undefined, supported: Lang[], fallback: Lang): Lang {
  const candidates = [requested, document.documentElement.lang];
  for (const c of candidates) {
    const base = c?.toLowerCase().split("-")[0];
    const hit = supported.find((l) => l === base);
    if (hit) return hit;
  }
  return fallback;
}

async function init(): Promise<void> {
  if (window.__bizvisionWidget) return; // one widget per page, even if the tag is included twice
  const script = findScript();
  const tenant = script?.dataset.tenant;
  if (!script || !tenant) {
    console.warn("[bizvision-widget] missing data-tenant attribute");
    return;
  }
  window.__bizvisionWidget = true;

  const base = script.dataset.api || (script.src ? new URL(script.src).origin : "");
  const api = makeApi(base, tenant);
  let config;
  try {
    config = await api.getConfig();
  } catch {
    console.warn("[bizvision-widget] could not load the configuration");
    window.__bizvisionWidget = false;
    return;
  }

  const lang = pickLang(script.dataset.lang, config.languages, config.default_language);
  const requested = script.dataset.position;
  const position =
    requested === "bottom-left" || requested === "bottom-right"
      ? requested
      : config.position === "bottom-left"
        ? "bottom-left"
        : "bottom-right";

  const host = document.createElement("div");
  host.id = "bizvision-widget";
  document.body.appendChild(host);
  const root = host.attachShadow({ mode: "open" });
  mountApp({
    root,
    api,
    config,
    lang,
    t: strings(lang),
    position,
    startOpen: script.dataset.open === "true",
    session: new Session(sessionStore, tenant),
    visitor: visitorId(localStore),
    pageUrl: () => location.href.split("#")[0] ?? location.href,
    css,
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => void init());
} else {
  void init();
}
