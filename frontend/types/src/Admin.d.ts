import React from "react";
export declare const ADMIN_TABS: string[];
export declare function AdminRoute({ me, onDataChanged, attention, onAttentionChanged, version }: {
    attention: any;
    me: any;
    onAttentionChanged: any;
    onDataChanged: any;
    version: any;
}): React.JSX.Element;
export default function Admin({ me, tab, sub, onDataChanged, attention, onAttentionChanged, version }: {
    attention: any;
    me: any;
    onAttentionChanged: any;
    onDataChanged: any;
    sub: any;
    tab: any;
    version: any;
}): React.JSX.Element;
