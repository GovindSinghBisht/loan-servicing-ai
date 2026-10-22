# blue-machines-fde-web

Customer-facing web app for the loan servicing assistant: sign in with a registered mobile number, ask
questions in a chat, confirm actions, and see accounts and open requests update alongside.

Built with Next.js (App Router), React and Tailwind CSS. It talks to the Python API in `../engine/server.py`.

## Run

```bash
npm install
npm run dev          # http://localhost:3000
```

The API must be running (see the root README). Point the app at a different API with
`NEXT_PUBLIC_API_URL` (default `http://127.0.0.1:8000`).

## What the UI shows

- **Live progress**: while the assistant works, each system lookup appears as a step ("Checking payment
  records") instead of a spinner, then the reply streams in.
- **Verifiable answers**: every reply lists the records and policy clauses it was based on.
- **Confirmations**: actions appear as a card with Confirm / Cancel; nothing is submitted until the
  customer confirms, and the card records the outcome.
- **Account panel**: loans, cards and open requests (with SLA status), refreshed after every turn.
- **Degraded mode**: if a backend system is down, the reply and the panel say which information is
  unavailable rather than failing.
- **Latency details** (header toggle): time to first word, total, model vs tool time, and per-system calls
  and retries for each reply.

## Structure

| Path | What |
|---|---|
| `src/app/page.tsx` | Session state, layout (chat + account panel, tabs on mobile) |
| `src/components/SignIn.tsx` | Mobile + OTP sign-in, demo customer shortcuts |
| `src/components/Chat.tsx` | Conversation, streaming, suggestions, confirm/cancel |
| `src/components/MessageBubble.tsx` | Reply with progress steps, sources, action card, latency |
| `src/components/AccountPanel.tsx` | Loans, cards, open requests |
| `src/lib/api.ts` | Typed API client and server-sent events parser |
