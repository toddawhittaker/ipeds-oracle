import React from "react";
export type HelpPopoverProps = {
    /**
     * Accessible name for the trigger button.
     */
    label: string;
    /**
     * Popover body.
     */
    children: React.ReactNode;
    /**
     * Trigger glyph. Defaults
     * to IconHelp; pass any icon from this system.
     */
    icon?: React.ComponentType<{
        size?: number;
    }>;
    className?: string;
};
/**
 * @typedef {object} HelpPopoverProps
 * @property {string} label Accessible name for the trigger button.
 * @property {React.ReactNode} children Popover body.
 * @property {React.ComponentType<{ size?: number }>} [icon] Trigger glyph. Defaults
 *   to IconHelp; pass any icon from this system.
 * @property {string} [className]
 */
/** @param {HelpPopoverProps} props */
export default function HelpPopover({ label, children, icon: Icon, className }: HelpPopoverProps): React.JSX.Element;
