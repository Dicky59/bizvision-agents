import { ApiFailure, type Api } from "../api";
import { fmt, type Strings } from "../i18n";
import { safeHttpUrl } from "../markdown";
import type { Lang, WidgetConfig } from "../types";
import { el } from "./dom";

export interface LeadFormDeps {
  t: Strings;
  config: WidgetConfig;
  lang: Lang;
  api: Api;
  conversationId: () => string | null;
  onDone: () => void;
  onCancel: () => void;
}

export interface LeadForm {
  el: HTMLFormElement;
  /** Reset the fields and focus the first one. */
  open(): void;
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function createLeadForm(d: LeadFormDeps): LeadForm {
  const { t } = d;

  const name = el("input", {
    id: "bv-lf-name",
    name: "name",
    type: "text",
    autocomplete: "name",
    required: true,
    maxlength: "200",
    "aria-describedby": "bv-lf-name-err",
  });
  const email = el("input", {
    id: "bv-lf-email",
    name: "email",
    type: "email",
    autocomplete: "email",
    inputmode: "email",
    required: true,
    maxlength: "320",
    "aria-describedby": "bv-lf-email-err",
  });
  const phone = el("input", {
    id: "bv-lf-phone",
    name: "phone",
    type: "tel",
    autocomplete: "tel",
    maxlength: "64",
  });
  const message = el("textarea", {
    id: "bv-lf-message",
    name: "message",
    rows: "3",
    maxlength: "5000",
  });
  const consent = el("input", {
    id: "bv-lf-consent",
    name: "consent",
    type: "checkbox",
    required: true,
    "aria-describedby": "bv-lf-consent-err",
  });
  // Honeypot: real visitors never see or reach it; bots that fill every field give themselves away.
  const trap = el("input", {
    name: "website",
    type: "text",
    tabindex: "-1",
    autocomplete: "off",
  });

  const err = (id: string) => el("p", { id: `bv-lf-${id}-err`, class: "field-err" });
  const nameErr = err("name");
  const emailErr = err("email");
  const consentErr = err("consent");
  const formErr = el("p", { class: "form-err", role: "alert" });

  const field = (label: string, input: HTMLElement, id: string, error?: HTMLElement, req = false) =>
    el(
      "div",
      { class: "field" },
      el(
        "label",
        { for: id },
        label,
        req ? el("span", { class: "sr-only" }, ` (${t.required})`) : "",
      ),
      input,
      ...(error ? [error] : []),
    );

  const privacyUrl = safeHttpUrl(d.config.privacy_notice_url[d.lang] ?? "");
  const consentLabel = el("label", { for: "bv-lf-consent", class: "consent-text" });
  consentLabel.append(fmt(t.consent_pre, { company: d.config.display_name ?? d.config.name }));
  if (privacyUrl) {
    const a = el(
      "a",
      { href: privacyUrl, target: "_blank", rel: "noopener noreferrer" },
      t.consent_link,
    );
    consentLabel.append(a);
  } else {
    consentLabel.append(t.consent_link);
  }
  consentLabel.append(t.consent_post);

  const submit = el("button", { type: "submit", class: "btn btn-primary" }, t.submit);
  const cancel = el("button", { type: "button", class: "btn btn-quiet" }, t.cancel);
  cancel.addEventListener("click", () => d.onCancel());

  const form = el(
    "form",
    { class: "lead", novalidate: true, "aria-labelledby": "bv-lf-title" },
    el("h2", { id: "bv-lf-title" }, t.lead_title),
    el("p", { class: "lead-intro" }, t.lead_intro),
    field(t.field_name, name, "bv-lf-name", nameErr, true),
    field(t.field_email, email, "bv-lf-email", emailErr, true),
    field(t.field_phone, phone, "bv-lf-phone"),
    field(t.field_message, message, "bv-lf-message"),
    el("div", { class: "trap", "aria-hidden": "true" }, trap),
    el("div", { class: "consent" }, consent, consentLabel),
    consentErr,
    formErr,
    el("div", { class: "actions" }, submit, cancel),
  );

  const setError = (input: HTMLElement, box: HTMLElement, text: string) => {
    box.textContent = text;
    if (text) input.setAttribute("aria-invalid", "true");
    else input.removeAttribute("aria-invalid");
  };

  function validate(): HTMLElement | null {
    const n = name.value.trim();
    const e = email.value.trim();
    setError(name, nameErr, n ? "" : t.err_name);
    setError(email, emailErr, EMAIL_RE.test(e) ? "" : t.err_email);
    setError(consent, consentErr, consent.checked ? "" : t.err_consent);
    formErr.textContent = "";
    return [name, email, consent].find((i) => i.getAttribute("aria-invalid") === "true") ?? null;
  }

  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const bad = validate();
    if (bad) {
      bad.focus();
      return;
    }
    submit.disabled = true;
    submit.textContent = t.sending;
    const conv = d.conversationId();
    d.api
      .postLead({
        name: name.value.trim(),
        email: email.value.trim(),
        phone: phone.value.trim() || undefined,
        message: message.value.trim() || undefined,
        consent: true,
        language: d.lang,
        conversation_id: conv ?? undefined,
        website: trap.value,
      })
      .then(() => d.onDone())
      .catch((e: unknown) => {
        formErr.textContent =
          e instanceof ApiFailure && e.status === 429 ? t.err_lead_rate : t.err_lead;
      })
      .finally(() => {
        submit.disabled = false;
        submit.textContent = t.submit;
      });
  });

  return {
    el: form,
    open() {
      form.reset();
      for (const [i, b] of [
        [name, nameErr],
        [email, emailErr],
        [consent, consentErr],
      ] as const) {
        setError(i, b, "");
      }
      formErr.textContent = "";
      name.focus();
    },
  };
}
