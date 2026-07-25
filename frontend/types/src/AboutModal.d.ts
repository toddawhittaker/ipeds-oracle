/**
 * @typedef {object} AboutModalProps
 * @property {() => void} onClose Called on every dismissal path. Required.
 * @property {boolean} [isAdmin] Gates the admin-guide link.
 * @property {{ current: string, latest?: string | null, update_available?: boolean } | null} [version]
 *   Running and latest version; shows the update note when `update_available` is
 *   true.
 */
/** @param {AboutModalProps} props */
export default function AboutModal({ onClose, isAdmin, version }: AboutModalProps): any;
export type AboutModalProps = {
    /**
     * Called on every dismissal path. Required.
     */
    onClose: () => void;
    /**
     * Gates the admin-guide link.
     */
    isAdmin?: boolean;
    /**
     * Running and latest version; shows the update note when `update_available` is
     * true.
     */
    version?: {
        current: string;
        latest?: string | null;
        update_available?: boolean;
    } | null;
};
