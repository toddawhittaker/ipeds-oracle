export function shouldRedirectTyping({ key, ctrlKey, metaKey, altKey }: {
    key: any;
    ctrlKey: any;
    metaKey: any;
    altKey: any;
}, { tag, editable, inDialog }: {
    tag: any;
    editable: any;
    inDialog: any;
}): boolean;
export function targetInfo(el: any): {
    tag: any;
    editable: boolean;
    inDialog: boolean;
};
