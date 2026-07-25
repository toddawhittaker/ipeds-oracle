/**
 * @typedef {object} UserMenuProps
 * @property {string} email Signed-in address. The avatar's initials are derived
 *   from it (first.last -> "TW"; a +tag is stripped).
 * @property {boolean} [isAdmin] Adds the Admin item and lets the attention badge
 *   render.
 * @property {number} [attentionTotal] Count of things awaiting an admin. Rendered
 *   through the capped formatBadge: "" at 0, the number to 99, then "99+".
 *   Accent-toned — a queue is work waiting, never a red failure.
 * @property {"light" | "dark"} theme
 * @property {() => void} onToggleTheme The ONE menu item that deliberately keeps
 *   the menu open on activation.
 * @property {() => void} onSignOut
 * @property {() => void} onAbout
 */
/** @param {UserMenuProps} props */
export default function UserMenu({ email, isAdmin, attentionTotal, theme, onToggleTheme, onSignOut, onAbout, }: UserMenuProps): React.JSX.Element;
export type UserMenuProps = {
    /**
     * Signed-in address. The avatar's initials are derived
     * from it (first.last -> "TW"; a +tag is stripped).
     */
    email: string;
    /**
     * Adds the Admin item and lets the attention badge
     * render.
     */
    isAdmin?: boolean;
    /**
     * Count of things awaiting an admin. Rendered
     * through the capped formatBadge: "" at 0, the number to 99, then "99+".
     * Accent-toned — a queue is work waiting, never a red failure.
     */
    attentionTotal?: number;
    theme: "light" | "dark";
    /**
     * The ONE menu item that deliberately keeps
     * the menu open on activation.
     */
    onToggleTheme: () => void;
    onSignOut: () => void;
    onAbout: () => void;
};
import React from "react";
