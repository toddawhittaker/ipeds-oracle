export declare const USER_SUBTABS: {
    key: string;
    label: string;
}[];
export declare const DEFAULT_SUBTAB = "current";
export declare function resolveSubTab(sub: any): any;
export declare function subTabKeyForArrow(currentKey: any, action: any): any;
export declare function pendingBadgeTone(count: any): "attention" | "idle";
export declare function rememberedSubTab(): any;
export declare function rememberSubTab(sub: any): void;
