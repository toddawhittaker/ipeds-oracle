import React from "react";
import { HelpPopover } from "ipeds-query-web";
import { IconInfo } from "ipeds-query-web";

// An inline help affordance: a small trigger that reveals its body on hover or
// focus. The open panel is interaction-driven, so these show the trigger.

// The popover opens on focus, so focusing the trigger on mount is what shows
// the panel in a static card. Only ONE story per card may take focus.
function AutoFocus({ children }: { children: React.ReactNode }) {
  const ref = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    ref.current?.querySelector("button")?.focus();
  }, []);
  return <div ref={ref}>{children}</div>;
}

export const Open = () => (
  <AutoFocus>
    <HelpPopover label="What counts as a first major?">
      IPEDS records a completion once per major, so a double major is counted
      twice unless you filter to first majors.
    </HelpPopover>
  </AutoFocus>
);

export const Default = () => (
  <HelpPopover label="What counts as a first major?">
    IPEDS records a completion once per major, so a double major is counted twice
    unless you filter to first majors.
  </HelpPopover>
);

export const CustomIcon = () => (
  <HelpPopover label="About provisional years" icon={IconInfo}>
    The most recent collection year is provisional until NCES publishes the final
    release; counts can still move.
  </HelpPopover>
);

export const InContext = () => (
  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
    <span className="field-label" style={{ letterSpacing: ".14em" }}>
      Grounded figures
    </span>
    <HelpPopover label="How grounded figures are measured">
      The share of answers whose hero number was reproduced from the query results.
    </HelpPopover>
  </span>
);
