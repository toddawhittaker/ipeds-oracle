export type KeyRevealProps = {
    /**
     * The raw key, exactly as minted. Shown once and never
     * fetched again — the caller must not persist it.
     */
    secret: string;
    /**
     * The label given at mint time, if any.
     */
    label?: string;
    /**
     * Whose key it is. Shown only when an admin minted it
     * for somebody else, so the reveal names the person to hand it to.
     */
    email?: string;
    /**
     * Called on every dismissal path. Required.
     */
    onClose: () => void;
};
/**
 * @typedef {object} KeyRevealProps
 * @property {string} secret The raw key, exactly as minted. Shown once and never
 *   fetched again — the caller must not persist it.
 * @property {string} [label] The label given at mint time, if any.
 * @property {string} [email] Whose key it is. Shown only when an admin minted it
 *   for somebody else, so the reveal names the person to hand it to.
 * @property {() => void} onClose Called on every dismissal path. Required.
 */
/** @param {KeyRevealProps} props */
export default function KeyReveal({ secret, label, email, onClose }: KeyRevealProps): any;
