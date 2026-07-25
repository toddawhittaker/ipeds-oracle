// Library entry for the claude.ai/design sync (see /.design-sync/). NOT used by
// the app — Vite builds the SPA from index.html/main.jsx as it always has.
//
// Why this file has to exist: the converter falls back to synthesizing an entry
// with `export * from "<each src file>"`, and `export *` does NOT re-export a
// module's DEFAULT export. Almost every component here is `export default
// function X`, so that fallback put only the icons and the two providers on
// window.IpedsOracle — 18 components silently missing from the bundle while
// still getting preview cards. Naming the exports is what makes them real.
//
// Add a component here when it becomes reusable, and pin it in
// .design-sync/config.json's componentSrcMap (the two lists are what the sync
// treats as this design system's public surface).

// ── Answer surface
export { default as Figure } from "./src/Figure.jsx";
export { default as Chart } from "./src/Chart.jsx";
export { default as ChartModal } from "./src/ChartModal.jsx";
export { default as Markdown } from "./src/Markdown.jsx";
export { default as SqlBlock } from "./src/SqlBlock.jsx";
export { default as Suggestions } from "./src/Suggestions.jsx";
export { default as Clarify } from "./src/Clarify.jsx";

// ── Data
export { default as DataTable } from "./src/DataTable.jsx";
export { default as BulkBar } from "./src/BulkBar.jsx";

// ── Navigation / chrome
export { default as Wordmark } from "./src/Wordmark.jsx";
export { default as UserMenu } from "./src/UserMenu.jsx";
export { default as CopyMenu } from "./src/CopyMenu.jsx";
export { default as HelpPopover } from "./src/HelpPopover.jsx";

// ── Overlays + app shell
export { default as AboutModal } from "./src/AboutModal.jsx";
export { default as ErrorBoundary } from "./src/ErrorBoundary.jsx";
export { default as MarkdownTextarea } from "./src/MarkdownTextarea.jsx";

// ── Providers and their hooks (ToastProvider/useToast, ConfirmProvider/useConfirm)
export * from "./src/Toast.jsx";
export * from "./src/ConfirmModal.jsx";

// ── Icons (all named exports)
export * from "./src/icons.jsx";

// ── Preview-only: a Router so UserMenu's <Link> can render in a card.
export { PreviewRouter } from "./ds-preview-env.js";
