// Foreman — StatusDot component
// Colored circle indicating task status. Pulses when status is "working".

import { h } from 'https://esm.sh/preact@10.25.4';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, statusColors } from '../tokens.js';

const html = htm.bind(h);

/**
 * StatusDot — a small colored circle for status indication.
 * Props:
 *   status  — task status string (working, completed, failed, needs-review, etc.)
 *   size    — dot diameter in px (default: 8)
 *   style   — additional inline styles
 *   pulse   — optional override for pulse animation (defaults to status === 'working')
 */
export function StatusDot({ status, size = 8, style: extraStyle, pulse }) {
    const color = statusColors[status] || colors.textTertiary;
    const isPulsing = pulse !== undefined ? pulse : (status === 'working');

    const dotStyle = {
        display: 'inline-block',
        width: `${size}px`,
        height: `${size}px`,
        borderRadius: '50%',
        backgroundColor: color,
        flexShrink: 0,
        ...(isPulsing ? {} : {}),
        ...extraStyle,
    };

    return html`
        <span
            class=${isPulsing ? 'foreman-status-dot-pulse' : ''}
            style=${dotStyle}
            aria-label=${status || 'unknown'}
        />
    `;
}
