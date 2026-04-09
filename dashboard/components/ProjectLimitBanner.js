// ProjectLimitBanner — persistent amber banner shown when over_project_limit is true.
// Fetches /dashboard/api/system and polls every 30s.
// Not dismissable — this is a hard account limit, not a notification.

import { h } from 'https://esm.sh/preact@10.25.4';
import { useState, useEffect } from 'https://esm.sh/preact@10.25.4/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography } from '../tokens.js';
import { api } from '../api.js';

const html = htm.bind(h);

const POLL_INTERVAL_MS = 30 * 1000; // 30 seconds

// Warning triangle SVG icon
function WarningIcon() {
    return html`
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none"
            xmlns="http://www.w3.org/2000/svg"
            style=${{ flexShrink: 0, marginTop: '1px' }}
            aria-hidden="true">
            <path d="M8 1.5L14.5 13H1.5L8 1.5Z"
                stroke="${colors.accent}" stroke-width="1.5"
                stroke-linejoin="round" fill="none" />
            <path d="M8 6V9" stroke="${colors.accent}" stroke-width="1.5"
                stroke-linecap="round" />
            <circle cx="8" cy="11" r="0.75" fill="${colors.accent}" />
        </svg>
    `;
}

export function ProjectLimitBanner() {
    const [overLimit, setOverLimit] = useState(false);
    const [projectsCount, setProjectsCount] = useState(0);
    const [maxProjects, setMaxProjects] = useState(0);

    function fetchSystem() {
        api.getSystem().then(data => {
            setOverLimit(!!data.over_project_limit);
            setProjectsCount(data.projects_count || 0);
            setMaxProjects(data.max_projects || 0);
        }).catch(() => {
            // Silently ignore — don't flash error for system poll failure
        });
    }

    useEffect(() => {
        fetchSystem();
        const timer = setInterval(fetchSystem, POLL_INTERVAL_MS);
        return () => clearInterval(timer);
    }, []);

    if (!overLimit) return null;

    const bannerStyle = {
        display: 'flex',
        alignItems: 'flex-start',
        gap: '10px',
        padding: '10px 24px',
        borderLeft: `3px solid ${colors.accent}`,
        borderBottom: `1px solid rgba(217, 119, 6, 0.25)`,
        background: colors.accentBg,
        fontSize: typography.size.sm,
        fontFamily: typography.fontBody,
        color: colors.text,
        lineHeight: typography.lineHeight.normal,
    };

    const textStyle = {
        flex: 1,
        color: colors.text,
    };

    return html`
        <div style=${bannerStyle} role="alert" aria-live="polite">
            <${WarningIcon} />
            <span style=${textStyle}>
                You have ${projectsCount} projects. Your plan allows ${maxProjects}.
                Upgrade your plan or remove projects to dispatch tasks.
            </span>
        </div>
    `;
}
