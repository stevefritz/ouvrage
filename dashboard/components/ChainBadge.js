// Foreman — ChainBadge component
// Purple accent badge showing chain position (e.g. "2/3"). Clickable.

import { h } from 'https://esm.sh/preact@10.25.4';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography } from '../tokens.js';

const html = htm.bind(h);

/**
 * ChainBadge — shows chain position as "current/total".
 * Props:
 *   position  — current position in chain (1-based)
 *   total     — total tasks in chain
 *   onClick   — click handler (navigates to chain view or shows chain)
 *   title     — tooltip override
 */
export function ChainBadge({ position, total, onClick, title }) {
    const label = `${position}/${total}`;
    const isClickable = typeof onClick === 'function';
    const defaultTitle = `Chain task ${position} of ${total}`;

    const style = {
        display: 'inline-flex',
        alignItems: 'center',
        gap: '4px',
        fontFamily: typography.fontMono,
        fontSize: typography.size.xs,
        fontWeight: typography.weight.medium,
        color: colors.accent,
        background: colors.accentBg,
        border: `1px solid rgba(124, 90, 246, 0.25)`,
        borderRadius: '4px',
        padding: '1px 7px',
        lineHeight: '18px',
        whiteSpace: 'nowrap',
        cursor: isClickable ? 'pointer' : 'default',
        transition: 'background 120ms',
        userSelect: 'none',
    };

    const iconStyle = {
        fontSize: '9px',
        opacity: 0.7,
    };

    if (isClickable) {
        return html`
            <button
                style=${style}
                onClick=${onClick}
                title=${title || defaultTitle}
            >
                <span style=${iconStyle}>⛓</span>
                ${label}
            </button>
        `;
    }

    return html`
        <span style=${style} title=${title || defaultTitle}>
            <span style=${iconStyle}>⛓</span>
            ${label}
        </span>
    `;
}
