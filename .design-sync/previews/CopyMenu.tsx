import React from "react";
import { CopyMenu } from "ipeds-query-web";

// The single "Copy ▾" trigger that collapsed the two per-answer copy actions.
// Same WAI-ARIA menu-button pattern as UserMenu; the open panel is
// interaction-driven, so these stories cover the trigger's two states.

// Opens the menu on mount so the panel — the part worth looking at — actually
// appears in a static card. Exactly ONE story per card may do this.
function AutoOpen({ children }: { children: React.ReactNode }) {
  const ref = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    ref.current?.querySelector("button")?.click();
  }, []);
  // The panel opens UPWARD from the trigger, so without headroom it is clipped
  // by the top of the card cell.
  return <div ref={ref} style={{ paddingTop: 130 }}>{children}</div>;
}

export const Closed = () => <CopyMenu onCopyMarkdown={() => {}} onCopyHtml={() => {}} />;

export const Open = () => (
  <AutoOpen>
    <CopyMenu onCopyMarkdown={() => {}} onCopyHtml={() => {}} />
  </AutoOpen>
);

export const Copied = () => <CopyMenu onCopyMarkdown={() => {}} onCopyHtml={() => {}} copied />;
