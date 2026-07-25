export function setUnauthenticatedHandler(fn: any): void;
export function streamChat({ question, conversationId, editMessageId }: {
    question: any;
    conversationId: any;
    editMessageId: any;
}, onEvent: any): Promise<void>;
export class ApiError extends Error {
    constructor(status: any, detail: any, statusText: any);
    status: any;
    detail: any;
    get isUnauthenticated(): boolean;
}
export namespace api {
    function me(): Promise<any>;
    function publicConfig(): Promise<any>;
    function version(): Promise<any>;
    function requestLink(email: any): Promise<any>;
    function verifyInfo(token: any): Promise<any>;
    function verify(token: any): Promise<any>;
    function logout(): Promise<any>;
    function conversations(): Promise<any>;
    function conversation(id: any): Promise<any>;
    function renameConversation(id: any, title: any): Promise<any>;
    function deleteConversation(id: any): Promise<any>;
    function csvUrl(msgId: any): string;
    function allowlist(): Promise<any>;
    function addAllow(email: any, note: any, is_admin: any): Promise<any>;
    function bulkAllow(users: any): Promise<any>;
    function removeAllow(email: any): Promise<any>;
    function setAdmin(email: any, is_admin: any): Promise<any>;
    function bulkAllowlistAction(action: any, emails: any): Promise<any>;
    function accessRequests(): Promise<any>;
    function denyAccessRequest(email: any): Promise<any>;
    function bulkAccessRequests(action: any, ids: any): Promise<any>;
    function deniedRequests(): Promise<any>;
    function clearDenial(email: any): Promise<any>;
    function bulkClearDenials(ids: any): Promise<any>;
    function usage(since: any, until: any): Promise<any>;
    function attention(): Promise<any>;
    function markLogsSeen(): Promise<any>;
    function skills(): Promise<any>;
    function deleteSkill(id: any): Promise<any>;
    function patchSkill(id: any, body: any): Promise<any>;
    function importJobs(): Promise<any>;
    function importJob(id: any): Promise<any>;
    function importCatalog(refresh?: boolean): Promise<any>;
    function integrateYears(years: any): Promise<any>;
    function deintegrateYear(startYear: any): Promise<any>;
    function logs(limit?: number, level?: string, q?: string, since?: any, until?: any): Promise<any>;
}
