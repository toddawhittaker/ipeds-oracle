# The agent loop
LLM = **any OpenAI-compatible provider** (`LLM_BASE_URL`, **OpenRouter** by default,
through the shared `backend/app/llmhttp.py` transport). **`MODEL_DEFAULT` ships with
NO default and must be set** — the app is vendor-neutral on purpose, so a shipped
default would both brand it and silently route a self-hoster's traffic to a model
they never chose; `MODEL_ESCALATION` is optional (blank = never escalate) and is
reached for after repeated tool failures. A key with no model logs a CRITICAL at
boot (`main._missing_model_warning`). Run as a tool-calling agent loop wrapped in
three guards.

**Every tool call runs OFF the event loop** (`llm._dispatch`). `registry.dispatch`
→ `tools/sql.run_sql` is blocking `sqlite3` called from inside `stream_agent`, an
**async generator** — so run inline, one query holding the full 25 s
`sql_timeout_seconds` budget stalled the ENTIRE event loop: with one uvicorn
worker that is every other user's stream, the admin console and `/api/health`,
and even that turn's own already-queued `{"type":"sql"}` event couldn't flush.
`routers/chat.py` already threadpooled its blocking DB work; these two sites (the
main tool loop and the critic-correction round) were the oversight. Safe because
`run_sql` opens a FRESH connection per call and closes it in `finally`, with
`check_same_thread=False` already set, and the timeout watchdog is already its
own thread. **The one invariant: callers await these ONE AT A TIME** — the
per-request `result_sink` dict and `res.sql_log` are shared mutable state, and
sequential awaits are the whole reason there's no race, so never
`asyncio.gather` the tool calls. Trade-off, stated: SQL now shares Starlette's
default 40-worker threadpool with every sync route handler, so a burst can
saturate it — a higher ceiling, not the absence of one. Pinned by
`test_a_blocking_tool_call_does_not_stall_the_event_loop`, which FORCES the bad
branch (dispatch blocks on an Event only a concurrent asyncio task can set)
rather than timing a fast query: measured 2.39 s inline vs <1 s threadpooled.

The three guards:
- a topical **guardrail** in front (off-topic questions never reach the DB) —
  `guard.py`'s `_SYSTEM` explicitly whitelists **corrective feedback and a
  meta-critique of a prior answer's method/scope** (e.g. "you should have kept
  the bachelor's scope") as IN_SCOPE, alongside brief contextual follow-ups and a
  short answer-phrase reply to the assistant's own clarifying question (e.g.
  "bachelor's only") — load-bearing for both the clarify chips and the feedback
  distiller below, and the fix for a real regression where the gate refused a
  user's own corrective feedback as off-topic (`backend/tests/test_guard.py`);
- a deterministic SQL **linter** (`backend/app/tools/sqllint.py`) — a pre-flight check that
  flags IPEDS aggregation foot-guns (CIP-rollup / second-major double counts,
  DISTINCT-year full-scans) in the model's SQL and feeds the warning back so the
  agent self-corrects;
- a deterministic **figure-grounding check** (`backend/app/grounding.py`) — the
  answer's hero figure is the most prominent number on screen, and `_extract_figure`
  once validated only its JSON *shape*. The check reproduces the figure's value from
  the turn's **retained** `QueryResult`s — verbatim, at the figure's display
  rounding, or via the derivation menu prompt step 6(ii) asks for
  (`sum`/`mean`/`pct_change`/`diff`/`share`/`max`/`min`/`row_total`) — recording
  `exact`/`rounded`/`derived`/`ungrounded` (plus non-evidence
  `no_figure`/`unchecked`/`retry_suppressed`). Pure arithmetic (no DB/LLM/network),
  runs on every
  answer, no setting. **OBSERVE-ONLY — alters no answer, blocks nothing**; lands on
  `usage_log.figure_grounding` (migration 21) → **Grounded figures** on Admin →
  Usage (`groundedFigureRate`, vitest-pinned), whose denominator counts *only* turns
  with both a numeric figure and results to check (folding the no-figure majority in
  would peg it near 100% and destroy the signal).
  **`retry_suppressed` is outside both counts, and that correction mattered:** a
  figure the retry forced, found ungrounded and therefore WITHHELD leaves a turn
  that shipped no figure at all, so scoring it as a missed figure contradicted the
  denominator's own definition. It was recorded as plain `ungrounded` until
  #330 — **10 of the 25 ungrounded turns in the real log were suppressions**, and
  the tile read 88.2% against a true 92.5%. The count is still surfaced, as
  `figures_suppressed` → a `· N suppressed` tail on the tile (omitted at zero), so
  correcting the rate did not delete the signal: a rising number means the model is
  repeatedly being pushed into figures the data cannot support. Aggregations are barred over
  **dimension** columns (`year`/`unitid`/`cipcode`/… — `_DIMENSION_COL_RE`): `year`
  is in nearly every IPEDS result, and a real +25.0% trend once "verified" as
  `share(year)` inside tolerance. **`row_total` is the SECOND op added after a LIVE
  false `ungrounded`** (the first was `diff`): every other op aggregates DOWN a
  column, so a figure totalling ACROSS one row of a PIVOTED result — the canonical
  by-award-level breakdown, and exactly what step 6(ii) invites for a peak-year
  hero stat — had no route and read as ungrounded despite being exactly
  reproducible (observed: `324,575 — peak national nursing degrees in 2022`, the
  row-wise sum of five award-level columns). Tried LAST (weakest route, never
  displacing a verbatim cell), needs ≥2 measure columns, excludes dimension/rank
  columns, and is **figure-only** — `check_table` grades hundreds of cells, so
  widening its match surface would inflate Grounded-cells with coincidental hits.
  A kernel that cannot reproduce a CORRECT number manufactures evidence of model
  error, the most damaging way this measurement can be wrong.
  **A TRUNCATED result may not supply a column aggregate.** `run_sql` cuts at
  `sql_row_cap_model` and tells the model not to total the page; when the model
  did it anyway, the kernel recomputed that same total from those same partial
  rows and called it `derived` — corroborating the error it exists to catch. The
  rule: a route may run over a truncated result **iff its value is invariant to
  appending the rows that were cut**. Truncation drops a SUFFIX, so a value at a
  known row index is invariant (verbatim cell, hedge bound, `row_total`, the
  row-wise ops, `prev_diff`/`prev_pct_change`) and stays allowed; anything
  reading the column's EXTENT (`sum`/`mean`/`share`/`pct_change`/`diff`, and a
  `_cross_scalars` total or complement sourced FROM a cut result) refuses. The
  gate is keyed **per RESULT, never per turn** — `sql.py`/`prompt.py` tell the
  model to fix a cut ranking with a separate `SELECT SUM(...)`, so an
  untruncated sibling in the same turn must stay fully checkable; a per-turn
  form is pinned against by `..._when_a_SIBLING_is_truncated`, which fails on
  the DERIVATION (`cross`/-1 instead of `sum`/1), not on the status — a bare
  "is it derived?" assertion does not discriminate. **`max`/`min` are named in
  the rule but cannot actually refuse**: `compute("max", …)` always returns a
  value that IS a cell, so the always-allowed verbatim route matches first. That
  is correct — grounding attests REPRODUCTION, not that the model's "this is the
  maximum" reading of the number is right. It needs **no migration**:
  `to_storage` carries `truncated` (emitted only when true, so an untruncated
  blob stays byte-identical and a legacy blob still reads False), and also sets
  it when its OWN `max_rows` cut rows — a blob that lost rows is exactly as
  unsound to aggregate over, whichever layer cut them. **NOT observe-only in
  effect**: the verdict itself still alters nothing, but two existing consumers
  act on `ungrounded` — `_maybe_retry_figure` SUPPRESSES a retry-recovered
  figure and `_s5_fabricated` can degrade a tool-exhausted answer — so widening
  what lands ungrounded feeds both, and steps Grounded figures / Grounded cells
  down on truncated turns by design. Retention is the foundation: `AgentResult.results`
  keeps every call's result (in call order), where `last_result` used to overwrite.
  **The persisted-results cap really is a cap now** (`_results_for_storage`,
  `routers/chat.py`). It drops the largest results first, but that loop was
  guarded by `len(blobs) > 1` — so it was a no-op for a single result and stopped
  the moment dropping left one, and **the survivor was never measured**.
  `RESULT_STORE_MAX_BYTES` (64 KB) therefore meant "at most one result may exceed
  it, unbounded": `to_storage` caps rows (200) but not WIDTH, and one value may
  reach `SQL_MAX_VALUE_BYTES` (1 MiB), so 200 rows of a wide `SELECT *` is
  comfortably megabytes — written **twice**, into `messages.results` AND
  `query_cache.results` (whose comment reasoned from "already capped by the
  caller", which is what stopped anyone looking). Measured 2,002,125 bytes stored
  against the 64,000 ceiling. The lone survivor is now **shrunk** — halve its
  rows until it fits — rather than dropped, since it is the turn's only evidence.
  **If not even one row fits, it stores NOTHING, and that direction is the
  point**: a blob with columns and zero rows reads to grounding as "checked, and
  nothing reproduced" — an `unmatched` verdict raising the ⚠ caution on a CORRECT
  answer — while NULL reads as `unchecked` and renders silently. My first fix
  returned the zero-row blob; the test caught it. Losing rows can only cost a
  match that would have been made (a false `ungrounded`), never manufacture a
  false ✓ — the same trade the 200-row cap already makes.
  **Grounding is CONVERSATION-scoped**: each turn's results are persisted
  (`messages.results`, migration 23, capped + backend-only) and the recent window is
  re-hydrated (`_load_prior_results`, same `before_id` semantics as `_load_history`
  — but a **~2× WIDER window**, a known open question: both LIMIT `HISTORY_TURNS`,
  yet history counts ALL messages (6 ≈ 3 turns) while prior-results counts only
  result-bearing assistant rows (6 ≈ 6 turns), so grounding can borrow results
  from turns whose prose the model never saw. Narrowing is defensible in
  principle but was **measured and could not be decided** — 8 of the 9 graded
  turns in the corpus were fed identical inputs, so "no change" proved nothing —
  and shrinking the pool can only produce a FALSE caution on a correct answer.
  Needs a corpus with several 6+ turn conversations; pinned meanwhile by
  `test_the_two_recent_windows_are_measured_in_different_units`)
  into `stream_agent(prior_results=…)`. A figure is checked against THIS turn's
  results FIRST, then the borrowed prior ones (`_ground_results`), so a follow-up
  that recites a number without re-querying grounds against the earlier turn that
  produced it, tagged **`ctx:`** in `figure_derivation` (composes with `retry:` →
  `retry:ctx:pct_change(q3.x)`). Prior results are borrowed for grounding only —
  **never re-persisted** as this turn's own and **never fed to the model** (we verify
  recitation, we don't prevent it) — and this relaxes `_figure_required` to fire on a
  no-SQL turn when prior results exist. Pinned in `backend/tests/test_grounding.py` +
  `test_agent_loop.py` + `test_chat_router.py`.
- a deterministic **table-grounding check** (`grounding.check_table`, same module,
  also **OBSERVE-ONLY**) — the results **table** is the model re-typing the query
  rows one-for-one, the densest block of numbers on screen. It parses the answer's
  GFM tables (`parse_markdown_tables`, header kept, skipping ```` ``` ````-fenced
  regions so a ```chart isn't read as a table) and grades the **MEASURE columns
  only** — `_is_measure_column` excludes a **rank ordinal** (a pure 1..N sequence,
  whatever the header) and any **dimension** column (`is_dimension`:
  rank/year/unitid/cipcode/id/…), so a model-added Rank column that was never in the
  DB can't drag the rate down. Each graded cell is reconciled **CONVERSATION-scoped,
  mirroring the figure**: against this turn's results borrowed with the recent window
  (`_ground_results`/`prior_results`, the same #166 infra), so a follow-up that
  RESHAPES an earlier table (transpose/regroup, no SQL of its own) is VERIFIED
  against the borrowed base rows, and a corrupted reshape is caught. Reconciliation
  uses the shared `_reconcile_value` kernel (verbatim / display-rounded / derivable)
  but with **`allow_dimension=False`**: a measure cell is verified only by a MEASURE
  result-column, never a code/dimension column it merely collides with (a small
  count "3" must not ground against an `awlevel` 3 — the figure path keeps
  `allow_dimension=True`, since a headline can legitimately BE a year/code).
  **A table row is ANCHORED to the result row it describes** (`_anchor_row`), and
  graded against that row alone. This replaced a column-wide search that was wrong
  in BOTH directions, and one mechanism fixed both:
  **(a) false negatives** — every op ran DOWN a column, so a row-wise `% change`
  column (`(2024-2021)/2021` for *that row*) had no route and a CORRECT table graded
  `partial`, or `unmatched` when such a column was its only measure. That
  measurement is why the reader-facing mark is positive-only.
  **(b) false positives** — measured on the retained corpus, scaling every number in
  eight real answers by 1.2–1.9× still left **24.0%** of cells "grounded"
  (2142/8920), 34% on the widest turn; 878 of those were plain `exact` hits on a
  `total_degrees` column holding **506 values across three results**, where "somewhere
  in the column" is nearly free. After anchoring: **0.63%** (56/8920), with real cells
  unchanged at 446/446.
  **Two cell FORMATS are handled, both found by driving live questions and
  reading the cautions** (neither was visible to review, and each turned a CORRECT
  answer into a warning): **(1) Markdown emphasis** — `parse_number` strips
  `**bold**`/`` `code` ``/`*italic*` (`_EMPHASIS_RE`). Without it such a cell failed
  to parse and was DROPPED — never counted, never checked; 7 of 14 numeric cells in
  one live answer escaped because the model bolded them, which is its own convention
  for the numbers that matter most, so the ✓ mark undercounted while sounding
  authoritative. **(2) Hedged cells** — `<0.1%`/`≥5` state a BOUND, so
  `parse_hedge`/`satisfies_hedge` test the INEQUALITY instead of the digits; reading
  `<0.1%` as the quantity `0.1` compared it against a true 0.0179% and called a
  correct hedge a miss. A bound is deliberately weaker evidence than an equality —
  that asymmetry is the honest reading of what the model claimed, not a loosened
  tolerance, and a bound nothing satisfies still fails.
  **CROSS-RESULT derivations** (`_cross_scalars`/`_match_cross_result`) close the
  last live gap: the model routinely takes rows from one query and the denominator
  from a second `SELECT SUM(...)`, so every share was one result's row over another
  result's scalar and nothing could reproduce it. Observed on an ordinary question
  — all eight unreproduced cells AND the hero figure were exact
  (`11,620/45,883 = 25.3%`, `45,883-30,568 = 15,315`). The ingredient is a TOTAL:
  every measure column's sum from any result, plus pairwise **complements** ("all
  others" is the other half of every share breakdown *and* the numerator of the
  next share). It is the WIDEST search in the module and runs absolutely LAST, with
  two precision guards that are **individually pinned because the aggregate probe
  cannot see them**: a share must be **written with a `%`** (the marker splits the
  two routes — unsplit, one answer offered 11 totals + 55 complements to every cell
  and fabricated grounds went **0.9% → 10.4%**), and a share must land **in
  (0,100]**. Applies to the FIGURE too, so Grounded figures moves — correctly: it
  was reporting a false `ungrounded` on a right answer.
  **The complement count is a CEILING, never an on/off switch** (#332). It used to
  be `if len(totals) <= 8: build all the pairs` — a cliff, not a bound: 8 totals
  yielded all 28 pairs and 9 yielded ZERO, so the widest route in the module
  silently switched off on exactly the result-rich turns where a share and its
  complement are most likely. Measured: **20% of turns with retained results (17 of
  84) were over that line** — conversation-scoped grounding makes it ordinary, since
  the prior-results window alone can carry six results before this turn's own, and
  the `45,883-30,568 = 15,315` example above is the shape that stopped grounding.
  Now `_MAX_COMPLEMENTS` (6) caps the OUTPUT, with **same-result pairs ordered
  first** (a share and its complement almost always come from one query). The
  ceiling is small because it was swept, not out of caution: recall plateaus at 3
  while fabricated cells climb monotonically (3→28, 6→30, 12→33, 28→38 of 1,715),
  so "keep every pair the old 8-total turns had" preserves the old NOISE and is the
  worst option. The sweep table is in the code — move it with data, not taste.
  Anchoring scores (label matches, numeric matches) and returns the **GROUP** of
  rows tied at the best score — not a unique winner. A **PIVOTED** table row
  legitimately describes several result rows at once (one row per year, one
  column per category), so demanding uniqueness was backwards and the two halves
  compounded: the result actually holding all the numbers tied N ways and was
  REFUSED as ambiguous, a SUPERSEDED result matched one row and anchored
  UNIQUELY, and because *something* anchored the right result was never
  consulted. Measured live (conversation 23): a table whose every number was
  correct and present graded **5/15 `partial`** — a ⚠ on correct work, the one
  thing the caution must never do. Grouping is bounded by `_MAX_ANCHOR_GROUP`
  (12): a group spanning most of the result is the unrestricted column search
  under another name. **Measured both ways on the retained corpus: recall
  83.3% → 98.0%, fabricated-ground rate UNCHANGED at 1.33%** — two false
  cautions removed (msgs 108 and 100, the latter the long-tracked "pivot gap")
  with no precision cost; it is in fact TIGHTER for pivot rows, which used to
  fall through to the column-wide search. The regression test carries a DECOY
  superseded result on purpose — without it the case passes with the bug still
  present. Anchoring still needs a label or ≥2 numeric matches, and
  compares numbers by **IDENTITY, never `_close()`** — a relative tolerance made
  adjacent years indistinguishable (2023 is within 0.1% of 2021/2022/2024/2025), tying
  every row of a by-year result and DROPPING correct cells.
  It scores **DISTINCT** values, not a list (#333): counting a repeated value twice
  is double-counting EVIDENCE, not stronger evidence, and with the tie-only grouping
  it actively evicted other entities. Live case — `| IN | 2,475 | MT | 67 | AK | 67 |`
  gave Montana and Alaska `(1 label, 2 numbers)` from the two 67s while Indiana
  scored `(1,1)` and was dropped, so a correct 2,475 was graded against the wrong
  rows and the table read `partial 2/3`: a ⚠ on numbers that were all right. #331
  stopped the model emitting multi-entity rows, so this is now defence in depth.
  An unanchorable row (a
  `Total` line, a reshape) falls back to the old unrestricted search, so those keep
  grounding as before. An anchored cell may use: its own row's cells; row-wise
  `sum`/`pct_change`/`diff`/`mean`/`share` (**the fix for (a)**); `prev_diff`/
  `prev_pct_change` against the PREVIOUS row (a "% vs prior year" column — a SECOND
  blind spot of the same class, found by probing the fix, which graded 3/6); and
  column `sum`/`mean`/`share`-at-this-row. **`max`/`min` are deliberately barred** —
  the row legitimately holding the column max grounds via its own cell, so they add no
  recall while re-admitting the likeliest real error (copying the top row's number
  down a column). Costs ~2× runtime (45→106 ms on the widest real turn), all of it
  off the LLM critical path. Records a per-turn
  status (`matched`/`partial`/`unmatched`/`no_table`/`unchecked` — the last means
  neither this turn nor the window retained anything) + numeric-cell counts on
  `usage_log.table_grounding`/`table_cells_checked`/`table_cells_matched`
  (**migration 25**; `no_table`/`unchecked` carry 0 counts so they self-exclude from
  the SUM-based rate) → a cell-level **Grounded cells** stat on Admin → Usage
  (`groundedTableRate`, vitest-pinned). Stamped in `llm.py`
  (`_stamp_table_grounding`) right after the figure stamp on BOTH terminators, on the
  FINAL settled answer. Pinned in `test_grounding.py` + `test_admin_router.py` +
  `test_migrations.py`.
  **The verdict is also shown to the READER** (the table's counterpart to the
  figure's ✓): status + counts persist on `messages.table_grounding`/
  `table_cells_checked`/`table_cells_matched` (**migration 33**) and ride the `done`
  SSE event, so `Chat.jsx`'s `TableTrust` renders one **answer-level** line —
  `✓ 40 values reproduced from the query result` — as a sibling AFTER `<Markdown>`,
  outside the `.md` copy surface (same rule as `<Figure>`). **ANSWER-scoped, not
  per-table**: `check_table` returns ONE verdict for every table in the answer, so
  attaching it to a particular table would mis-attribute it — which is also why it
  needs no single-table gate (unlike the truncation caption, whose flag maps to one
  query result). Wording rules in the pure `tabletruth.js` (`tableTrustNote`,
  vitest): state the **count, never "all"** (measure columns only were graded), and
  promise **reproduction, not correctness**. **TWO-SIDED since the reconciler was
  anchored:** `partial`/`unmatched` render a **⚠ caution** in `--warn`
  (`.table-trust.warn`, an inline `IconWarning` — the ⚠ codepoint renders as a colour
  emoji on some platforms) reading **`Check 13 of 22 values against the SQL or CSV`**.
  **It is phrased as an INSTRUCTION, not a verdict, and that is the whole design.**
  Every time it fired on real data it was a gap in the CHECKER, not a model error —
  bolded numbers, a `<0.1%` read as `0.1`, a cross-query share, a header mistaken for
  an ID: four correct answers flagged. A line claiming the numbers "could not be
  reproduced" reads as *don't trust these* and attacks work that was fine, and a
  warning that is usually wrong teaches people to ignore it — costing exactly the day
  it is finally right. An instruction survives being wrong: the reader looks, sees the
  numbers are fine, and has lost ten seconds. Both destinations are real controls on
  the same answer (the SQL disclosure below it, the CSV export on the table).
  **Don't reword it into a claim about the numbers unless the false-alarm rate has
  been measured at zero.** It also must not borrow the `--danger` treatment of a
  genuinely failed turn; the answer is still an answer.
  **BORROWED evidence says so.** Grounding is conversation-scoped, so a turn that
  reshapes an earlier table runs no SQL and is checked against THAT turn's rows —
  deliberate, and the only reason a transpose verifies at all. But the note read
  "reproduced from **the** query result" on an answer whose `sql_log` is `[]`,
  sending anyone who wanted to check to a SQL disclosure that isn't there (found
  live; it made a CORRECT ✓ look suspect). `hasSql` (from `m.sql_log`) now picks
  the source clause: "the **earlier** query result", and the caution points at
  "the **earlier answer's** SQL or CSV" — the destinations have to EXIST, and on
  a reshape the CSV button exports only the transcribed rows anyway (see
  `Markdown.jsx`'s `hasSql` gate). Same claim, different source; only the source
  clause changes. Pinned in `tabletruth.test.js` + a `table-grounding.spec.js`
  case that fails if the prop is dropped in `Chat.jsx` — the plumbing is the part
  that silently regresses. **`unchecked`/`no_table` stay SILENT** (nothing was
  compared, so neither tone applies), and so does any failure verdict whose counts
  are missing or contradict it: **`Number(null)` is `0` and finite**, so a
  pre-migration row's NULL counts read as "0 of N matched" and manufactured a caution
  against an answer nothing ever graded — the same trap `years.js` hit, caught here
  by a test, not by review. `table_cells_matched` had to be plumbed onto the live
  turn (the `done` event carried it; `Chat.jsx` dropped it). A
  cache hit shows NEITHER mark (it passes no grounding, like
  `figure_grounding`). Pinned in `frontend/e2e/table-grounding.spec.js` (incl. a
  direct contrast assertion — the axe scan never renders this element, and light
  theme clears AA by only ~0.07) + `tabletruth.test.js` + `test_chat_router.py`.
- a post-answer **critic** that can force one revision round. **It is given the
  actual result rows** (capped, via `QueryResult.to_markdown`, with a truncation
  flag) — without them it saw only the SQL *text* and the prose, so it could
  judge whether a query looked right but never whether the answer's numbers were
  in the data. The revision only
  ships if the model **re-queried AND changed the answer AND its prose carries no
  reviewer-directed meta** (`_leaks_review_meta` in `llm.py` matches
  "reviewer"/"the review"); otherwise the clean pre-critique draft is re-emitted,
  `critic_revised=False`. This closes the observed leak where a *confirm*-by-
  requery rebuttal (same number, new "the reviewer's concern…" prose) slipped
  past the requeried-and-changed gate — see `backend/tests/test_critic.py`.
  **Only SOME findings may become lessons, and the prompt never says which.**
  The REVISE reply carries a **`CATEGORY:`** from the closed seven-token set in
  **`backend/app/lessoncats.py`** (a dependency-free leaf module, `seeds.py`'s
  precedent — three modules need the enum and `skills.py` reaching it via
  `critic.py` would drag `httpx` into the skills import graph for a constant).
  Five data-modeling categories are LEARNABLE; **`UNGROUNDED_NUMBER`** and
  **`OTHER`** are not. The first IS the class Todd kept rejecting in production —
  "verify figures against the query result before emitting them" — which
  `grounding.py` already enforces deterministically per turn, so a lesson
  retrieved at query time cannot fix it. The second is excluded because it would
  otherwise be the **escape hatch**: a model whose `UNGROUNDED_NUMBER` findings
  are discarded would simply relabel `OTHER`, making the gate a one-hop detour
  rather than a fence. **No parseable category → no lesson** (fail closed); the
  cost is that a genuinely novel insight fitting no bullet is never learned,
  accepted because adding a bullet is a one-line change.
  **The gate is CATEGORICAL because similarity provably cannot do it** —
  measured with the app's own model: five phrasings of the rejected class sit at
  cosine **0.625–0.802** to each other while two genuinely different legitimate
  lessons sit at **0.673**, best separation **0.703 vs 0.681**, i.e. none. Don't
  re-derive that by trying an embedding filter; it's in `lessoncats.py`'s
  docstring.
  **THE REVISE STILL FIRES FOR EVERY CATEGORY** — only the *learning* is gated,
  and an `UNGROUNDED_NUMBER` finding must still force its revision round, the one
  thing `grounding.py` can't do alone (make the model re-query and fix the number
  before the user sees it). "The critic no longer handles X" is exactly the wrong
  summary to act on.
  `_SYSTEM`'s bullets are **assembled from `lessoncats.BULLETS`** so prompt and
  enum can't drift, and its old closing line ("…AND stored as a learned lesson")
  is **DELETED** — once categories gate storage that sentence invites relabeling.
  The prompt now never uses the word "lesson" nor reveals the learnable set,
  pinned by a **negative** test. Two bugs found on the way, both invisible to
  review: `_DESCRIPTION_RE` stopped only at a following `headline:`, so a
  DESCRIPTION-before-CATEGORY reply swallowed the literal `CATEGORY: X` into the
  stored description (surfaced through `test_feedback.py`, since `feedback.py`
  reuses `parse_verdict` — which keeps its **exact 3-tuple**, that suite passing
  untouched being the behaviour-neutrality signal); and the critic-lesson
  recording call was a bare `await` inside the SSE generator **after** the answer
  is persisted with no `try/except`, while its feedback sibling has always been
  guarded. `critic_category` must be set at **BOTH** `llm.py` critic call sites
  (main loop AND exhaustion path) — missing the second fails closed and silently.
  **The critic also runs on the TOOL-BUDGET-EXHAUSTED path** (S5): when the agent
  burns all `llm_max_tool_iters` and falls back to the tools-disabled "best effort"
  synthesis (the highest-risk path, once shipped with ZERO review), it now gets the
  same critic — and on a REVISE a **bounded correction round with tools RE-ENABLED**
  (`_CRITIC_CORRECTION_ITERS=3`, a capped exception fired only by a REVISE) so a
  flagged aggregation error can actually be re-queried and fixed. The SAME anti-leak
  gate applies (a rebuttal or confirm-only re-query reverts to the clean draft).
  The exhaustion path also carries a deterministic **GROUNDING GATE and a raised
  ceiling** (measured from a real fabrication — a whole 0/15-cell table invented at
  the old cap): **(1)** `llm_max_tool_iters` **defaults to 20** (`LLM_MAX_TOOL_ITERS`,
  was 12) — a genuine multi-table question needs ~15-17 rounds, and cutting off
  mid-progress is what forced the confabulation; higher only costs on hard turns,
  each reusing the cached prefix. **(2)** After the synthesis + critic + grounding
  stamps, `_s5_fabricated(res)` degrades the answer to an honest
  **`_EXHAUSTION_DEGRADE`** message (dropping any fabricated figure/chips) when its
  numbers are WHOLLY ungrounded (`table_grounding=unmatched`, or an `ungrounded`
  figure with no grounded table); a `partial`/`no_table`/`unchecked` answer is left
  alone. **S5-only** on purpose — the normal path keeps shipping first-pass
  ungrounded figures observe-only (#163); acting on the verdict is scoped to the
  highest-risk path (a sibling to `retry:suppressed`). **(3)** `_strip_tool_markup`
  scrubs leaked pseudo-XML tool-call markup (`<｜｜DSML｜｜tool_calls>…`, emitted by
  some model families instead of the API's tool_calls field) from BOTH
  terminators. Exhaustion is recorded on `usage_log.exhaustion` (**migration 27**:
  `answered`/`degraded`/NULL) → the **Exhausted** stat on Admin → Usage
  (`exhaustionLabel`, `· N degraded` breakdown). Pinned by the `S5:`/`S5 gate:` cases
  in `test_agent_loop.py` + `test_admin_router.py` + `test_migrations.py`.
- **A STRANDED critic revision is not exhaustion.** Two different failures used to
  land in that same tail. The critic `continue`s for a revision round; if that round
  never returns a tool-call-free reply — it fired on the **last** iteration, or it
  burned every remaining iteration on tool calls — the loop ends with `draft_answer`
  set and the settle gate (which lives *inside* the terminator) never runs. The tail
  then skipped its own critic (`not critiqued` is False) and applied
  **no `_leaks_review_meta`**, shipping the revision round's reviewer-rebuttal prose
  verbatim: the PR #43 [[critic-revision-leak]] regression, reintroduced through a
  door that forgot the gate. Now: `res.exhausted = not draft_answer`, and a stranded
  draft **ships the clean pre-critique answer, skipping the synthesis call entirely**
  — that call passes `tools=None`, so a revision could never re-query, so `requeried`
  is False *by construction* and the gate could only ever revert to the draft; the
  call is guaranteed-wasted and its only novel output is the leak. The `_s5_fabricated`
  degrade is gated on `res.exhausted` so a reviewed draft is never replaced by
  `_EXHAUSTION_DEGRADE`. The settle gate itself now lives in ONE place
  (`_settle_revision`), called by both terminators — it existing twice is how they
  drifted. Accepted: a stranded draft skips `_maybe_retry_figure`. Note the metric
  narrowing — a revision round that genuinely burned the budget no longer counts as
  Exhausted; that's deliberate (overloading the flag is what corrupted it), and a
  separate `critic_unsettled` counter is the follow-up if the rate matters. Pinned by
  the `[[critic-stranded-revision]]` cases in `test_agent_loop.py`.
- **Both terminators are ONE function now: `_finalize_answer`.** The normal
  no-tool-call path and the S5 exhaustion/stranded tail ran the same settle
  sequence inline — normalize → extract figure/suggestions → grounding stamps →
  scrub → emit — and had already drifted **twice**, the second time into the #205
  P0 above. The failure mode is a difference that exists only as a **missing
  line**, invisible in review. Every real difference is now a **named flag with a
  reason**: `allow_figure_retry` (normal path only — a tools-disabled S5 synthesis
  could not have grounded a recovered figure) and `allow_degrade` (S5 only, and
  additionally gated on `res.exhausted`, so a reviewed stranded draft is never
  replaced by `_EXHAUSTION_DEGRADE`). Both flags are **proven load-bearing**:
  flipping `allow_figure_retry` off fails the three retry contracts, flipping
  `allow_degrade` off fails the S5 gate contract. The terminal events live in
  `_final_events` (a plain generator — only the async generator itself can yield
  into the stream). `res.model_used`/`results`/`last_result` stay at the call
  sites: they describe the calling context, not the settle. Behaviour-neutral —
  `test_agent_loop.py`/`test_critic.py`/`test_grounding.py` pass **untouched**,
  and the NL→SQL eval stayed 3/3 with no escalation.
- **Structured emission** (`config.structured_emission_enabled`, **DEFAULTS ON**;
  validated 100%-structured / 0-leaks across four vendors). The durable,
  model-agnostic fix for mangled fences: instead of free-typing
  ```figure/```chart/```followups/```clarify fences, the model FINISHES a turn by
  calling an **`emit_answer`** (or **`ask_clarification`**) tool whose fields the
  *provider* validates. `llm.py` intercepts that call and **reconstructs
  WELL-FORMED fences from the validated args** (`_reconstruct_answer` + `_fence` —
  the SERVER writes them, so they always parse), then falls into the SAME
  no-tool-call terminator, leaving `_extract_*` / critic / grounding / retry /
  persistence AND the frontend unchanged. A tool-incapable model falls back to the
  fence path (the retained fallback; set the flag false to force it).
  **Forced re-emit — the structured-emission GUARANTEE:** when a turn free-types
  the terminal answer under structured mode, `_forced_emit` makes ONE
  **reasoning-off** follow-up call that FORCES `emit_answer`
  (`tool_choice:{function:emit_answer}` + `reasoning:{enabled:false}`), so the
  figure/chart come back as validated args (no fence to mangle → no leak, and the
  figure SHIPS). Reasoning-off is REQUIRED: forcing a specific function is rejected
  while thinking is enabled (400 *"Thinking mode does not support this
  tool_choice"*), and the draft turn already did the reasoning. It **FAILS OPEN** →
  `_forced_emit` returns None → the **`_EMIT_REPROMPT` nudge** + fence path. Bounded
  once per turn (`emit_reprompted`). **Clarify is handled FIRST** — a single-function
  `tool_choice` can't target "emit_answer OR ask_clarification", so forcing emit must
  never clobber a clarification. Records `emit_mode="forced"` (counts as structured;
  measures how often the force was NEEDED). `chat_completion` gained per-call
  `tool_choice`/`reasoning` overrides for this.
  A **leak scrubber** (`_scrub_leaked_blocks`) runs on the FINAL answer of both
  terminal paths and STRIPS any residual figure/chart-shaped JSON a mangled fence
  left in the prose — **whatever the wrapping**, keyed off the object SHAPE (figure =
  `value`+`label`, chart = `type`+`data`), so a novel mangle is caught too; a proper
  ```chart fence is preserved (fenced segments skipped whole). The fence-path
  fallback fires ~30% of the time live on a cheap/fast model, so this net matters in
  practice. `usage_log.answer_leaked` records debris **caught and removed** (never
  shipped); with `emit_mode` (structured|fence, migration 24) it drives the
  **Answer-leaks** scrub-rate stat on Admin → Usage (`leakRate`/`leakLabel`). Clarify
  paths are NOT scrubbed (no figure/chart by contract). The **number stays
  model-supplied** (envelope only); server-computed figures from declared provenance
  is the next step. Pinned in `test_agent_loop.py` + `test_llmhttp.py` +
  `test_admin_router.py` + `test_migrations.py`.
- **Disambiguation (clarify).** Prompt INSTRUCTIONS' leading "Before you answer"
  step: when a plausible alternate reading would change the HEADLINE result (e.g.
  "which major produces the most graduates?" — bachelor's-only vs. all award
  levels can crown a different program), the model does NOT query — it asks ONE
  short clarifying question and emits a ```clarify `{"question":"...",
  "options":["<short phrase>",...]}` fence (2–4 SHORT answer phrases, not
  restated questions). `llm.py`'s `_extract_clarify` parses + ALWAYS strips the
  fence (mirrors `_extract_figure`), and when a clarify is found `stream_agent`
  yields `{"type":"clarify",…}` then the answer, sets NO figure/suggestions, and
  **skips the critic entirely** — a clarify turn has no data claim to
  sanity-check. Persisted on `messages.clarify` (migration 20) so a reload shows
  the same question + chips; deliberately **no `query_cache.clarify` column** — a
  clarify turn is **never cached** and **records no critic lesson**
  (`chat.py` guards both on `clarify is None`). Frontend: `Clarify.jsx` (pure
  `clarify.js` normalizer, vitest) renders the answer-phrase chips
  structurally identical to `Suggestions.jsx` but with a **louder accent-FILLED
  treatment** (UX-H2: `.clarify` chips are accent-tinted/filled, the label in the
  accent color) — a clarify is a REQUIRED decision that blocks the answer, not the
  optional "you might also ask" exploration the identical outline chip read as; the
  distinction is shape+fill, not colour alone (the "Did you mean" heading already
  differs). Clicking one — or just typing a free-text reply in the composer, always
  the escape hatch — submits it as an ordinary follow-up turn. When ambiguity is NOT material, the prompt instead
  has the model answer under the most reasonable assumption, name it in the
  method line, and offer the alternate reading as a `followups` chip; a scope
  established earlier in the thread (award level, year range, institution/state
  set, program grouping) carries forward on later turns unless the user changes
  it. Pinned in `frontend/e2e/clarify.spec.js` + `backend/tests/test_agent_loop.py`
  / `test_chat_router.py` / `test_migrations.py`.
- The **signature "figure"** — a typeset hero statistic (mono caption · big serif
  number · ochre rule · mono source) rendered ABOVE an answer. Prompt INSTRUCTIONS
  **step 6** leads with a figure on BOTH kinds of answer (the trigger is prompt-only;
  no code gates the figure by query type). **(i)** When the answer's headline IS a
  single number, it builds the full **BRIEF**: (a) the ```figure fence, (b) a 1–2
  sentence synopsis, (c) a recent-years breakdown table (constant-bound `year >
  (SELECT MAX(year)-5 …)`), and (d) a ```chart trend — the story behind the number,
  not just one point. **(ii)** When the answer is a **trend / ranking / top-N list /
  multi-row comparison** (which already carries its own table/chart), it STILL leads
  with a figure carrying a **derived** hero stat + one insight sentence — a net %
  change over a time range, a leader's value or its share of the total, an average, or
  a max/min — chosen to fit the query; no second table/trend is bolted on. The figure
  is **omitted only** when no single number honestly summarizes the result (a plain
  lookup — address/URL/accreditor — or a tiny two-row fact). The model emits a
  ```figure `{value,unit?,label,source?}` fence; **`llm.py`'s `_extract_figure`
  parses it out server-side, ALWAYS strips every figure fence from the prose (so raw
  JSON never reaches the user, even on a parse error), and — only for valid JSON with
  value+label — sets `AgentResult.figure` and yields a `{"type":"figure",…}` SSE
  event**. Parsed AFTER the critic's revert settles `answer`, so the figure always
  matches the winning prose. Persisted in `messages.figure` (migration 13) and the
  answer cache `query_cache.figure` (migration 14) so it survives reload AND a
  cache-hit repeat — mirroring `sql_log`/`thinking`. Frontend: a structured `figure`
  message field (not scraped) → `Figure.jsx` (pure `figure.js` normalizer, vitest)
  renders it as a sibling BEFORE `<Markdown>` in the assistant bubble — above the
  prose and OUTSIDE the `.md` copy surface — reusing the Reading-Room `.figure`/
  `.fig-rule`/`.field-label` device (the same primitive the Login "door" uses).
  (`_extract_figure` accepts BOTH the ```figure fence AND an HTML `<figure>` tag —
  some models emit the latter.) The brief applies on **follow-up turns too**, but
  prompt wording alone can't carry it: figure emission **decays with conversation
  DEPTH** — the system prompt must stay FIRST to remain the cacheable prefix, so its
  rules sit behind ever more history, and reword/compress/model-swap experiments all
  under-delivered (a compressed step 6 even broke the FORMAT — correct JSON,
  mis-wrapped). The fix is STRUCTURAL — three guards in `llm.py`:
  (1) **`_TURN_REMINDER`** — a short pointer back to steps 6/7 injected as a
  `system` message **after the history and immediately before the question**, on
  follow-up turns only. Built per request, never persisted, and it must never move
  ahead of the system prefix (that collapses cache reuse) — pinned by
  `test_followup_turn_gets_a_tail_reminder_after_the_cached_prefix`.
  (2) `_extract_figure`'s **mis-wrap fallback** (recovers a bare `{value,label}`
  object at the answer's HEAD, behind an optional stray `[..](..)`; head-scoped so a
  ```chart fence or mid-prose object is never mistaken for a figure) plus
  **`_normalize_misfenced_blocks`** (runs BEFORE extraction; repairs a figure/chart
  emitted as MARKDOWN IMAGE syntax — `![figure]\n{json}` — into real
  ```figure/```chart fences via a balanced-brace scan, firing only when the label is
  followed by a JSON object, so a genuine `![alt](image.png)` is untouched).
  Otherwise that raw JSON leaks (charts have no other net) and can DUPLICATE a
  retry-recovered figure. Pinned in `test_agent_loop.py`.
  (3) A **missing-figure retry** (`retry_missing_figure` + `_maybe_retry_figure`,
  gated `FIGURE_RETRY_ENABLED`, modeled on the critic: own call, fails open): when a
  data-backed answer that should lead with a figure emits none (`_figure_required` —
  has SQL, has a digit, no clarify/error, OR a no-SQL turn with prior results to
  ground against), ONE targeted call asks for ONLY the ```figure fence — a narrower
  ask than re-obeying step 6, which is why it works. A recovered figure is
  **grounded before it ships**: reproducible → kept, derivation tagged **`retry:`**;
  **ungrounded → SUPPRESSED** (`retry:suppressed`) — a forced figure not in the data
  is an induced hallucination, the ONE place a figure is suppressed rather than
  shipped (first-pass ungrounded figures still ship observe-only, #163). **If you
  touch step 6, the reminder, or the retry, re-measure `figure_grounding` before and
  after** — emission is prompt-compliance behaviour and prompt fixes have repeatedly
  under-delivered; `retry:`-prefixed derivations in `usage_log` mark what the retry
  recovered. A brief's
  **table + trend chart render side by side** (`briefdata.js` pairs one-table +
  one-chart → `Markdown.jsx` passes the chart into the table component and suppresses
  the standalone fence; drops the redundant "Chart this"). To hand the chart room,
  the side-by-side table is **capped** (`.brief-figrow:not(.stacked) .table-block {
  max-width: min(360px,100%) }`, `overflow-x: visible`) so a wide table **shrinks and
  WRAPS its multi-word headers** (`.md th` wrapping; data cells stay nowrap) instead
  of taking full width — a `flex`/max-width-on-cell alone won't force this when the
  row has room. `.brief-figrow` **wraps to stacked on a narrow viewport**, AND a
  **wider or taller table (`headers.length > 3 || rows.length > 8`) is forced
  `.stacked`** — chart BELOW the full-width table, since a bigger table can't share a
  row without its nowrap cells sliding UNDER the chart (only the brief's compact
  recent-years strip — a couple of columns, a handful of rows — sits side-by-side;
  the earlier `> 4`-columns-only threshold let a 4-column ranking table overlap the
  chart). Pinned in `frontend/e2e/answer-figure.spec.js`.
  **A reproduced figure is marked "✓ verified"** on its source line (S6). The
  server already graded every figure, but the verdict lived only on `usage_log`,
  so the person reading the number learned nothing. `messages.figure_grounding`
  (migration 31, STATUS only) + the `done` SSE event carry it; `figure.js`'s
  vitest-pinned `isFigureVerified` decides, and `Figure.jsx` renders the mark
  (in the `aria-label` too — a sighted-only trust signal would be the wrong kind
  of quiet). **POSITIVE-ONLY BY DESIGN, and the asymmetry is the contract**: an
  ungrounded figure renders NO mark and NO warning. The kernel is observe-only
  precisely because it has produced false negatives (#212 was a CORRECT figure
  graded `ungrounded`), and a missing mark costs a little trust while a warning on
  a correct number destroys it. **Don't confuse `figure_grounding` with
  `figure_derivation`**: the former is only ever a BARE status
  (`exact`/`rounded`/`derived`/`ungrounded`/…—every assignment in `llm.py` is
  `check.status` or a constant); the latter is the composed provenance string
  (`retry:ctx:sum(q3.awards)`) and stays backend-only telemetry. Writing prefix
  parsing into the frontend predicate models a shape that never occurs.
  The **chart toolbar is compact** so it fits a
  narrow side-by-side chart without overflowing: a single **`<select>`** collapses
  Line / Bar / **Line + trend** (trend is a line subtype, offered whenever the data is
  **trend-eligible** — a single numeric time-series with ≥3 points — **independent of
  the current type**, so "Line + trend" stays selectable while "Bar" is active; the
  fitted line only draws on a line chart). **Data labels** + **Copy image** +
  **Maximize** are **icon-only** buttons (tooltip on hover; `IconCopy`→`IconCheck` on
  copy). **Maximize** (`IconMaximize`) opens `ChartModal.jsx` — the same chart at
  large size in a dialog (reuses the `ConfirmModal` a11y pattern: focus-in/trap,
  Escape/overlay/Close, background `inert`, focus returns to the opener); the inner
  `<Chart inModal>` hides its own maximize control and carries the opener's current
  type/trend/labels via `initial*` props (Chart ↔ ChartModal is an intentional cyclic
  import, resolved at render time). A long chart **title wraps to 2 lines**
  (`wrapLabel`) so a narrow chart doesn't clip it, while the wide PNG export keeps one
  line. `.chart-head` wraps rather than overflowing. **`role="img"` sits on the inner
  `.chart-graphic`, NEVER on the outer `<figure>`** — ARIA's presentational-children
  rule strips every descendant of a `role="img"` from the a11y tree, so on the figure
  it hid the whole toolbar (type select, delta badge, labels/copy/maximize) from
  assistive tech while leaving it on screen. **Playwright's role engine does not prune
  presentational children**, so `getByRole` found the controls and the toolbar specs
  passed the entire time it was broken; the regression test therefore asserts
  **containment** (`[role="img"] .chart-head` → 0) in `chat-happy-path.spec.js`, not
  role. Treat "pinned by e2e" with suspicion for a11y semantics specifically.
- **Four ANSWER-PROSE contracts in prompt step 4** (#331, #334), all pinned in
  `test_prompt.py` and all covering defects **no checker can see** — grounding
  grades numbers, and these are about the shape of the prose around them. Each was
  observed live in a single 29-turn pass.
  **(1) ONE ENTITY PER ROW** — a 54-state answer came back as a three-pair
  newspaper grid with repeated headers. Every value was right, but column sorting,
  the CSV export and compare-mode row selection all read one row as one entity, so
  the grid breaks all three; it is also what triggers the `_anchor_rows` eviction
  above. The rule states WHY, because a bare rule with no reason is the first thing
  a later edit drops. **(2) DON'T PROMISE A DOWNLOAD YOU DIDN'T PRODUCE** — step 4
  already said to mention the CSV when `run_sql` TRUNCATES, and never distinguished
  that from a `LIMIT` the model wrote itself; an answer showed 20 of 53 under its
  own LIMIT and pointed at the download "for the full list", which re-runs the same
  query. **(3) NO THINKING OUT LOUD** — an answer shipped "…the only school where
  women are a majority... wait, no — it's actually 23.9% there." This is prevented
  in the prompt rather than SCRUBBED server-side, deliberately: the FALSE claim
  precedes the marker, so deleting "wait, no —" and what follows would leave the
  wrong statement standing, and there is no reliable way to delimit backwards where
  the bad clause began. A scrubber could only make the answer worse.
  **(4) EVERY NUMBER IN A SENTENCE COMES FROM A QUERY** — two wrong numbers shipped
  in prose (a fabricated "~31,000" against a true 33,126; "about 165,000 …
  roughly 32 per 100" over a column summing to 155,693). **A prose checker was
  measured and deliberately NOT built** — three scopings, all dominated by correct
  prose (52.9% / 78.3% / 57 flags in 119 answers, against 2 real defects in 29
  turns); the numbers are in `grounding.py`'s docstring under "TWO THINGS
  DELIBERATELY NOT BUILT HERE". Don't re-propose it without new evidence.
  Testing note that cost a vacuous assertion twice: key these on the phrase the
  rule ADDS, not on words step 4 already contained ("limit"/"truncat"/"total"/
  "query" were all present before), and whitespace-normalize the step-4 slice — the
  phrases are contiguous to a reader but land either side of the prompt's hard wrap.
- **The analyst layer** on top of the brief:
  - **Trend line + %-change** — `Chart.jsx` overlays a least-squares fit (a computed
    `__trend` `<Line>`, dashed ochre, injected into `chartChildren()` so it flows to
    the PNG export too; kept out of `keys` → no label/legend) and a **delta badge**
    (`▲/▼ X%` over the range, `--ok`/`--danger`) for a single-series line time-series.
    All client-side from the numeric chart data (`trendstats.js`, vitest) — accurate,
    no model dependency; the trend line is default-on via the chart-type control.
    **Both trend line AND delta are gated to a TIME-LIKE x-axis**
    (`/year|date|month|quarter|day/i`) — a
    "% change over the range" / fitted slope is meaningless across categorical
    entities, so a categorical bar (e.g. compare mode below) shows neither.
  - **Richer narrative + rank/share** — prompt step 6(b): direction/magnitude,
    peak/trough years, provisional-year flags, and (when meaningful) the figure's rank
    among peers or share of a national total (the model runs one extra query).
  - **"You might also ask" drill-down chips** — the model emits a ```followups
    fence on EVERY answered turn (step 7 is REQUIRED, not optional — only an
    off-topic/unanswerable turn skips it, so chips appear on every real answer, not
    just single-number briefs); `_extract_suggestions` parses+strips it (mirrors
    the figure) → `{"type":"suggestions",…}` event → `messages.suggestions` (migration
    15) + `query_cache.suggestions` (16). `Suggestions.jsx` (pure `suggestions.js`,
    vitest) renders chips below the actions row; clicking one `submit()`s it as a
    follow-up turn (which gets its own brief) — an exploration loop.
- **Compare mode** — pick 2–4 rows from any result table and **instantly** chart just
  those rows, client-side, from the numbers ALREADY in the table (no new query, no
  backend, no persistence). Gated to a **comparable (categorical) table** — one where
  `chartSpecFromTable` infers `type: "bar"` (entity rows: universities/states/…),
  never a year-over-year trend table. Pure logic in `compare.js` (vitest):
  `comparableTable(headers, rows)` (reuses `chartSpecFromTable`'s entity-column
  inference — `spec.x`) and `compareSpec(spec, selectedLabels)` (filters the parent
  spec's data to the selected entities, forces a bar snapshot). `Markdown.jsx`'s
  `SortableTable` renders the leading checkbox **inline in its own row map**, with
  selection keyed by the entity LABEL rather than a row index — so a tick survives a
  re-sort, which is the whole reason for the label key. (The earlier react-markdown
  `tr` override + per-table `CompareContext` are **gone**; don't go looking for them,
  and see `Markdown.jsx`'s comment at the `SortableTable` definition.) A "Compare N →" bar
  appears once ≥1 row is ticked (action enables at 2, capped at 4), rendering the
  snapshot `<Chart>` in a `.compare-panel`. `Chart.jsx` renders **every** categorical
  tick (`interval={0}`) and **wraps** long labels onto multi-line centered ticks
  (`wrapLabel`/`WrapTick`) — Recharts otherwise silently DROPS colliding ticks, so a
  long-named bar (e.g. "Texas A&M University–College Station") would go unlabeled.
  Browser truth in `frontend/e2e/compare.spec.js`.

## Self-learning & cache
- **Lessons** — a short generalized **headline** + a longer generalized
  **description** (collapsible in the admin UI) + a commented SQL worked example.
  Retrieved as guidance at query time, from **two sources**, both feeding the same
  unverified pool: the **critic** (`app/critic.py`) mines the MODEL's own mistake
  — when it catches one it phrases it as a headline+description in one call,
  reused as both the revision feedback and the stored lesson
  **A REJECTION IS NOW REMEMBERED, AND SUPPRESSION HAS A FIXED ORDER.**
  Rejecting a lesson was a hard `DELETE FROM skills` leaving no trace, and
  `_find_duplicate` can only match rows that still exist — so every rejection
  erased the very evidence that would have suppressed the next proposal, which
  is why the same lesson came back forever. **Migration 35** adds
  `skills.category`, a `lesson_rejections` tombstone table (headline,
  description, embedding, category, `was_verified`, `hits`) and
  `meta['muted_lesson_categories']` (a JSON list, the `seed_lessons_applied`
  precedent — ≤7 elements and admin-mutable at runtime, so it is state, not
  config; corrupt JSON **fails OPEN**, since a corrupt marker should re-queue
  for review, never keep silently suppressing).
  `delete_skill` writes a tombstone before deleting — for **every** deletion,
  approved or queued, because retiring an approved rule also means "don't
  re-suggest this" — reusing the row's **stored** embedding rather than
  re-embedding (free, and it works when fastembed is down). It takes
  `?mute_category=1` so "Reject & mute" is **one atomic request**; chaining two
  calls can leave the delete done and the mute failed, and the mute is the whole
  point of the button.
  **`skill_id` on a tombstone is a non-unique provenance breadcrumb — NOTHING
  may key off it.** `skills.id` is `INTEGER PRIMARY KEY` with no AUTOINCREMENT,
  so SQLite reuses a freed id. An earlier implementation deleted prior
  tombstones sharing a `skill_id`, which **defeated the whole feature**:
  rejecting a new lesson that inherited a reused id erased a genuinely
  different earlier lesson's tombstone, letting it be re-proposed forever. Two
  tombstones sharing a `skill_id` is expected. The tests learned the same
  lesson — they discriminate by **headline**, since these suites share one
  `app.db` for the whole file (a `skill_id` filter matched every tombstone the
  file had ever created: measured `[8, 8, 8, 8, 8, 8, 8, 8, 8]`).
  **The order in `_upvote_or_save` IS the design**, both halves mutation-pinned:
  (1) muted-category gate, in `record_lesson_from_critic` **before any embed**
  (deterministic, and the only step that still works with embeddings
  unavailable) → (2) embed → (3) **tombstone check** → (4) the existing
  `_find_duplicate` same-source upvote check, **unchanged** → (5) the widened
  `_find_suppressor` → (6) save. Step 3 precedes step 4 or a rejected idea still
  inflates a pending row's upvotes. Step 5 **follows** step 4 because the two
  predicates are complements that can each match a *different* row for the same
  candidate — checking the wider net first would suppress on a verified match
  and never reach the same-source upvote, silently dropping the everyday "this
  rule came up again" signal the review queue runs on.
  **`_find_suppressor` is deliberately ASYMMETRIC** (`include_pending_other_source`):
  its **verified arm applies to every source** — an approved rule is already
  active in the prompt, so restating it adds nothing — while the
  **different-source-pending arm is critic-only**, because a user's correction
  and the model's own self-critique on the same scenario are *different
  evidence* and the queue should show both (pinned by
  `test_feedback_lesson_not_collapsed_into_a_critic_row_same_scenario`). The
  predicate needs `IFNULL(created_by,'') != ?` — `created_by` is nullable and
  `NULL != 'critic'` evaluates to NULL, not true, silently excluding every
  NULL-source row. Reuses `skill_dedup_threshold`; **no new setting**, which
  would only invite re-opening the hole.
  **Every suppression logs at INFO** naming the reason — suppression is
  invisible by construction (no row appears), so without the log a legitimate
  lesson vanishes with no trace and nobody can learn the feature over-reaches.
  Admin → Logs is already substring-searchable, so this needed no new UI.
  Admin → Skills gains a category pill, "Reject & mute", and collapsed
  "Rejected (N)" / "Muted categories (N)" sections with Allow-again/Unmute
  (**"Allow again", not "Undo"** — the endpoint deletes the tombstone and does
  NOT restore the lesson, and the visible text has to be a contiguous substring
  of the accessible name for WCAG 2.5.3); a
  rejections **load failure renders an error, never "Rejected (0)"** (the
  `deniedError` precedent). Pure logic in `admin/lessoncats.js` (vitest) —
  `categoryLabel` returns `""` for a NULL category, which is what stops a
  pre-migration row rendering "Reject and mute **undefined**".
  **Known limit:** rows queued before migration 35 have `category = NULL`, so
  "Reject & mute" isn't offered on them; clearing that backlog is a one-time
  manual pass.
  (`skills.record_lesson_from_critic`); the **feedback distiller**
  (`app/feedback.py`, `distill_feedback`) mines the USER's own corrective
  feedback on a follow-up turn ("you should have kept the bachelor's scope") the
  same shape, via `skills.record_lesson_from_feedback`
  (`created_by="user-feedback"`) — a cheap separate probe call, fails open exactly
  like the critic/guard, gated on `skills_enabled`, run only when `history` is
  non-empty (a first-turn question has no prior answer to correct). Lessons
  start **unverified → an admin approves**; deduped on save (scoped per-source, so
  a feedback candidate never collapses into a critic/seed row on the same
  scenario); the embedding key is **headline+description, never the question**.
  `SKILLS_ENABLED=0/1` gates the on/off eval A/B.
  **Shipped SEED lessons arrive per-lesson, and exactly once.** `app/seeds.py`'s
  `SEED_EXAMPLES` are inserted at boot by `skills.seed_from_schema_examples`,
  which used to bail whenever the `skills` table held **any** row — so a seed
  added in a later release reached **fresh installs only**: every existing
  deployment had rows (its original seeds, plus critic/feedback lessons), the
  gate was shut forever, and new exemplars silently never arrived. Found in the
  wild on 0.2.0 by Todd, whose upgraded deployment kept its original 3 while the
  image shipped 8. Each `SeedLesson` now carries a stable **`slug`** — its only
  durable identity, since headline/description/SQL all get rewritten
  (`SEED_LESSON_UPGRADES` exists because they have been) — and the slugs applied
  so far live in `meta.seed_lessons_applied`. Two consequences, both deliberate:
  an admin who **deletes** a seed from the Skills tab has made a decision the
  next boot respects (deriving "missing" from the table alone would resurrect it
  every restart), and a database that predates the marker is recognized by a
  **one-time backfill** matching each seed's headline **OR question** against
  existing `created_by='seed'` rows — `question` because no upgrade path has ever
  rewritten one, so a pre-migration-6 row with a NULL headline still matches and
  the backfill does not depend on `upgrade_seed_lessons` having run first.
  `save_skill` does **no** dedup of its own (that's `_upvote_or_save`, unverified
  same-source rows only), so the marker is the only thing between an upgrade and
  a pile of duplicate seeds. Pinned in `test_skills.py`.
- A **semantic answer cache** short-circuits repeat questions — **scoped to the
  user who asked** (migration 29's `query_cache.user_id`) and **bounded**.
  `cache_lookup` had no user predicate, so a colleague asking within
  `cache_similarity_threshold` (0.93) of your question was served *your* stored
  answer prose verbatim — the same attributable leak `/api/admin/usage` refuses
  to make by never returning question text. Rows written before the migration
  have `user_id` NULL and are reachable by **nobody** (fail closed, not
  shared-by-default); the sweep clears them. Accepted cost: a popular question is
  now answered once *per person*, not once per deployment. It also had **no bound
  of any kind** — the only DELETE anywhere was `invalidate_cache`'s wholesale wipe
  on a data import, while every first-turn question `vstack`s and matmuls the
  WHOLE table before the agent starts, so latency and memory grew with uptime
  forever. `_prune_cache` mirrors `logbuffer._prune` (`cache_retention_days` /
  `cache_max_rows`, non-positive disables, OFFSET-based row cap, incremental-vacuum
  reclaim) and runs opportunistically on the **write** path only — a read must
  stay cheap. Pinned in `test_skills.py`.
  **An APP UPGRADE also wipes it**, which nothing did before: a cached answer is
  a verbatim replay of prose an older build produced under an older
  `SCHEMA.md`/system prompt, so a code change can leave stored answers that are
  simply WRONG. Found while *verifying* #326 — that PR fixed a false award-level
  rule, and re-asking the question returned the pre-fix total from cache
  (`model_used='cache'`, no SQL events); at 30-day retention and 0.93 similarity
  the fix would have reached nobody who had already asked.
  `invalidate_cache_if_version_changed` (called from `lifespan`) compares
  `config.app_version` to the `meta` key `cache_app_version`. **A MISSING marker
  counts as changed** — every deployment upgrading INTO the release that adds
  this has no marker and a full cache, so reading "no marker" as "current" would
  make the feature miss its own first release, the exact bug
  `seed_from_schema_examples` shipped. Version-keyed, not content-keyed, on
  purpose: it wipes once per upgrade even when nothing relevant moved (one cache
  miss per question), because a needless miss costs a query while a stale hit
  ships a wrong answer. Fingerprinting `SCHEMA.md` + the prompt per row is the
  better design and is backlog. `app_version` is `"dev"` locally, so dev wipes
  at most once.
  **A cache hit carries its own evidence** (`query_cache.results` +
  `results_truncated`, migration 31). It used to store the answer but not the ROWS
  behind it, so the cached branch persisted `messages.results=NULL` and every
  LATER turn in that conversation had nothing to ground a recited number against —
  it silently graded `unchecked`, denting a rate the project steers by with no
  visible failure. The rows are legitimate evidence for that answer (the replayed
  prose is byte-identical to the turn that produced them), and they're already
  capped by `_results_for_storage`, so there's no new size risk. Deliberately NOT
  done: re-grading the cached figure on the hit — now possible, but it would move
  the Grounded-figures denominator, and that shouldn't shift inside a plumbing
  change. Pinned by `a cache hit keeps the conversation grounding chain intact`
  (`test_chat_router.py`), which asserts `_load_prior_results` can actually read
  them back — a non-NULL-column check would pass on a blob grounding can't parse.
