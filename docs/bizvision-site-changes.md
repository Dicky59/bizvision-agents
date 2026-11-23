# Instructions for the `bizvision` site repo: replace the old chatbot with the new widget

Self-contained brief for a Claude Code session working in the **bizvision** repo (Next.js on Vercel,
site at https://www.bizvision.fi). Context: the new AI assistant is a separate service at
`https://agents.bizvision.fi` (repo `bizvision-agents`). It serves one script that renders a chat
widget in a Shadow DOM and talks to the service API. The site only has to allow and embed it.
Do not add any agent logic to the site.

## Do these, in order

1. **Remove the old chatbot.**
   - Delete the old `ChatWidget` component (and its styles/assets) and every import and usage of it.
   - Delete the `api/chat` route (App Router: `app/api/chat/route.ts`, or the pages-router equivalent).
   - Remove dependencies, env vars and constants only the old chatbot used (for example an
     Anthropic/OpenAI SDK and its key reference). Do not remove anything else.
   - List the env vars you removed in the PR description: the owner will delete them in Vercel
     and revoke the old API key.

2. **Allow the widget in the Content-Security-Policy** (wherever the CSP is defined: `next.config.*`
   headers, `middleware.ts`, or `vercel.json`). Add exactly:
   - `script-src`: `https://agents.bizvision.fi`
   - `connect-src`: `https://agents.bizvision.fi`

   The widget builds its UI inside a Shadow DOM using inline `style` text. If the CSP has a
   `style-src` without `'unsafe-inline'`, check the browser console for violations after step 3;
   if styles are blocked, report it to the owner rather than loosening the policy on your own.
   Nothing else needs to be opened (no `frame-src`, no `img-src` changes expected).

3. **Embed the widget on every page** via the root layout, using `next/script`:

   ```tsx
   import Script from "next/script";

   <Script
     src="https://agents.bizvision.fi/widget.v1.js"
     data-tenant="bizvision"
     data-lang={locale}          // "fi" or "en", whichever language the current page is
     strategy="afterInteractive"
   />
   ```

   If the site's locale handling makes `data-lang` awkward, omit it: the widget falls back to
   `<html lang>`, which must then be correct (`fi` / `en`). Do not set `data-open`, `data-position`
   or `data-api` (defaults are right).

4. **Privacy notice** (Finnish at `/tietosuoja`, English at `/en/privacy`). Add a section covering the
   AI assistant, and add these processors to the processor table, with purpose, location and
   safeguards:

   | Processor | Purpose | Location |
   |---|---|---|
   | Hetzner Online GmbH | Hosting of the assistant's API server (IP addresses and conversation traffic pass through it) | Helsinki, Finland (EU) |
   | Neon (Databricks) | Database for conversations and leads | Frankfurt, Germany (AWS eu-central-1) |
   | Anthropic | Generates the assistant's answers from the visitor's message and retrieved site content | USA: transfer safeguards (SCC / DPF) |
   | OpenAI | Embeddings of site content and of the visitor's question for search | USA: transfer safeguards (SCC / DPF) |
   | Resend | Sends the lead notification email to BizVision | USA / EU: per Resend's DPA |

   Also state: what is stored (conversation text, a random visitor id, and name/email/phone/message
   only if the visitor submits the contact form with the consent box ticked), retention (conversations
   90 days; leads 365 days), purpose (answering questions and handling contact requests), and how to
   request erasure (the existing contact address). **Keep any existing processors and wording that
   are already correct.** If you are unsure about a legal statement, leave a clearly marked TODO for
   the owner rather than inventing one. Show the owner the final text: it must stay consistent with
   the widget's consent wording.
   Put the notice's version/date on the page (for example "Päivitetty 2026-11 / Updated 2026-11");
   the platform stores the version string `2026-11` with each lead, so use that exact string unless
   the owner tells you otherwise.

5. **Do not** hard-code anything else about the assistant (no API keys, no tenant keys, no origin
   lists). The tenant's allowed origins are `https://bizvision.fi` and `https://www.bizvision.fi`;
   a Vercel preview URL is **not** allowed, so the widget will show an error state on previews.
   That is expected. Verify by running the site locally/preview for CSP and layout only and
   do the functional test on production after merge.

## Verify (before opening the PR)

- `npm run build` (and the repo's lint/typecheck/test scripts) pass.
- No remaining references: search the repo for `ChatWidget`, `api/chat` and the removed SDK.
- In the browser (preview or local), DevTools console shows no CSP violations mentioning
  `agents.bizvision.fi`; the script request to `https://agents.bizvision.fi/widget.v1.js` is the only
  new network call before the visitor opens the widget.
- The page layout is unchanged (the widget is a fixed-position launcher in the bottom-right corner;
  check it does not cover the cookie banner or existing floating buttons on mobile).

## After merge (owner does these)

- On production: open the launcher, ask "Mitä palveluita tarjoatte?", get a streamed answer with
  sources; submit a test lead with consent and check it arrives at info@bizvision.fi.
- Vercel: delete the old chatbot env vars; revoke the old LLM API key.
- If something is wrong: Vercel → Deployments → Instant Rollback.
