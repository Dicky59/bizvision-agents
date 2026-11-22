export type Lang = "fi" | "en";

export interface WidgetConfig {
  name: string;
  languages: Lang[];
  default_language: Lang;
  greeting: Partial<Record<Lang, string>>;
  starter_questions: Partial<Record<Lang, string[]>>;
  launcher_label: Partial<Record<Lang, string>>;
  primary_color: string;
  position: string;
  privacy_notice_url: Partial<Record<Lang, string>>;
  privacy_notice_version: string;
  max_message_chars: number;
}

/** A cited page. `ns` are the inline [n] marker ids that resolve to it. */
export interface Source {
  ns: number[];
  title: string;
  url: string;
}

export type ChatEvent =
  | { type: "meta"; conversationId: string }
  | { type: "token"; text: string }
  | { type: "sources"; items: Source[] }
  | { type: "lead_form"; reason: string }
  | { type: "done"; messageId: string | null }
  | { type: "error"; code: string; message: string };

export interface ChatMessage {
  role: "user" | "assistant";
  text: string;
  sources: Source[];
  /** Server-provided or local error attached to this assistant turn. */
  error?: string;
}
