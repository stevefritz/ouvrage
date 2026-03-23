// Foreman — Tag component
// Small pill with monospace text. Used for IDs, branch names, labels.

import { h } from 'https://esm.sh/preact@10.25.4';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography } from '../tokens.js';

const html = htm.bind(h);

/**
 * Tag — compact pill label in monospace.
 * Props:
 *   children — label text
 *   color    — text color (default: textSecondary)
 *   bg       — background color (default: surface)
 *   onClick  — optional click handler (makes it interactive)
 *   title    — tooltip text
 */
export function Tag({ children, color, bg, onClick, title }) {
    const isClickable = typeof onClick === 'function';

    const style = {
        display: 'inline-flex',
        alignItems: 'center',
        fontFamily: typography.fontMono,
        fontSize: typography.size.xs,
        fontWeight: typography.weight.normal,
        color: color || colors.textSecondary,
        background: bg || colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: '4px',
        padding: '1px 6px',
        lineHeight: '18px',
        whiteSpace: 'nowrap',
        cursor: isClickable ? 'pointer' : 'default',
        transition: 'background 120ms, color 120ms',
        textDecoration: 'none',
        userSelect: 'none',
    };

    if (isClickable) {
        return html`
            <button
                style=${style}
                onClick=${onClick}
                title=${title}
                class="foreman-tag-btn"
            >${children}</button>
        `;
    }

    return html`
        <span style=${style} title=${title}>${children}</span>
    `;
}
