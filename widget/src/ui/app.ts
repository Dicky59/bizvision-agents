import { ApiFailure, type Api, type ChatBody } from "../api";
import { onColor, readableOnWhite, normalizeColor } from "../color";
import { fmt, type Strings } from "../i18n";
import { renderMarkdown } from "../markdown";
import type { Session } from "../storage";
import type { ChatMessage, Lang, WidgetConfig } from "../types";
import { ICONS, el, focusables } from "./dom";
import { createLeadForm } from "./leadform";

export interface AppDeps {
  root: ShadowRoot;
  api: Api;
  config: WidgetConfig;
  lang: Lang;
  t: Strings;
  position: "bottom-right" | "bottom-left";
  startOpen: boolean;
  session: Session;
  visitor: string;
  pageUrl: () => string;
  css: string;
}

export interface App {
  open(): void;
  close(): void;
}

const MOBILE_QUERY = "(max-width: 480px)";

export function mountApp(d: AppDeps): App {
  const { t, config, lang, root } = d;
  const messages: ChatMessage[] = d.session.thread();
  let streaming = false;
  let controller: AbortController | null = null;
  let lastUserText = "";

  // ---------------------------------------------------------------- structure
  const primary = normalizeColor(config.primary_color);
  const style = el("style");
  style.textContent = d.css;

  const launcherLabel = config.launcher_label[lang] ?? t.open;
  const launcher = el(
    "button",
    {
      type: "button",
      class: "launcher",
      "aria-expanded": "false",
      "aria-controls": "bv-panel",
      "aria-label": launcherLabel,
    },
    ICONS.bubble(),
    el("span", { class: "launcher-label" }, launcherLabel),
  );

  const newChat = el(
    "button",
    { type: "button", class: "icon-btn", "aria-label": t.new_chat, title: t.new_chat },
    ICONS.restart(),
  );
  const closeBtn = el(
    "button",
    { type: "button", class: "icon-btn", "aria-label": t.close, title: t.close },
    ICONS.close(),
  );
  const contactBtn = el("button", { type: "button", class: "contact-btn" }, t.contact);

  const header = el(
    "header",
    { class: "header" },
    el(
      "div",
      { class: "heading" },
      el("h2", { id: "bv-title" }, config.name),
      el("p", {}, t.assistant_label),
    ),
    newChat,
    closeBtn,
  );

  const log = el("div", { class: "log", role: "log", "aria-label": config.name, tabindex: "0" });
  const chips = el("div", { class: "chips", role: "group", "aria-label": t.starters });
  const alertBox = el("div", { class: "alert", role: "alert", hidden: true });
  const alertText = el("span");
  const retryBtn = el("button", { type: "button", class: "btn-link" }, t.retry);
  alertBox.append(alertText, retryBtn);

  const input = el("textarea", {
    id: "bv-input",
    class: "input",
    rows: "1",
    placeholder: t.placeholder,
    "aria-label": t.message_label,
    maxlength: String(config.max_message_chars),
    enterkeyhint: "send",
  });
  const counter = el("p", { class: "counter", hidden: true });
  const sendBtn = el(
    "button",
    { type: "submit", class: "send", "aria-label": t.send, title: t.send },
    ICONS.send(),
  );
  const composer = el("form", { class: "composer" }, input, sendBtn);
  const chatView = el("div", { class: "chat-view" }, log, chips, alertBox, counter, composer);

  const lead = createLeadForm({
    t,
    config,
    lang,
    api: d.api,
    conversationId: () => d.session.conversationId(),
    onCancel: () => showChat(true),
    onDone: () => {
      messages.push({ role: "assistant", text: t.lead_ok, sources: [] });
      persist();
      showChat(false);
      renderAll();
      announce(t.lead_ok);
      input.focus();
    },
  });
  lead.el.hidden = true;
  const leadView = el("div", { class: "lead-view", hidden: true }, lead.el);

  const panel = el(
    "section",
    { id: "bv-panel", class: "panel", role: "dialog", "aria-labelledby": "bv-title", hidden: true },
    header,
    el("p", { class: "notice" }, t.ai_notice, contactBtn),
    chatView,
    leadView,
  );
  const status = el("div", { class: "sr-only", role: "status", "aria-live": "polite" });
  const wrap = el(
    "div",
    { class: `bv ${d.position === "bottom-left" ? "left" : "right"}` },
    panel,
    launcher,
    status,
  );
  wrap.style.setProperty("--bv-primary", primary);
  wrap.style.setProperty("--bv-on-primary", onColor(primary));
  wrap.style.setProperty("--bv-primary-text", readableOnWhite(primary));
  root.append(style, wrap);

  // ---------------------------------------------------------------- helpers
  const mobile = window.matchMedia(MOBILE_QUERY);
  const isOpen = () => !panel.hidden;
  const announce = (text: string) => {
    status.textContent = "";
    // Reset first so repeating the same text is announced again.
    requestAnimationFrame(() => (status.textContent = text));
  };
  const persist = () => d.session.saveThread(messages.filter((m) => m.text !== ""));

  const syncModal = () => {
    panel.setAttribute("aria-modal", mobile.matches ? "true" : "false");
    wrap.classList.toggle("mobile", mobile.matches);
  };
  syncModal();
  mobile.addEventListener("change", syncModal);

  const nearBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  const toBottom = () => {
    log.scrollTop = log.scrollHeight;
  };

  const setAlert = (text: string, retry: boolean) => {
    alertText.textContent = text;
    retryBtn.hidden = !retry;
    alertBox.hidden = text === "";
  };

  const updateCounter = () => {
    const left = config.max_message_chars - input.value.length;
    const show = left <= Math.min(200, config.max_message_chars * 0.2);
    counter.hidden = !show;
    counter.textContent = show ? fmt(t.chars_left, { n: left }) : "";
  };
  const autosize = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
  };

  // ---------------------------------------------------------------- rendering
  function bubble(m: ChatMessage, live = false): { node: HTMLElement; body: HTMLElement } {
    const mine = m.role === "user";
    const body = el("div", { class: "body" });
    if (mine) body.textContent = m.text;
    else body.append(renderMarkdown(document, m.text, m.sources, { streaming: live }));
    const node = el(
      "div",
      { class: `msg ${mine ? "msg-user" : "msg-bot"}` },
      el("span", { class: "sr-only" }, mine ? t.you : t.assistant),
      body,
    );
    if (!mine && m.sources.length > 0) {
      const list = el("ol", { class: "sources-list" });
      m.sources.forEach((s, i) => {
        const a = el(
          "a",
          { href: s.url, target: "_blank", rel: "noopener noreferrer" },
          `[${i + 1}] ${s.title}`,
        );
        list.append(el("li", {}, a));
      });
      node.append(el("div", { class: "sources" }, el("p", {}, t.sources), list));
    }
    if (m.error) node.append(el("p", { class: "msg-error" }, m.error));
    return { node, body };
  }

  function greeting(): HTMLElement {
    const text = config.greeting[lang] ?? "";
    return bubble({ role: "assistant", text, sources: [] }).node;
  }

  function renderChips() {
    chips.replaceChildren();
    const show = messages.length === 0 && !streaming;
    chips.hidden = !show;
    if (!show) return;
    for (const q of config.starter_questions[lang] ?? []) {
      const b = el("button", { type: "button", class: "chip" }, q);
      b.addEventListener("click", () => void send(q));
      chips.append(b);
    }
  }

  function renderAll() {
    const stick = nearBottom();
    log.replaceChildren(greeting(), ...messages.map((m) => bubble(m).node));
    renderChips();
    if (stick) toBottom();
  }

  // ---------------------------------------------------------------- chat flow
  function failureText(e: unknown): string {
    if (e instanceof ApiFailure) {
      if (e.status === 429) {
        return e.retryAfter
          ? fmt(t.err_rate, { minutes: Math.max(1, Math.ceil(e.retryAfter / 60)) })
          : t.err_rate_soon;
      }
      if (e.status === 413) return t.err_too_long;
      if (e.status === 403 || e.status >= 500) return t.err_unavailable;
    }
    return t.err_network;
  }

  function setStreaming(on: boolean) {
    streaming = on;
    input.disabled = false;
    sendBtn.replaceChildren(on ? ICONS.stop() : ICONS.send());
    sendBtn.setAttribute("aria-label", on ? t.stop : t.send);
    sendBtn.title = on ? t.stop : t.send;
    sendBtn.type = on ? "button" : "submit";
    log.setAttribute("aria-busy", String(on));
  }

  async function send(text: string) {
    const message = text.trim();
    if (!message || streaming) return;
    lastUserText = message;
    setAlert("", false);
    messages.push({ role: "user", text: message, sources: [] });
    const reply: ChatMessage = { role: "assistant", text: "", sources: [] };
    messages.push(reply);
    input.value = "";
    autosize();
    updateCounter();
    renderAll();
    const { node, body } = bubble(reply, true);
    log.append(node);
    toBottom();

    controller = new AbortController();
    setStreaming(true);
    let frame = 0;
    const paint = () => {
      frame = 0;
      const stick = nearBottom();
      body.replaceChildren(
        renderMarkdown(document, reply.text, reply.sources, { streaming: true }),
      );
      if (stick) toBottom();
    };
    const request: ChatBody = {
      visitor_id: d.visitor,
      message,
      conversation_id: d.session.conversationId() ?? undefined,
      page_url: d.pageUrl(),
    };
    let failed: unknown = null;
    let serverError = "";
    try {
      for await (const ev of d.api.streamChat(request, controller.signal)) {
        if (ev.type === "meta" && ev.conversationId) d.session.setConversationId(ev.conversationId);
        else if (ev.type === "token") {
          reply.text += ev.text;
          if (!frame) frame = requestAnimationFrame(paint);
        } else if (ev.type === "sources") reply.sources = ev.items;
        else if (ev.type === "lead_form") showLead();
        else if (ev.type === "error") serverError = ev.message || t.err_unavailable;
      }
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError")) failed = e;
    } finally {
      if (frame) cancelAnimationFrame(frame);
      controller = null;
      setStreaming(false);
    }

    const problem = serverError || (failed ? failureText(failed) : "");
    if (reply.text === "") {
      // Nothing to show (error, or the visitor stopped it at once): drop the empty reply.
      const i = messages.indexOf(reply);
      if (i >= 0) messages.splice(i, 1);
      if (problem) setAlert(problem, true);
    } else if (problem) {
      reply.error = problem;
    }
    persist();
    renderAll();
    if (reply.text)
      announce(fmt(t.answer_done, { text: reply.text.replace(/\[\d+(?:\s*,\s*\d+)*\]/g, "") }));
    input.focus();
  }

  // ---------------------------------------------------------------- views
  function showChat(focus: boolean) {
    leadView.hidden = true;
    lead.el.hidden = true;
    chatView.hidden = false;
    if (focus) input.focus();
  }

  function showLead() {
    if (!isOpen()) openPanel();
    chatView.hidden = true;
    leadView.hidden = false;
    lead.el.hidden = false;
    lead.open();
  }

  function openPanel() {
    panel.hidden = false;
    launcher.setAttribute("aria-expanded", "true");
    wrap.classList.add("is-open");
    renderAll();
    toBottom();
    if (!leadView.hidden) lead.open();
    else input.focus();
  }

  function closePanel() {
    controller?.abort(); // the server still finishes and stores the turn
    panel.hidden = true;
    launcher.setAttribute("aria-expanded", "false");
    wrap.classList.remove("is-open");
    launcher.focus();
  }

  // ---------------------------------------------------------------- events
  launcher.addEventListener("click", () => (isOpen() ? closePanel() : openPanel()));
  closeBtn.addEventListener("click", closePanel);
  contactBtn.addEventListener("click", showLead);
  retryBtn.addEventListener("click", () => {
    const text = lastUserText;
    setAlert("", false);
    // The failed turn left only the question: remove it so it is not shown twice.
    const last = messages[messages.length - 1];
    if (last?.role === "user" && last.text === text) messages.pop();
    void send(text);
  });
  newChat.addEventListener("click", () => {
    controller?.abort();
    messages.length = 0;
    d.session.clear();
    setAlert("", false);
    showChat(false);
    renderAll();
    input.focus();
  });

  composer.addEventListener("submit", (e) => {
    e.preventDefault();
    void send(input.value);
  });
  sendBtn.addEventListener("click", (e) => {
    if (streaming) {
      e.preventDefault();
      controller?.abort();
    }
  });
  input.addEventListener("input", () => {
    autosize();
    updateCounter();
  });
  input.addEventListener("keydown", (e) => {
    // Enter sends, Shift+Enter is a newline; never send while an IME composition is open.
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      void send(input.value);
    }
  });

  panel.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      closePanel();
      return;
    }
    // Desktop: the panel is non-modal, Tab must be able to leave it. Mobile (full screen) is modal.
    if (e.key === "Tab" && mobile.matches) {
      const items = [...focusables(panel)];
      const first = items[0];
      const last = items[items.length - 1];
      if (!first || !last) return;
      const active = root.activeElement;
      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    }
  });

  // Keep the composer above the on-screen keyboard (iOS does not resize the layout viewport).
  const vv = window.visualViewport;
  const syncViewport = () => {
    if (vv) wrap.style.setProperty("--bv-vh", `${vv.height}px`);
  };
  syncViewport();
  vv?.addEventListener("resize", syncViewport);
  vv?.addEventListener("scroll", syncViewport);

  renderAll();
  if (d.startOpen) openPanel();

  return { open: openPanel, close: closePanel };
}
