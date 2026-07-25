export function IconTrash(p: IconProps): React.JSX.Element;
export function IconClose(p: IconProps): React.JSX.Element;
export function IconCheck(p: IconProps): React.JSX.Element;
export function IconUnlock(p: IconProps): React.JSX.Element;
export function IconSend(p: IconProps): React.JSX.Element;
export function IconEdit(p: IconProps): React.JSX.Element;
export function IconCopy(p: IconProps): React.JSX.Element;
export function IconTag(p: IconProps): React.JSX.Element;
export function IconMaximize(p: IconProps): React.JSX.Element;
export function IconRerun(p: IconProps): React.JSX.Element;
export function IconShieldPlus(p: IconProps): React.JSX.Element;
export function IconShieldMinus(p: IconProps): React.JSX.Element;
export function IconShield(p: IconProps): React.JSX.Element;
export function IconUpload(p: IconProps): React.JSX.Element;
export function IconWarning(p: IconProps): React.JSX.Element;
export function IconSun(p: IconProps): React.JSX.Element;
export function IconMoon(p: IconProps): React.JSX.Element;
export function IconGitHub({ size, ...rest }: IconProps): React.JSX.Element;
export function IconInfo(p: IconProps): React.JSX.Element;
export function IconSignOut(p: IconProps): React.JSX.Element;
export function IconChevronDown(p: IconProps): React.JSX.Element;
export function IconChevronLeft(p: IconProps): React.JSX.Element;
export function IconChevronRight(p: IconProps): React.JSX.Element;
export function IconPlus(p: IconProps): React.JSX.Element;
export function IconPause(p: IconProps): React.JSX.Element;
export function IconPlay(p: IconProps): React.JSX.Element;
export function IconHelp(p: IconProps): React.JSX.Element;
/**
 * Every icon in this module takes the same props. Stroke is `currentColor` at 2px
 * on a 24-viewBox, so an icon inherits its button's text colour — recolour with
 * `color`, never a fill prop.
 */
export type IconBaseProps = {
    /**
     * Width and height in px. Defaults to 15.
     */
    size?: number;
    className?: string;
    style?: React.CSSProperties;
};
/**
 * `@property` cannot express a hyphenated key, so the aria props are intersected
 * in — that keeps the documented props above readable rather than collapsing the
 * whole typedef into one inline object literal.
 */
export type IconProps = IconBaseProps & {
    "aria-hidden"?: boolean | "true" | "false";
};
import React from "react";
