# IPEDS Query

Ask questions about U.S. colleges and universities in plain English and get
back conversational answers with tables and charts — no SQL, no spreadsheets.

It's a private, invitation-only web app for exploring **IPEDS** (the U.S.
Department of Education's annual census of colleges) across collection years
**2020‑21 through 2024‑25**: degrees awarded, enrollment, tuition and financial
aid, graduation and outcome rates, admissions, and institutional details.

> **Just want to use it?** Read the rest of this page.
> **Running or deploying it?** See [DEPLOY.md](docs/DEPLOY.md).
> **Working on the code?** See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## What you can ask

Type questions the way you'd ask a colleague. A few to get you started:

- "Top 20 institutions awarding Associate's degrees in Registered Nursing over
  the last 3 years."
- "How many Computer Science bachelor's degrees did California public
  universities award last year?"
- "Which states awarded the most Master's degrees in Education?"
- "Show me a graph of nursing degrees awarded nationally over the last 5 years."
- "In a 60‑mile radius of Columbus, Ohio, the top 5 universities graduating MBA
  students over 5 years."

You don't need to know program codes or table names — just describe what you
want. The assistant figures out the query, runs it, sanity‑checks the numbers,
and explains the answer.

## Signing in

Access is by invitation. On the sign‑in page, enter your email:

- If you've been approved, you'll get a **one‑time sign‑in link** by email — no
  password to remember. Click it and you're in for about a month.
- If you haven't been approved yet, you can **request access**, and an
  administrator will be notified.

## Using it

Ask a question in the box at the bottom and watch the answer stream in.

- **Answers** lead with the direct result, then a compact table, then a short
  note on how it was calculated. Expand **Thinking** to see the steps and the
  exact SQL the assistant ran.
- **Tables** — each result table has its own **Download CSV** button, and
  **Chart this** when the data suits a graph.
- **Charts** — switch between **Line** and **Bar**, toggle **data labels**, and
  **Copy image** to paste a chart straight into an email, doc, or slide. Charts
  paste as clean images that look right in light or dark mode.
- **Copy** a whole answer as **Markdown** or **HTML** (the HTML keeps the table
  and chart formatting when pasted into Word, Outlook, or Google Docs).
- **Edit** or **Rerun** any of your earlier prompts to refine a question — the
  new answer replaces the old one in place.
- **Conversations** are saved in the sidebar (named automatically), and you can
  delete any you don't need. Collapse the sidebar for more room.
- **👍 / 👎** on an answer tells the app which queries were good — helpful
  answers are remembered and reused to make future questions faster and more
  accurate.
- **Light or dark mode** — toggle in the top bar; your choice is remembered.

A repeat of a near‑identical question may return instantly from a cache, but the
numbers are always re‑checked against the live data.

## Data coverage & accuracy

The database contains **five collection years, 2020‑21 → 2024‑25**, covering the
main IPEDS surveys. When a new year is published, an administrator loads it and
the app picks it up automatically.

The assistant sanity‑checks magnitudes before answering (for example, ~1 million
associate's degrees are awarded nationally per year), but it's a tool, not an
oracle — for anything you'll publish or make a decision on, spot‑check the
result, and use **Download CSV** or **Thinking → SQL** to verify the underlying
numbers.

## For administrators

Signed‑in admins get an **Admin** tab:

- **Allowlist** — approve or remove people, and act on access requests.
- **Imports** — upload a new year's IPEDS file; it builds and validates in the
  background and swaps in only if the checks pass (the live data is never
  disturbed mid‑import).
- **Usage** — queries, tokens, and **spend** over a chosen time range
  (hour / day / 7 / 30 days / custom), with a chart and per‑user breakdown.
- **Skills** — review and curate the learned NL→SQL examples.
- **Logs** — recent server activity.

## Under the hood

A FastAPI backend runs an embedded, tool‑calling AI agent over a read‑only
SQLite copy of the IPEDS data; a React front end renders the chat, tables, and
charts. It's designed to be cheap to run and safe by construction — the model
can only issue read‑only queries, guarded by a timeout. Details in
[CONTRIBUTING.md](CONTRIBUTING.md) and [DEPLOY.md](docs/DEPLOY.md); the data model and
query conventions are documented in [SCHEMA.md](docs/SCHEMA.md).
