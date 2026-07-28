export declare function shouldRedirectTyping({ key, ctrlKey, metaKey, altKey }: {
    altKey: any;
    ctrlKey: any;
    key: any;
    metaKey: any;
}, { tag, editable, inDialog }: {
    editable: any;
    inDialog: any;
    tag: any;
}): boolean;
export declare function targetInfo(el: any): {
    tag: any;
    editable: boolean;
    inDialog: boolean;
};
