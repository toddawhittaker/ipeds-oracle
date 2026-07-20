# AI-Assisted Software Engineering

This project is built with heavy use of AI coding assistants, and that's stated
plainly — the engineering handbook ([`CLAUDE.md`](../CLAUDE.md)) and the directory
of specialized review and implementation agents ([`.claude/`](../.claude)) are
checked into the repository. This note explains the approach, because it's a
deliberate one and it deserves a straight answer rather than a shrug.

## The tool is not the story; the process is

Every meaningful leap in programming has been met with the same objection: that
the new abstraction isn't *real* engineering. Assembly programmers said it of the
higher-level languages that took over in the 1970s and 80s — the compiler couldn't
possibly know the machine as well as a person, the generated code was wasteful, it
wasn't real programming. They were right about the trade-offs and wrong about the
trajectory. The abstraction won because it was enormously more productive, and the
discipline didn't disappear. It **moved up a level** — to architecture,
interfaces, data models, and tests.

AI-assisted development is the next step in that lineage, and it draws the same
scorn, now compressed into a single dismissive phrase: *"vibe coding."* The
caricature is prompt-and-pray — code generated without understanding, shipped
without review, held together by hope. That kind of work exists, and it is bad.
But it's bad for exactly the reasons unreviewed, untested, copy-pasted *human*
code has always been bad. The problem was never who, or what, typed it.

## Where the work moves

Each abstraction leap relocates the engineer's judgment upward, and this one is no
different. When the mechanical layer — writing the functions, the loops, the
boilerplate — is largely automated, the hard and durable part of the job becomes
more of the job, not less: **architecture, interface and data design, test
strategy, security posture, and the judgment of what is actually correct, safe,
and maintainable.** Deciding *what* to build and *how you'll know it's right* is
the whole game — and it is precisely the layer these tools do **not** do for you.
That's where the craft is heading, and it's a more demanding place to stand, not
an easier one.

## What separates engineering from vibe coding

The line was never whether a model helped write the code. It's whether the change
was subjected to engineering discipline. In this repository, every change —
however it was authored — passes the same gates a serious team would insist on:

- **Tests that guard real behavior**, written first for anything that can regress
  — ownership and authorization scoping, persistence invariants, security
  contracts, aggregation correctness.
- **Coverage floors that bite:** a **≥80% per-module** line-coverage minimum on
  every backend module, and the same on the pure-logic JavaScript, enforced in CI
  — not a feel-good aggregate.
- **A test pyramid, not a pile:** fast unit tests for pure logic, real browser
  tests for genuine UI truth (focus, routing, streamed DOM), each regression caught
  at the lowest tier that can catch it.
- **One merge gate.** A single script reproduces the entire CI pipeline — lint,
  unit, backend, end-to-end — and runs before every push; nothing reaches the main
  branch except through a branch, a pull request, and a green build.
- **Explicit review passes** for correctness, security, and accessibility on
  changes that warrant them — the same specialties a mature team staffs.
- **Docs kept honest.** Architecture, workflow, and configuration docs are updated
  in the *same* change that alters them, so the written record never drifts from
  the code.

The assistant works *inside* those guardrails, not around them. It accelerates the
writing; the gates decide what survives. That is the entire point.

## Why bother making the case

Because "it was AI, so it's junk" is already the wrong heuristic, and it's aging
badly. Judge software the way we always should have: by its tests, its structure,
its security posture, and whether it does what it claims — not by the tooling that
produced it. Serious developers are using these tools now, and more will. The
engineer's value didn't evaporate; it moved to the architecture, discipline, and
judgment these gates exist to enforce. Discarding a productivity leap of this size
over a prejudice — instead of governing it with process — isn't caution. It's
leaving the craft on the table.

Read the [contributor handbook](../CONTRIBUTING.md) and [`CLAUDE.md`](../CLAUDE.md),
and hold this codebase to that standard. That's the only test that matters.
