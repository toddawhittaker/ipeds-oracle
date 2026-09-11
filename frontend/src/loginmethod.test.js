import { describe, expect, it } from "vitest";

import { METHODS, loginMethod, ssoButtonLabel } from "./loginmethod.js";

// THE REGRESSION these guard: the door rendering a form the deployment cannot
// use. `/api/auth/config` can fail (offline, a proxy answering HTML, an older
// backend that predates the field), and every one of those has to land on
// magic_link — the method that still works when the server is half-reachable,
// and the only one gated by the manually curated allowlist rather than by an
// external provider.
describe("loginMethod", () => {
  it("returns a known method unchanged", () => {
    for (const m of METHODS) expect(loginMethod({ auth_method: m })).toBe(m);
  });

  it("falls back to magic_link for anything it cannot read", () => {
    const junk = [
      undefined, null, {}, { auth_method: "" }, { auth_method: "   " },
      { auth_method: "saml" }, { auth_method: 7 }, { auth_method: null },
      { auth_method: ["oidc"] },
    ];
    for (const cfg of junk) expect(loginMethod(cfg)).toBe("magic_link");
  });

  it("tolerates surrounding whitespace rather than falling back", () => {
    expect(loginMethod({ auth_method: "  oidc  " })).toBe("oidc");
  });

  it("never invents a method that isn't in the list", () => {
    expect(METHODS).toContain(loginMethod({ auth_method: "anything" }));
  });
});

// A blank label is a real possibility: the setting is free text, and an empty
// .env value arrives as "". A button with no name is unusable.
describe("ssoButtonLabel", () => {
  it("uses the configured label", () => {
    expect(ssoButtonLabel({ oidc_button_label: "Sign in with NetID" }))
      .toBe("Sign in with NetID");
  });

  it("falls back when the label is blank, missing or not a string", () => {
    for (const cfg of [undefined, {}, { oidc_button_label: "" },
                       { oidc_button_label: "   " }, { oidc_button_label: 3 }]) {
      expect(ssoButtonLabel(cfg)).toBe("Sign in with SSO");
    }
  });
});
