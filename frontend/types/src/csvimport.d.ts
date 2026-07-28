export declare function parseCsv(text: any): any[];
export declare function normalizeHeader(raw: any): "admin" | "email" | "note";
export declare function mapColumns(headerRow: any): {
    email: any;
    note: any;
    admin: any;
};
export declare function parseAdminFlag(value: any): boolean;
export declare function isValidEmail(value: any): boolean;
export declare function resolveNote(rawNote: any, today: any): string;
export declare function buildImportPlan(text: any, existingEmails: any, { today }?: {}): {
    totalRows: number;
    ready: any[];
    existingOrDuplicate: any[];
    invalid: any[];
    adminCount: number;
    headerError: string;
};
