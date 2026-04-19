// Foreman FormKit — shared form components and style primitives
// Import from here for all form/settings pages.
//
// Usage:
//   import { styles, SectionHeader, FormField, FormRow,
//            CredentialCard, SecretRow, Toggle, ConfirmAction }
//     from './FormKit.js';

import { h } from 'https://esm.sh/preact@10.25.4';
import { useState } from 'https://esm.sh/preact@10.25.4/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, spacing, layout, animation } from '../tokens.js';

const html = htm.bind(h);

// ── Style primitives ──────────────────────────────────────────────────────────
// Pre-built style objects for inline use. All values from tokens.js.

export const styles = {
    // Card container — transparent bg, subtle border (Settings canonical pattern)
    card: {
        background: 'transparent',
        border: `0.5px solid ${colors.border}`,
        borderRadius: layout.borderRadius.lg,
        padding: spacing[4],
        marginBottom: spacing[3],
    },

    // Elevated card — surface bg, 1px border (project card / section card pattern)
    cardElevated: {
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.lg,
        padding: `${spacing[4]} ${spacing[5]}`,
    },

    // Section header — INSTANCE, ACCOUNT, NOTIFICATIONS
    sectionLabel: {
        fontSize: '11px',
        fontWeight: typography.weight.medium,
        color: colors.textTertiary,
        letterSpacing: '0.1em',
        textTransform: 'uppercase',
    },

    // Card title text — 14px medium
    cardTitle: {
        fontSize: typography.size.base,
        fontWeight: typography.weight.medium,
        color: colors.text,
        margin: 0,
    },

    // Card subtitle / description text
    cardSubtitle: {
        fontSize: typography.size.xs,
        color: colors.textSecondary,
        margin: 0,
    },

    // Form field label — 11px muted uppercase-weight
    label: {
        display: 'block',
        fontSize: '11px',
        fontWeight: typography.weight.medium,
        color: colors.textTertiary,
        marginBottom: '5px',
    },

    // Body text — 13px primary color
    body: {
        fontSize: typography.size.sm,
        color: colors.text,
    },

    // Monospace text — 13px mono
    mono: {
        fontSize: typography.size.sm,
        fontFamily: typography.fontMono,
        color: colors.text,
    },

    // Text input
    input: {
        width: '100%',
        boxSizing: 'border-box',
        padding: '7px 10px',
        fontSize: typography.size.sm,
        fontFamily: 'inherit',
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.md,
        background: colors.surface,
        color: colors.text,
        outline: 'none',
    },

    // Select dropdown (same structure as input)
    select: {
        width: '100%',
        boxSizing: 'border-box',
        padding: '7px 10px',
        fontSize: typography.size.sm,
        fontFamily: 'inherit',
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.md,
        background: colors.surface,
        color: colors.text,
        outline: 'none',
        cursor: 'pointer',
    },

    // Default/ghost button
    button: {
        fontSize: typography.size.xs,
        padding: '5px 14px',
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.md,
        background: 'transparent',
        color: colors.text,
        cursor: 'pointer',
        fontFamily: 'inherit',
        transition: `background ${animation.durationFast}, border-color ${animation.durationFast}, color ${animation.durationFast}`,
    },

    // Danger button — dormant ghost; caller applies red on hover via JS
    buttonDanger: {
        fontSize: typography.size.xs,
        padding: '5px 14px',
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.md,
        background: 'transparent',
        color: colors.textTertiary,
        cursor: 'pointer',
        fontFamily: 'inherit',
        transition: `color ${animation.durationFast}, border-color ${animation.durationFast}`,
    },

    // Primary button — accent background, white text
    buttonPrimary: {
        fontSize: typography.size.xs,
        padding: '5px 14px',
        border: 'none',
        borderRadius: layout.borderRadius.md,
        background: colors.accent,
        color: '#ffffff',
        cursor: 'pointer',
        fontFamily: 'inherit',
        transition: `background ${animation.durationFast}`,
    },

    // Status dot — call as styles.dot(color) to get a style object
    dot: (color) => ({
        display: 'inline-block',
        width: '7px',
        height: '7px',
        borderRadius: '50%',
        backgroundColor: color || colors.textTertiary,
        flexShrink: 0,
    }),

    // Convenience dot styles for connected / disconnected states
    dotConnected: {
        display: 'inline-block',
        width: '7px',
        height: '7px',
        borderRadius: '50%',
        backgroundColor: colors.green,
        flexShrink: 0,
    },

    dotDisconnected: {
        display: 'inline-block',
        width: '7px',
        height: '7px',
        borderRadius: '50%',
        backgroundColor: colors.red,
        flexShrink: 0,
    },
};

// ── SectionHeader ─────────────────────────────────────────────────────────────

/**
 * SectionHeader — uppercase muted category label (INSTANCE, ACCOUNT, etc.)
 * Props: text
 */
export function SectionHeader({ text }) {
    return html`
        <div style=${{ ...styles.sectionLabel, marginBottom: spacing[2] }}>
            ${text}
        </div>
    `;
}

// ── FormField ─────────────────────────────────────────────────────────────────

/**
 * FormField — label stacked above input/content.
 * Props: label (string, optional), children
 */
export function FormField({ label, children }) {
    return html`
        <div style=${{ marginBottom: spacing[3] }}>
            ${label && html`<label style=${styles.label}>${label}</label>`}
            ${children}
        </div>
    `;
}

// ── FormRow ───────────────────────────────────────────────────────────────────

/**
 * FormRow — two-column equal-width grid for side-by-side fields.
 * Props: children
 */
export function FormRow({ children }) {
    return html`
        <div style=${{
            display: 'grid',
            gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)',
            gap: spacing[3],
        }}>
            ${children}
        </div>
    `;
}

// ── CredentialCard ────────────────────────────────────────────────────────────

/**
 * CredentialCard — compact single-row credential entry with status and actions.
 *
 * Props:
 *   icon        — string (emoji) or element rendered at 16px
 *   name        — credential name, e.g. "GitHub"
 *   connected   — boolean; controls dot color (green/red)
 *   statusText  — short status string, e.g. "Connected as example-user"
 *   maskedValue — masked secret preview, e.g. "····JZ9f"
 *   onUpdate    — click handler for Update button (omit to hide button)
 *   onTest      — click handler for Test button (omit to hide button)
 *   children    — expanded edit slot, rendered below the row when present
 */
export function CredentialCard({ icon, name, connected, warning, statusText, maskedValue, onUpdate, onTest, children }) {
    const [hoverUpdate, setHoverUpdate] = useState(false);
    const [hoverTest, setHoverTest] = useState(false);

    const dotColor = warning ? colors.yellow : connected ? colors.green : colors.red;

    const cardStyle = {
        ...styles.card,
        marginBottom: 0,
    };

    const rowStyle = {
        display: 'flex',
        alignItems: 'center',
        flexWrap: 'wrap',
        gap: spacing[2],
    };

    const nameStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: spacing[2],
        flexShrink: 0,
    };

    const statusStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: spacing[2],
        flex: '1 1 200px',
        minWidth: 0,
    };

    const rightStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: spacing[2],
        flexShrink: 0,
        marginLeft: 'auto',
    };

    const actionBtnStyle = (hover) => ({
        ...styles.button,
        padding: '3px 10px',
        fontSize: '11px',
        background: hover ? colors.surfaceHover : 'transparent',
    });

    const hasStatus = statusText || maskedValue;

    return html`
        <div style=${cardStyle}>
            <div style=${rowStyle}>
                <div style=${nameStyle}>
                    ${icon && html`
                        <span style=${{ fontSize: '16px', flexShrink: 0, lineHeight: 1 }}>
                            ${icon}
                        </span>
                    `}
                    <span style=${{
                        fontSize: typography.size.base,
                        fontWeight: typography.weight.medium,
                        color: colors.text,
                    }}>${name}</span>
                </div>
                ${hasStatus && html`
                    <div style=${statusStyle}>
                        ${statusText && html`
                            <span style=${{
                                fontSize: typography.size.xs,
                                color: colors.textTertiary,
                            }}>${statusText}</span>
                        `}
                        ${maskedValue && html`
                            <span style=${{
                                fontFamily: typography.fontMono,
                                fontSize: typography.size.xs,
                                color: colors.textTertiary,
                            }}>${maskedValue}</span>
                        `}
                    </div>
                `}
                <div style=${rightStyle}>
                    <span style=${styles.dot(dotColor)} />
                    ${onUpdate && html`
                        <button
                            style=${actionBtnStyle(hoverUpdate)}
                            onMouseEnter=${() => setHoverUpdate(true)}
                            onMouseLeave=${() => setHoverUpdate(false)}
                            onClick=${onUpdate}
                        >Update</button>
                    `}
                    ${onTest && html`
                        <button
                            style=${actionBtnStyle(hoverTest)}
                            onMouseEnter=${() => setHoverTest(true)}
                            onMouseLeave=${() => setHoverTest(false)}
                            onClick=${onTest}
                        >Test</button>
                    `}
                </div>
            </div>
            ${children && html`
                <div style=${{
                    marginTop: spacing[3],
                    paddingTop: spacing[3],
                    borderTop: `0.5px solid ${colors.border}`,
                }}>
                    ${children}
                </div>
            `}
        </div>
    `;
}

// ── SecretRow ─────────────────────────────────────────────────────────────────

/**
 * SecretRow — label + masked mono value + Show/Copy buttons.
 * Value is hidden (dots) by default. Show toggles visibility.
 * Copy writes to clipboard without requiring the value to be revealed.
 *
 * Props:
 *   label  — field label, e.g. "Client ID"
 *   value  — the actual secret string
 *   onCopy — optional async callback(value); defaults to navigator.clipboard
 */
export function SecretRow({ label, value, onCopy, alwaysVisible = false }) {
    const [visible, setVisible] = useState(alwaysVisible);
    const [copied, setCopied] = useState(false);
    const [hoverShow, setHoverShow] = useState(false);
    const [hoverCopy, setHoverCopy] = useState(false);

    const handleCopy = async () => {
        try {
            if (onCopy) {
                await onCopy(value);
            } else if (navigator.clipboard) {
                await navigator.clipboard.writeText(value || '');
            }
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
        } catch (_) { /* ignore clipboard errors */ }
    };

    const smallBtnStyle = (hover) => ({
        fontSize: '11px',
        padding: '2px 8px',
        borderRadius: layout.borderRadius.sm,
        border: `1px solid ${colors.border}`,
        background: hover ? colors.surfaceHover : colors.surface,
        color: colors.textTertiary,
        cursor: 'pointer',
        fontFamily: 'inherit',
        flexShrink: 0,
        transition: `background ${animation.durationFast}`,
    });

    const displayValue = visible
        ? (value || '')
        : '•'.repeat(Math.min((value || '').length, 24));

    return html`
        <div style=${{
            border: `0.5px solid ${colors.border}`,
            borderRadius: layout.borderRadius.md,
            padding: `${spacing[2]} ${spacing[3]}`,
            marginBottom: spacing[2],
        }}>
            <div style=${{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                marginBottom: '4px',
            }}>
                <span style=${{ ...styles.label, marginBottom: 0 }}>${label}</span>
                <div style=${{ display: 'flex', alignItems: 'center', gap: spacing[1] }}>
                    ${!alwaysVisible && html`
                        <button
                            style=${smallBtnStyle(hoverShow)}
                            onMouseEnter=${() => setHoverShow(true)}
                            onMouseLeave=${() => setHoverShow(false)}
                            onClick=${() => setVisible(v => !v)}
                        >${visible ? 'Hide' : 'Show'}</button>
                    `}
                    <button
                        style=${smallBtnStyle(hoverCopy)}
                        onMouseEnter=${() => setHoverCopy(true)}
                        onMouseLeave=${() => setHoverCopy(false)}
                        onClick=${handleCopy}
                    >${copied ? 'Copied!' : 'Copy'}</button>
                </div>
            </div>
            <span style=${{
                fontFamily: typography.fontMono,
                fontSize: typography.size.sm,
                color: colors.textSecondary,
                display: 'block',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
            }}>${displayValue}</span>
        </div>
    `;
}

// ── Toggle ────────────────────────────────────────────────────────────────────

/**
 * Toggle — styled on/off switch.
 * Props:
 *   checked   — boolean
 *   onChange  — callback (no arguments; caller manages state)
 *   disabled  — boolean
 */
export function Toggle({ checked, onChange, disabled }) {
    const trackStyle = {
        display: 'inline-flex',
        alignItems: 'center',
        position: 'relative',
        width: '32px',
        height: '18px',
        borderRadius: layout.borderRadius.pill,
        background: checked ? colors.accent : colors.border,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.5 : 1,
        border: 'none',
        padding: 0,
        flexShrink: 0,
        transition: `background ${animation.durationNormal} ${animation.easing}`,
        outline: 'none',
    };

    const thumbStyle = {
        position: 'absolute',
        left: checked ? '16px' : '2px',
        top: '2px',
        width: '14px',
        height: '14px',
        borderRadius: '50%',
        background: '#ffffff',
        transition: `left ${animation.durationNormal} ${animation.easing}`,
        boxShadow: '0 1px 3px rgba(0,0,0,0.35)',
        pointerEvents: 'none',
    };

    return html`
        <button
            role="switch"
            aria-checked=${String(!!checked)}
            style=${trackStyle}
            onClick=${disabled ? undefined : onChange}
            disabled=${disabled || false}
        >
            <span style=${thumbStyle} />
        </button>
    `;
}

// ── ConfirmAction ─────────────────────────────────────────────────────────────

/**
 * ConfirmAction — button with two-phase confirm flow.
 * First click reveals "Are you sure?" with confirm + cancel.
 * Danger variant makes the confirm button red (not the initial button).
 *
 * Props:
 *   label        — initial button label
 *   confirmLabel — label on the confirm button (default: "Confirm")
 *   warningText  — short warning shown before confirm/cancel buttons
 *   onConfirm    — callback fired on confirmation
 *   danger       — if true, confirm button turns red on hover
 */
export function ConfirmAction({ label, confirmLabel = 'Confirm', warningText, onConfirm, danger }) {
    const [confirming, setConfirming] = useState(false);
    const [hoverConfirm, setHoverConfirm] = useState(false);

    const confirmBtnStyle = danger ? {
        ...styles.button,
        padding: '3px 10px',
        fontSize: '11px',
        color: hoverConfirm ? colors.red : colors.textTertiary,
        borderColor: hoverConfirm ? colors.red : colors.border,
        transition: `color ${animation.durationFast}, border-color ${animation.durationFast}`,
    } : {
        ...styles.buttonPrimary,
        padding: '3px 10px',
        fontSize: '11px',
    };

    const cancelBtnStyle = {
        ...styles.button,
        padding: '3px 10px',
        fontSize: '11px',
    };

    const handleConfirm = () => {
        setConfirming(false);
        onConfirm && onConfirm();
    };

    if (confirming) {
        return html`
            <span style=${{ display: 'inline-flex', alignItems: 'center', gap: spacing[2] }}>
                ${warningText && html`
                    <span style=${{
                        fontSize: '11px',
                        color: colors.textTertiary,
                    }}>${warningText}</span>
                `}
                <button
                    style=${confirmBtnStyle}
                    onMouseEnter=${() => setHoverConfirm(true)}
                    onMouseLeave=${() => setHoverConfirm(false)}
                    onClick=${handleConfirm}
                >${confirmLabel}</button>
                <button
                    style=${cancelBtnStyle}
                    onClick=${() => setConfirming(false)}
                >Cancel</button>
            </span>
        `;
    }

    return html`
        <button
            style=${{ ...styles.button, padding: '3px 10px', fontSize: '11px' }}
            onClick=${() => setConfirming(true)}
        >${label}</button>
    `;
}
