// TrialBanner — shows a dismissable trial expiry banner when trial_ends_at is set and in the future.
// Dismiss state is stored in localStorage only (no backend persistence).

import { h } from 'https://esm.sh/preact@10.25.4';
import { useState, useEffect } from 'https://esm.sh/preact@10.25.4/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, layout } from '../tokens.js';
import { api } from '../api.js';

const html = htm.bind(h);

const DISMISS_KEY = 'trial_banner_dismissed_at';
const DISMISS_TTL_MS = 12 * 60 * 60 * 1000; // 12 hours
const URGENT_THRESHOLD_MS = 3 * 24 * 60 * 60 * 1000; // 3 days

function isDismissedRecently() {
    try {
        const raw = localStorage.getItem(DISMISS_KEY);
        if (!raw) return false;
        const ts = parseInt(raw, 10);
        if (!ts) return false;
        return Date.now() - ts < DISMISS_TTL_MS;
    } catch (_) {
        return false;
    }
}

function formatDate(isoString) {
    try {
        const d = new Date(isoString);
        return d.toLocaleDateString(undefined, { month: 'long', day: 'numeric', year: 'numeric' });
    } catch (_) {
        return isoString;
    }
}

export function TrialBanner() {
    const [trialEndsAt, setTrialEndsAt] = useState(null);
    const [dismissed, setDismissed] = useState(false);
    const [loaded, setLoaded] = useState(false);

    useEffect(() => {
        // Check dismissal state immediately (synchronous localStorage read)
        if (isDismissedRecently()) {
            setDismissed(true);
        }

        // Fetch system info to get trial_ends_at
        api.getSystem().then(data => {
            setTrialEndsAt(data.trial_ends_at || null);
            setLoaded(true);
        }).catch(() => {
            setLoaded(true);
        });
    }, []);

    function handleDismiss() {
        try {
            localStorage.setItem(DISMISS_KEY, String(Date.now()));
        } catch (_) {}
        setDismissed(true);
    }

    if (!loaded || dismissed || !trialEndsAt) return null;

    const expiryMs = new Date(trialEndsAt).getTime();
    const now = Date.now();

    // Hide permanently if expired or invalid date
    if (!expiryMs || expiryMs <= now) return null;

    const msRemaining = expiryMs - now;
    const isUrgent = msRemaining <= URGENT_THRESHOLD_MS;

    // $200/mo is hardcoded for now — should be read from instance config once available
    const message = `Your free trial ends on ${formatDate(trialEndsAt)}. Your card on file will be charged $200/mo after that.`;

    const bannerStyles = {
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '10px 24px',
        borderBottom: isUrgent
            ? `1px solid ${colors.yellow}`
            : `1px solid ${colors.blue}`,
        background: isUrgent
            ? 'rgba(245, 166, 35, 0.10)'
            : 'rgba(77, 163, 255, 0.08)',
        fontSize: typography.size.sm,
        fontFamily: typography.fontBody,
        color: colors.text,
    };

    const textStyles = {
        flex: 1,
        color: isUrgent ? colors.yellow : colors.blue,
        fontWeight: isUrgent ? typography.weight.medium : typography.weight.normal,
    };

    const dismissStyles = {
        background: 'none',
        border: 'none',
        color: colors.textSecondary,
        cursor: 'pointer',
        fontSize: '16px',
        lineHeight: 1,
        padding: '0 0 0 16px',
        flexShrink: 0,
    };

    return html`
        <div style=${bannerStyles} role="alert">
            <span style=${textStyles}>${message}</span>
            <button
                style=${dismissStyles}
                onClick=${handleDismiss}
                aria-label="Dismiss trial banner"
                title="Dismiss"
            >×</button>
        </div>
    `;
}
