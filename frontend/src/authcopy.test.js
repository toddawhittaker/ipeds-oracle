import { describe, expect, it } from "vitest";

import {
  SERVER_UNREACHABLE,
  SESSION_EXPIRED,
  detailText,
  loadErrorMessage,
  turnErrorMessage,
} from "./authcopy.js";

// THE REGRESSION these guard: a raw response body reaching a user. Chat used to
// render `"⚠️ " + err.message` where message was the unparsed body, so an
// ordinary rate-limit appeared as literal JSON braces.
describe("turnErrorMessage", () => {
  it("never returns JSON, whatever it is handed", () => {
    const inputs = [
      [429, '{"detail":"Too many requests"}'],
      [401, '{"detail":"Not signed in."}'],
      [500, undefined],
      [undefined, undefined],
      [503, '{"detail":"upstream gone"}'],
    ];
    for (const [status, detail] of inputs) {
      const msg = turnErrorMessage(status, detail);
      expect(msg).toBeTruthy();
      expect(msg).not.toMatch(/[{}]/);
      expect(msg).not.toMatch(/"detail"/);
    }
  });

  it("says something actionable about a rate limit, not 'error'", () => {
    const msg = turnErrorMessage(429, "Too many requests — please slow down.");
    expect(msg).toMatch(/faster than/i);
    expect(msg).toMatch(/try again/i);
  });

  it("points an expired session at signing in, and reassures about the chats", () => {
    expect(turnErrorMessage(401, "Not signed in.")).toBe(SESSION_EXPIRED);
    expect(SESSION_EXPIRED).toMatch(/sign in again/i);
    expect(SESSION_EXPIRED).toMatch(/saved/i);
  });

  it("prefers the server's own sentence for anything unclassified", () => {
    // Guard refusals and "no query is associated with this answer" are written
    // for a human and beat a generic apology.
    expect(turnErrorMessage(400, "That question isn't about IPEDS data."))
      .toBe("That question isn't about IPEDS data.");
  });

  it("falls back when there is no detail to show", () => {
    expect(turnErrorMessage(500, "")).toMatch(/something went wrong/i);
    expect(turnErrorMessage(500, "")).toMatch(/try again/i);
  });

  it.each([[502], [503], [504]])("treats %i as temporary, not as the user's fault", (status) => {
    expect(turnErrorMessage(status, "")).toMatch(/unavailable|try again/i);
  });
});

describe("the two logged-out reasons stay distinct", () => {
  it("does not tell someone to sign in again when the server is unreachable", () => {
    // Different problem, different fix: "sign in again" wastes their time when
    // signing in is exactly what won't work.
    expect(SERVER_UNREACHABLE).not.toBe(SESSION_EXPIRED);
    expect(SERVER_UNREACHABLE).toMatch(/couldn't reach|connection/i);
    expect(SERVER_UNREACHABLE).not.toMatch(/expired/i);
  });
});

describe("loadErrorMessage", () => {
  it("names what failed, so an admin knows which panel is lying", () => {
    expect(loadErrorMessage("the logs", "")).toMatch(/the logs/);
    expect(loadErrorMessage("usage", "")).toMatch(/usage/);
  });

  it("prefers the server's detail when there is one", () => {
    expect(loadErrorMessage("the logs", "Database is locked.")).toBe("Database is locked.");
  });

  it("never reads as an empty result", () => {
    // The whole point: "No log records." was indistinguishable from a failure.
    for (const msg of [loadErrorMessage("the logs", ""), loadErrorMessage("usage", "")]) {
      expect(msg).not.toMatch(/^no /i);
      expect(msg).toMatch(/couldn't load/i);
    }
  });
});

// THE REGRESSION: FastAPI raises 422 itself, before any handler runs, whenever a
// body fails validation — and its `detail` is an ARRAY, not the string every
// hand-written error in this codebase sends. Passed to `new ApiError(...)` the
// array stringifies to "[object Object]", which is the raw-body leak this module
// exists to prevent, arriving through the one status nobody writes by hand.
describe("detailText", () => {
  it("flattens a pydantic 422 array to its human messages", () => {
    const detail = [
      { loc: ["body", "conversation_id"], msg: "Input should be a valid integer",
        type: "int_parsing" },
    ];
    expect(detailText(detail)).toBe("Input should be a valid integer");
  });

  it("joins multiple field errors", () => {
    expect(detailText([{ msg: "field required" }, { msg: "too long" }]))
      .toBe("field required; too long");
  });

  it("never yields the object stringification", () => {
    for (const d of [[{ msg: "nope" }], [{}], [{ msg: 7 }], {}, 42, null, undefined]) {
      expect(detailText(d)).not.toMatch(/\[object Object\]/);
    }
  });

  it("passes a normal string detail straight through", () => {
    expect(detailText("Question is too long (max 4,000 characters)."))
      .toBe("Question is too long (max 4,000 characters).");
  });

  it("returns empty for a shape it can't read, so the caller falls back", () => {
    expect(detailText({ unexpected: true })).toBe("");
    expect(detailText([{ noMsg: 1 }])).toBe("");
  });
});
