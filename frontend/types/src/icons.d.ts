import React from "react";
export type IconBaseProps = {
    /**
     * Width and height in px. Defaults to 15.
     */
    size?: number;
    className?: string;
    style?: React.CSSProperties;
};
export type IconProps = IconBaseProps & {
    "aria-hidden"?: boolean | "true" | "false";
};
/** @param {IconProps} p */
export declare const IconTrash: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconClose: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconCheck: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconUnlock: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconSend: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconEdit: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconCopy: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconTag: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconMaximize: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconRerun: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconShieldPlus: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconShieldMinus: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconShield: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconUpload: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconWarning: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconSun: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconMoon: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} props */
export declare const IconGitHub: ({ size, ...rest }: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconInfo: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconSignOut: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconChevronDown: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconChevronLeft: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconChevronRight: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconPlus: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconPause: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconPlay: (p: IconProps) => React.JSX.Element;
/** @param {IconProps} p */
export declare const IconHelp: (p: IconProps) => React.JSX.Element;
