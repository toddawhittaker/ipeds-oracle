# Administering IPEDS Oracle

This guide covers the **Admin** tools. It assumes you already know how to use the
app day-to-day — if not, start with the [User guide](USER_GUIDE.md).

As an administrator you can approve and manage who has access, load new IPEDS
years, watch usage and cost, curate what the assistant has learned, and review
server logs. Everything lives under **Admin**, reachable from your account menu
(the avatar in the top-right → **Admin**).

> **Deploying** the app (Docker, configuration, email, backups) is a separate
> topic — see the **Self-hosting** section of the [README](../README.md).

---

## Contents

- [The attention badges](#the-attention-badges)
- [Users: allowlist and access requests](#users-allowlist-and-access-requests)
- [Imports: loading IPEDS years](#imports-loading-ipeds-years)
- [Usage: activity and cost](#usage-activity-and-cost)
- [Skills: what the assistant has learned](#skills-what-the-assistant-has-learned)
- [Logs](#logs)
- [Keeping up to date](#keeping-up-to-date)

---

## The attention badges

You never have to go hunting for work. A small **count badge** appears wherever
something is waiting for you:

- On your **avatar** (visible on every page, including Chat) — the total across
  all areas.
- On each **Admin section** in the nav — **Users** (pending access requests),
  **Skills** (unverified lessons), and **Logs** (new problems since you last
  looked).

![The Admin → Users screen, with attention badges on the avatar and the section nav](images/admin-users.png)

The badges update on their own, and clear as soon as you act on what they point
to. Imports and Usage never badge — there's nothing to "clear" there.

---

## Users: allowlist and access requests

The **allowlist is the sole authority on who can sign in.** The Users section has
three sub-tabs, each with its own count:

- **Current users** — everyone approved to sign in.
- **Pending requests** — people who've asked for access.
- **Blocked users** — addresses you've denied.

### Adding people

On **Current users** you can:

- **Add** a single address (with an optional note), or
- **Import from CSV** to onboard a roster at once.

Either way, the person gets a friendly *"you're approved"* email pointing them at
the sign-in page — approval itself never emails a sign-in link; people request
their own one-time link when they're ready.

Each row shows the note, whether the person is an admin, and their last sign-in.
The action buttons **promote/demote** an admin or **remove** a user. Tick the
checkboxes to act on **many rows at once** (promote, demote, or remove in bulk).

> You can't remove or demote **yourself**, and you can't remove another admin
> without demoting them first — a guard against locking everyone out.

### Approving or declining requests

**Pending requests** lists everyone waiting. **Approve** to let someone in;
**Reject** to block them.

![The Pending requests tab](images/admin-pending.png)

A rejection blocks that address **and all of its variants** (`+tag` and
letter-case forms), and a blocked address can't file new requests or reach your
inbox again. Bulk approve/reject works here too.

### Unblocking

**Blocked users** lists every denied address. Its undo control **removes the
block** — returning the address to a clean, never-requested state. That grants no
access and sends no email; the person can request access again if they wish.
(Approving a blocked address on the allowlist also lifts the block, and *does*
grant access.)

---

## Imports: loading IPEDS years

The dataset is a stack of IPEDS collection years, and you control which years are
loaded. The **Imports** tab shows a live catalog of what the U.S. Department of
Education has released.

![The Imports tab with the year catalog](images/admin-imports.png)

Each year is a card:

- **Integrated** — already loaded (and queryable).
- **Final** / **Provisional** — released and available to add; tick the ones you
  want.
- Unavailable years are shown but not selectable.

Select the years you want and **Integrate** them. Behind the scenes the app
downloads the source files, builds a **fresh copy** of the whole database in
staging, runs integrity and magnitude checks, and **atomically swaps** it in only
if the checks pass — so the live data is never disturbed mid-import, and a bad
import can't corrupt what's already there. A progress bar tracks the rebuild.

- **Remove a year** with its trashcan control — the same safe staging-and-swap
  process runs in reverse, fully offline.
- **Manual upload** — if you'd rather provide the source `.accdb` file yourself
  (for a year not in the catalog, or an air-gapped setup), expand **Manual
  upload** and drop the file in. It runs through the same checks.

Once a year is integrated, the assistant picks it up automatically — no restart.

---

## Usage: activity and cost

The **Usage** tab summarizes how the app is being used over a time range you
choose (hour / day / 7 days / 30 days / custom):

![The Usage tab: totals, a trend chart, and top users](images/admin-usage.png)

- **Totals** — queries, tokens, spend, the three cache stats below, escalations, and
  failures.
- **A trend chart** — queries, tokens, or spend over time (switch with the toggle;
  the chart has the same controls as any answer chart, including image copy).
- **Top users** — the busiest accounts, by queries, tokens, and spend.

### Where "Spend" comes from (and what to do if it reads $0)

Spend is **not** computed from a price list we maintain — it's the **actual dollar
cost the LLM provider reports for each request** (OpenRouter returns it per call),
summed over the window. That means it's always current: switch models, or the
provider changes its rates, and Spend follows automatically with nothing to update.

The catch: reporting cost this way is an **OpenRouter** feature. If you point
`LLM_BASE_URL` at a provider that doesn't return a per-request cost (DeepSeek-direct,
a self-hosted gateway, most raw OpenAI-compatible endpoints), **Spend reads $0** —
not because nothing was spent, but because nobody told the app the price. Token
counts still populate; only the dollar figure is blank. When the app detects this
(real activity, but no cost recorded and no fallback prices set), it shows a **yellow
warning** at the top of the Usage tab so a silent $0 never looks like "free." The
warning clears on its own once cost data starts arriving or you set the prices below.

To get spend back in that case, set your model's list prices in `.env` and the app
will **estimate** the cost from token counts:

```
LLM_INPUT_COST_PER_MTOK=0.27    # USD per 1,000,000 prompt (input) tokens
LLM_OUTPUT_COST_PER_MTOK=1.10   # USD per 1,000,000 completion (output) tokens
```

Leave both unset (the default) whenever the provider reports real cost — the
provider's figure always wins; the estimate only fills in when the reported cost is
0. Two caveats on the estimate: it uses the prices **you** enter, so keep them in
sync with your provider if they change (unlike the reported cost, this one *can* go
stale), and it prices every prompt token at the input rate **without** the
cached-prefix discount, so it slightly over-states spend when the **Prompt cache**
rate is high.

### The three caches (they mean different things)

The dashboard shows **three** cache figures — don't confuse them:

- **Answer cache** — a *count* of questions answered straight from the app's own
  semantic cache of past answers, with **no LLM call at all**. A repeat or
  near-repeat question is served instantly and for free.
- **Schema cache** — a *percentage* measured on the **first** model call of each
  question: how much of that call's prompt the LLM provider served from **its own
  cache**. Every request carries a large, identical block of schema instructions up
  front, and the first call is the clean signal for whether that block is being
  reused *across* questions and users. **This is the number to watch** — a healthy,
  busy deployment runs it high, and that reuse is what keeps sending the full schema
  on every request cheap.
- **Prompt cache** — the same idea as Schema cache, but *blended across every model
  call of every question* (a hard question makes several calls as the assistant
  works through the data). It's the truest **cost** figure — it reflects the actual
  billing discount — but it runs higher than Schema cache because those follow-on
  calls also reuse the growing within-question conversation, not just the schema. Use
  Prompt cache to gauge spend; use **Schema cache** to judge whether the schema
  prefix itself is being amortized.

- **Grounded figures** — a *percentage*, and the one **data-integrity** stat here
  rather than a cost one. Every answer that leads with a hero figure (the big
  typeset number above the prose) gets that number checked against the rows the
  app's own queries actually returned: it counts as grounded if the value appears
  in the data verbatim, matches at the rounding the answer displayed, or is
  correctly derived from a column (a total, an average, a percentage change, a
  share of the total). Answers with no hero figure — and answers whose figure
  isn't a number, like a leading institution's *name* — aren't counted either
  way, so a quiet range reads "—" rather than a falsely perfect 100%.

> **A rate below 100% means figures reached people that the app could not
> reproduce from its own data.** The underlying number is written by the language
> model, which transcribes it out of the query results — so a slip is possible,
> and this is the measurement that makes it visible. A one-off is worth a look; a
> persistent gap is worth reporting.

Three more integrity/telemetry stats sit alongside Grounded figures:

- **Grounded cells** — the same idea as Grounded figures, extended to the
  *results table*. Every number in a table's measure columns is checked back
  against the rows the app's own queries returned; this is the share that
  reproduce. (Rank and label columns aren't counted — only the data.) It's a
  cell-level transcription-accuracy signal for the densest block of numbers on
  screen.
- **Answer leaks** — a *count* of answers where stray formatting debris (a bit of
  raw chart/figure markup the model mis-wrapped) was **caught and removed before
  the answer shipped**. It reads how often that safety net fired, not how often
  something reached a user.
- **Exhausted** — a *count* of questions that used up the whole tool budget before
  the assistant could answer (with a `· N degraded` sub-label for the few whose
  numbers couldn't be grounded and were replaced with an honest "couldn't
  complete" message). A rising count is the signal to raise `LLM_MAX_TOOL_ITERS`.

> **Watch the Schema cache rate.** If it sits low over a range with real traffic,
> the provider isn't reusing the schema prefix and you're paying close to full price
> for it on every question. The usual cause is **routing**, explained next.

> **Routing caveat — switching models/providers blows the cache away.** Prompt
> caching lives on the provider's servers and is *node-local*: a cached prefix on
> one machine is invisible to another. If your gateway (e.g. OpenRouter) spreads
> requests across several upstream providers, the cache lapses between bursts (common
> on a quiet, low-traffic pilot), or you **change the model or `LLM_BASE_URL`**, the
> rate drops even though the prompt text is byte-for-byte identical. For steady
> reuse: keep the model stable, and pin a single provider (OpenRouter's
> `provider.order` / `only`) or talk to one provider directly. A persistently low
> rate is a signal to check your routing — not the schema.

> **Privacy by design.** Usage shows only aggregates. The **text of people's
> questions is never shown here** — that would be an attributable privacy leak.
> Use this to watch cost and load, not to read what people asked.

---

## Skills: what the assistant has learned

The assistant improves over time by keeping short **lessons** — a generalized rule
plus a worked SQL example — that it recalls when answering similar questions.

![The Skills tab, listing learned lessons](images/admin-skills.png)

Lessons are proposed automatically from two sources, and start **unverified**:
the built-in reviewer (when it catches and fixes a mistake), and a user's own
**corrective feedback** on a follow-up turn (e.g. "you should have kept the
bachelor's scope" or "you could have asked me a clarifying question") — each
lesson's "from …" tag shows which. Your job is to curate them:

- **Verify** a lesson you trust, so it's used with confidence.
- **Edit** a headline or description to sharpen it.
- **Delete** anything wrong or unhelpful.

Each lesson shows its headline, the fuller description (expandable), and a
commented SQL example, formatted and syntax-highlighted. Good, verified lessons
make future answers faster and more accurate.

---

## Logs

The **Logs** tab is a live view of recent server activity — startups, queries,
imports, email delivery, rate-limit events, and any warnings or errors.

![The Logs tab](images/admin-logs.png)

Entries are color-coded by level (INFO / WARNING / ERROR). The **Logs** attention
badge counts **problems (warnings and errors) since you last opened this tab**, so
it's easy to notice when something needs a look; opening Logs clears it and it
re-counts only later problems. It's the first place to check if a user reports
that email isn't arriving or a query behaved oddly.

---

## Keeping up to date

The **About** dialog (account menu → **About**) shows the version you're running
and the latest release available on GitHub. When a newer version exists, admins
also see a **banner in the Admin console** — "vX.Y.Z is available" with a link to
the release notes — and the same "something's waiting" count on your avatar badge.
The banner isn't dismissible: it's there until you're on the current release, so
an available update never quietly disappears.

The version check is cached, fails silently if GitHub can't be reached, and can be
turned off entirely (`UPDATE_CHECK_ENABLED=false`) if you'd rather the app make no
outbound calls to check — see the README's **Self-hosting** section for how to
update the running image.
