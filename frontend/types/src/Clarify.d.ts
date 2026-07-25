/**
 * @typedef {object} ClarifyProps
 * @property {{ question: string, options: string[] }} spec The blocking
 *   clarification. `options` are SHORT answer phrases ("bachelor's only"), never
 *   restated questions.
 * @property {(answer: string) => void} onAsk
 * @property {boolean} [disabled]
 * @property {boolean} [showQuestion] Show the model's actual question as the label
 *   instead of the default "Did you mean".
 */
/** @param {ClarifyProps} props */
export default function Clarify({ spec, onAsk, disabled, showQuestion }: ClarifyProps): React.JSX.Element;
export type ClarifyProps = {
    /**
     * The blocking
     * clarification. `options` are SHORT answer phrases ("bachelor's only"), never
     * restated questions.
     */
    spec: {
        question: string;
        options: string[];
    };
    onAsk: (answer: string) => void;
    disabled?: boolean;
    /**
     * Show the model's actual question as the label
     * instead of the default "Did you mean".
     */
    showQuestion?: boolean;
};
import React from "react";
