// Shared utilities for the Preact dashboard

import { h } from 'https://esm.sh/preact@10.25.4';
import htm from 'https://esm.sh/htm@3.1.1';

export const html = htm.bind(h);

export function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// Sanitization guard — if DOMPurify CDN fails, escape HTML instead of passing raw.
// Check .sanitize is a function, not just that the global exists, in case the CDN
// loads a partial/broken object.
export const sanitize = typeof DOMPurify?.sanitize === 'function'
    ? (dirty) => DOMPurify.sanitize(dirty)
    : (dirty) => {
        console.warn('DOMPurify not loaded — falling back to HTML escaping');
        return escapeHtml(dirty);
    };

export function relativeTime(iso) {
    if (!iso) return '\u2014';
    const diff = Math.max(0, (Date.now() - new Date(iso + (iso.endsWith('Z') ? '' : 'Z')).getTime()) / 1000);
    if (diff < 5) return 'just now';
    if (diff < 60) return `${Math.floor(diff)}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}

export function progressBar(done, total, len = 10) {
    if (total === 0) return '\u2591'.repeat(len);
    const filled = Math.round(done / total * len);
    return '\u2593'.repeat(filled) + '\u2591'.repeat(len - filled);
}

const STATUS_MAP = {
    working:          { bg: 'bg-amber-500/20', text: 'text-amber-400', icon: '\u25CF', dot: true },
    completed:        { bg: 'bg-blue-500/20', text: 'text-blue-400', icon: '\u2713' },
    failed:           { bg: 'bg-red-500/20', text: 'text-red-400', icon: '\u2715' },
    'needs-review':   { bg: 'bg-amber-500/20', text: 'text-amber-400', icon: '\u26A0' },
    'turns-exhausted':{ bg: 'bg-orange-500/20', text: 'text-orange-400', icon: '\u23F3' },
    cancelled:        { bg: 'bg-slate-500/20', text: 'text-slate-400', icon: '\u2014' },
    ready:            { bg: 'bg-slate-500/20', text: 'text-slate-300', icon: '\u25CB' },
};

export function StatusBadge({ status }) {
    const s = STATUS_MAP[status] || STATUS_MAP.ready;
    const dotClass = s.dot ? 'status-dot-working' : '';
    return html`<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-medium ${s.bg} ${s.text}">
        <span class=${dotClass}>${s.icon}</span> ${(status || 'ready').toUpperCase()}
    </span>`;
}

const GATE_MAP = {
    testing:        { bg: 'bg-violet-500/20', text: 'text-violet-400', icon: '\u2699', pulse: true },
    'test-passed':  { bg: 'bg-emerald-500/20', text: 'text-emerald-400', icon: '\u2713' },
    reviewing:      { bg: 'bg-pink-500/20', text: 'text-pink-400', icon: '\uD83D\uDC41', pulse: true },
    'test-failed':  { bg: 'bg-red-500/20', text: 'text-red-400', icon: '\u2715' },
    'review-failed':{ bg: 'bg-red-500/20', text: 'text-red-400', icon: '\u2715' },
};

export function GateBadge({ task }) {
    if (!task.gate_status || task.gate_status === 'passed') return null;
    const s = GATE_MAP[task.gate_status];
    if (!s) return null;
    const retries = task.gate_retries > 0 ? ` (${task.gate_retries}/${task.max_gate_retries || 3})` : '';
    const pulseClass = s.pulse ? 'status-dot-working' : '';
    return html`<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-medium ${s.bg} ${s.text}">
        <span class=${pulseClass}>${s.icon}</span> GATE: ${task.gate_status.toUpperCase()}${retries}
    </span>`;
}

export function PrUrlBadge({ task }) {
    const prUrl = task.pr_url || (task.artifacts && task.artifacts.find(a => a.type === 'pr_url')?.ref);
    if (!prUrl) return null;
    // Validate protocol to prevent javascript: XSS
    const safeUrl = (typeof prUrl === 'string' && (prUrl.startsWith('https://') || prUrl.startsWith('http://'))) ? prUrl : '#';
    return html`<a href=${safeUrl} target="_blank" rel="noopener"
        class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-purple-500/20 text-purple-400 hover:bg-purple-500/30">PR \u2197</a>`;
}

export function jiraUrl(ticket, jiraBaseUrl) {
    if (!ticket || typeof ticket !== 'string') return '#';
    if (ticket.startsWith('http')) return ticket;
    if (jiraBaseUrl) return `${jiraBaseUrl}/browse/${ticket}`;
    return '#';
}

export function jiraLabel(ticket) {
    if (!ticket || typeof ticket !== 'string') return '';
    if (ticket.startsWith('http')) {
        const parts = ticket.split('/');
        return parts[parts.length - 1] || ticket;
    }
    return ticket;
}

export function renderMarkdown(content) {
    if (!content) return '';
    return sanitize(marked.parse(content));
}

export function getRoute() {
    const hash = location.hash.slice(1) || '/';
    if (hash.startsWith('/graph/')) return { view: 'graph', projectId: decodeURIComponent(hash.slice(7)) };
    if (hash.startsWith('/tasks/')) return { view: 'detail', taskId: hash.slice(7) };
    if (hash.startsWith('/conversations/')) return { view: 'conversation-detail', convId: decodeURIComponent(hash.slice(15)) };
    if (hash === '/conversations') return { view: 'conversations' };
    if (hash === '/projects') return { view: 'projects' };
    return { view: 'board', params: Object.fromEntries(new URLSearchParams(hash.slice(2))) };
}

export function navigate(hash) {
    location.hash = hash;
}

export function ActionButtons({ task, onAction, stopPropagation }) {
    const btn = (action, label, colorClass) => html`
        <button onClick=${(e) => { if (stopPropagation) e.stopPropagation(); onAction(action, task.id); }}
            class="px-2 py-1 text-xs rounded ${colorClass}">${label}</button>`;

    const btns = [];
    if (task.status === 'working') {
        btns.push(btn('cancel', 'Cancel', 'bg-red-500/20 text-red-400 hover:bg-red-500/30'));
    }
    if (task.status === 'failed' || task.status === 'cancelled') {
        btns.push(btn('retry', 'Retry', 'bg-amber-500/20 text-amber-400 hover:bg-amber-500/30'));
    }
    if (task.status === 'completed') {
        btns.push(btn('resume', 'Resume', 'bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30'));
        btns.push(btn('retry', 'Retry', 'bg-amber-500/20 text-amber-400 hover:bg-amber-500/30'));
        btns.push(btn('close', 'Close', 'bg-slate-500/20 text-slate-400 hover:bg-slate-500/30'));
    }
    if (task.status === 'needs-review' || task.status === 'turns-exhausted') {
        btns.push(btn('resume', 'Resume', 'bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30'));
        btns.push(btn('retry', 'Retry', 'bg-amber-500/20 text-amber-400 hover:bg-amber-500/30'));
        btns.push(btn('cancel', 'Cancel', 'bg-red-500/20 text-red-400 hover:bg-red-500/30'));
    }
    if (task.gate_status && ['testing', 'test-passed', 'reviewing', 'test-failed', 'review-failed'].includes(task.gate_status)) {
        btns.push(btn('skip-gate', 'Skip Gate', 'bg-violet-500/20 text-violet-400 hover:bg-violet-500/30'));
    }
    if (task.status === 'completed' && task.gate_status === 'passed') {
        btns.push(btn('advance-chain', 'Advance Chain', 'bg-indigo-500/20 text-indigo-400 hover:bg-indigo-500/30'));
    }
    if (task.depends_on || task.gate_status) {
        btns.push(btn('cancel-chain', 'Cancel Chain', 'bg-red-500/10 text-red-400/70 hover:bg-red-500/20'));
    }
    return html`<span class="flex gap-2 flex-wrap">${btns}</span>`;
}
