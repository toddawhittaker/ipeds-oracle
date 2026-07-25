export function parseCsv(text: any): any[];
export function normalizeHeader(raw: any): "email" | "note" | "admin";
export function mapColumns(headerRow: any): {
    email: any;
    note: any;
    admin: any;
};
export function parseAdminFlag(value: any): boolean;
export function isValidEmail(value: any): boolean;
export function resolveNote(rawNote: any, today: any): string;
export function buildImportPlan(text: any, existingEmails: any, { today }?: {}): {
    headerError: string;
    totalRows: number;
    ready: any[];
    existingOrDuplicate: any[];
    invalid: any[];
    adminCount: number;
};
