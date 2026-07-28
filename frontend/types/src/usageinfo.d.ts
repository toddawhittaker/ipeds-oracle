export declare const STAT_INFO: {
    queries: {
        name: string;
        what: string;
        direction: string;
    };
    tokens: {
        name: string;
        what: string;
        direction: string;
        note: string;
    };
    spend: {
        name: string;
        what: string;
        direction: string;
    };
    answerCache: {
        name: string;
        what: string;
        direction: string;
        note: string;
    };
    schemaCache: {
        name: string;
        what: string;
        direction: string;
        note: string;
    };
    promptCache: {
        name: string;
        what: string;
        direction: string;
    };
    escalations: {
        name: string;
        what: string;
        direction: string;
        note: string;
    };
    failures: {
        name: string;
        what: string;
        direction: string;
    };
    groundedFigures: {
        name: string;
        what: string;
        direction: string;
        note: string;
    };
    groundedCells: {
        name: string;
        what: string;
        direction: string;
    };
    answerLeaks: {
        name: string;
        what: string;
        direction: string;
        note: string;
    };
    exhausted: {
        name: string;
        what: string;
        direction: string;
        note: string;
    };
};
export declare function directionHint(direction: any): "Higher is better." | "Just a count — neither high nor low is inherently good." | "Lower is better.";
