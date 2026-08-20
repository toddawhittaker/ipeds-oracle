export declare const DELETE_FAILED = "Couldn't delete that chat.";
export declare const COPY_FAILED = "Couldn't copy to the clipboard. Select the text and copy it manually.";
export declare function deleteAnnouncement({ title, open, remaining, filtered }: {
    filtered?: boolean;
    open: any;
    remaining: any;
    title: any;
}): string;
