// Foreman Layout Shell — ForemanShell + ForemanHeader
// Import this in your Foreman view entry points.
// Not wired to the existing app.js — additive foundation only.

import { h } from 'https://esm.sh/preact@10.25.4';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, layout } from './tokens.js';

const html = htm.bind(h);

/**
 * ForemanHeader — 52px header bar with "Foreman" branding and "v5" tag.
 * Props:
 *   children — optional right-side content (nav links, user menu, etc.)
 */
export function ForemanHeader({ children }) {
    return html`
        <header class="foreman-header">
            <a href="#/" class="foreman-header-brand">Foreman</a>
            <span class="foreman-header-version">v5</span>
            <span class="foreman-header-spacer"></span>
            <a href="#/files" class="foreman-header-settings">📁 Files</a>
            <a href="#/settings" class="foreman-header-settings">⚙ Settings</a>
            ${children}
        </header>
    `;
}

/**
 * ForemanShell — full-page layout wrapper.
 * Renders header + scrollable content area (max-width 900px, centered).
 * Props:
 *   header    — optional custom header slot (defaults to ForemanHeader)
 *   children  — page content
 */
export function ForemanShell({ header, children }) {
    return html`
        <div id="foreman-app">
            ${header !== undefined ? header : html`<${ForemanHeader} />`}
            <main class="foreman-content">
                ${children}
            </main>
        </div>
    `;
}

/**
 * ForemanPage — convenience wrapper for a titled page inside ForemanShell.
 * Props:
 *   title     — page title string
 *   actions   — optional right-aligned action buttons
 *   children  — page body
 */
export function ForemanPage({ title, actions, children }) {
    const styles = {
        header: {
            display: 'flex',
            alignItems: 'baseline',
            justifyContent: 'space-between',
            marginBottom: layout.spacing?.[6] || '24px',
            paddingBottom: '16px',
            borderBottom: `1px solid ${colors.border}`,
        },
        title: {
            fontFamily: typography.fontBody,
            fontSize: typography.size['2xl'],
            fontWeight: typography.weight.semibold,
            color: colors.text,
            margin: 0,
            letterSpacing: '-0.02em',
        },
    };

    return html`
        <div>
            ${title ? html`
                <div style=${styles.header}>
                    <h1 style=${styles.title}>${title}</h1>
                    ${actions ? html`<div>${actions}</div>` : null}
                </div>
            ` : null}
            ${children}
        </div>
    `;
}
