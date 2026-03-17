// Shared utilities for the Preact dashboard

import { h } from 'https://esm.sh/preact@10.25.4';
import { useState, useCallback } from 'https://esm.sh/preact@10.25.4/hooks';
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

// ── Tooltip Component ────────────────────────────────────────
export function Tip({ text, children }) {
    return html`<span class="tip">
        ${children}
        <span class="tip-text">${text}</span>
    </span>`;
}

// ── Confirmation Dialog ──────────────────────────────────────
const CONFIRM_CONFIGS = {
    cancel: {
        title: 'Cancel Task',
        message: (taskId) => `Kill the running CC process for "${taskId}"? Code changes in the worktree will be preserved. You can resume or retry later.`,
        confirmLabel: 'Cancel Task',
        btnClass: 'confirm-btn-danger',
    },
    retry: {
        title: 'Retry Task',
        message: (taskId) => `Start a new CC session for "${taskId}"? Previous session context will be lost. Review feedback from the last attempt will be injected.`,
        confirmLabel: 'Retry',
        btnClass: 'confirm-btn-primary',
    },
    resume: {
        title: 'Resume Session',
        message: (taskId) => `Continue the existing CC session for "${taskId}" with full conversation history?`,
        confirmLabel: 'Resume',
        btnClass: 'confirm-btn-primary',
    },
    close: {
        title: 'Close Task',
        message: (taskId) => `Close "${taskId}"? This will destroy the worktree and delete the branch. This cannot be undone.`,
        confirmLabel: 'Close Task',
        btnClass: 'confirm-btn-danger',
    },
    'skip-gate': {
        title: 'Skip Gate',
        message: (taskId) => `Manually mark the gate as passed for "${taskId}"? This bypasses test and review validation.`,
        confirmLabel: 'Skip Gate',
        btnClass: 'confirm-btn-primary',
    },
    'advance-chain': {
        title: 'Advance Chain',
        message: () => `Dispatch the next dependent task in the chain? The current task must be completed and passed all gates.`,
        confirmLabel: 'Advance',
        btnClass: 'confirm-btn-primary',
    },
    'cancel-chain': {
        title: 'Cancel Chain',
        message: (taskId) => `Cancel "${taskId}" and ALL dependent tasks in the chain? This cannot be undone.`,
        confirmLabel: 'Cancel Chain',
        btnClass: 'confirm-btn-danger',
    },
    'release-worktree': {
        title: 'Release Worktree',
        message: (taskId) => `Detach the worktree from "${taskId}" without closing the task? The branch will be freed for new work. Code changes in the worktree will be removed.`,
        confirmLabel: 'Release Worktree',
        btnClass: 'confirm-btn-primary',
    },
};

export function ConfirmDialog({ action, taskId, onConfirm, onCancel }) {
    if (!action) return null;
    const config = CONFIRM_CONFIGS[action] || {
        title: action,
        message: (id) => `${action} task "${id}"?`,
        confirmLabel: action,
        btnClass: 'confirm-btn-primary',
    };

    const handleKeyDown = useCallback((e) => {
        if (e.key === 'Escape') onCancel();
    }, [onCancel]);

    return html`
        <div class="confirm-overlay" onClick=${onCancel} onKeyDown=${handleKeyDown} tabindex="-1">
            <div class="confirm-dialog" onClick=${(e) => e.stopPropagation()}>
                <h3>${config.title}</h3>
                <p>${config.message(taskId)}</p>
                <div class="confirm-actions">
                    <button class="confirm-btn confirm-btn-cancel" onClick=${onCancel}>Nevermind</button>
                    <button class="confirm-btn ${config.btnClass}" onClick=${onConfirm}>${config.confirmLabel}</button>
                </div>
            </div>
        </div>
    `;
}

const STATUS_MAP = {
    working:          { bg: 'bg-amber-500/20', text: 'text-amber-400', icon: '\u25CF', dot: true, explain: 'Task is running — CC is actively working' },
    completed:        { bg: 'bg-blue-500/20', text: 'text-blue-400', icon: '\u2713', explain: 'Task finished successfully' },
    failed:           { bg: 'bg-red-500/20', text: 'text-red-400', icon: '\u2715', explain: 'Task ended with an error' },
    'needs-review':   { bg: 'bg-amber-500/20', text: 'text-amber-400', icon: '\u26A0', explain: 'Task needs human review or input' },
    'turns-exhausted':{ bg: 'bg-orange-500/20', text: 'text-orange-400', icon: '\u23F3', explain: 'CC ran out of turns — may need manual resume' },
    cancelled:        { bg: 'bg-slate-500/20', text: 'text-slate-400', icon: '\u2014', explain: 'Task was cancelled by user' },
    ready:            { bg: 'bg-slate-500/20', text: 'text-slate-300', icon: '\u25CB', explain: 'Task is ready to be dispatched' },
};

export function StatusBadge({ status }) {
    const s = STATUS_MAP[status] || STATUS_MAP.ready;
    const dotClass = s.dot ? 'status-dot-working' : '';
    const label = (status || 'ready').toUpperCase();
    return html`<${Tip} text="${label} — ${s.explain}">
        <span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-medium ${s.bg} ${s.text}">
            <span class=${dotClass}>${s.icon}</span> ${label}
        </span>
    <//>`;
}

const GATE_MAP = {
    testing:        { bg: 'bg-violet-500/20', text: 'text-violet-400', icon: '\u2699', pulse: true, explain: 'Automated tests are running' },
    'test-passed':  { bg: 'bg-emerald-500/20', text: 'text-emerald-400', icon: '\u2713', explain: 'Tests passed' },
    reviewing:      { bg: 'bg-pink-500/20', text: 'text-pink-400', icon: '\uD83D\uDC41', pulse: true, explain: 'Automated code review is running' },
    'test-failed':  { bg: 'bg-red-500/20', text: 'text-red-400', icon: '\u2715', explain: 'Tests failed' },
    'review-failed':{ bg: 'bg-red-500/20', text: 'text-red-400', icon: '\u2715', explain: 'Code review found issues' },
};

export function GateBadge({ task }) {
    if (!task.gate_status || task.gate_status === 'passed') return null;
    const s = GATE_MAP[task.gate_status];
    if (!s) return null;
    const retries = task.gate_retries > 0 ? ` (attempt ${task.gate_retries + 1}/${task.max_gate_retries || 3})` : '';
    const pulseClass = s.pulse ? 'status-dot-working' : '';
    const tipText = `${s.explain}${retries}`;
    return html`<${Tip} text=${tipText}>
        <span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-medium ${s.bg} ${s.text}">
            <span class=${pulseClass}>${s.icon}</span> GATE: ${task.gate_status.toUpperCase()}${retries}
        </span>
    <//>`;
}

export function PrUrlBadge({ task }) {
    const prUrl = task.pr_url || (task.artifacts && task.artifacts.find(a => a.type === 'pr_url')?.ref);
    if (!prUrl) return null;
    // Validate protocol to prevent javascript: XSS
    const safeUrl = (typeof prUrl === 'string' && (prUrl.startsWith('https://') || prUrl.startsWith('http://'))) ? prUrl : '#';
    return html`<${Tip} text="View pull request on GitHub">
        <a href=${safeUrl} target="_blank" rel="noopener"
            class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-purple-500/20 text-purple-400 hover:bg-purple-500/30">PR \u2197</a>
    <//>`;
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
    if (hash === '/settings') return { view: 'settings' };
    return { view: 'board', params: Object.fromEntries(new URLSearchParams(hash.slice(2))) };
}

export function navigate(hash) {
    location.hash = hash;
}

// ── Loading / Error / Empty States ───────────────────────────
export function LoadingState({ message = 'Loading...' }) {
    return html`<div class="flex items-center justify-center p-8 gap-3">
        <span class="loading-spinner"></span>
        <span class="text-sm" style="color: var(--text-muted)">${message}</span>
    </div>`;
}

export function ErrorState({ message, onRetry }) {
    return html`<div class="error-state">
        <div class="error-state-icon">\u26A0</div>
        <div class="error-state-msg">${message || 'Something went wrong'}</div>
        ${onRetry ? html`<button class="error-state-retry" onClick=${onRetry}>Retry</button>` : null}
    </div>`;
}

export function EmptyState({ message }) {
    return html`<div class="flex items-center justify-center p-8">
        <span class="text-sm" style="color: var(--text-faint)">${message || 'Nothing here yet'}</span>
    </div>`;
}

// ── Worktree Indicator ───────────────────────────────────────
export function WorktreeIndicator({ task }) {
    if (!task.worktree_path) return null;
    const isCompleted = ['completed', 'merged'].includes(task.status);
    const cls = isCompleted ? 'worktree-amber' : 'worktree-active';
    const tip = isCompleted
        ? `Worktree still attached — consider releasing (${task.worktree_path})`
        : `Worktree attached at ${task.worktree_path}`;
    return html`<${Tip} text=${tip}>
        <span class="worktree-icon ${cls}">\uD83D\uDCBE${isCompleted ? ' \u26A0' : ''}</span>
    <//>`;
}

// ── Heartbeat Indicator ──────────────────────────────────────
export function HeartbeatIndicator({ task }) {
    if (task.status !== 'working') return null;
    const la = task.last_activity;
    if (!la) return html`<${Tip} text="No activity data">
        <span class="inline-block w-2 h-2 rounded-full heartbeat-dead"></span>
    <//>`;

    const age = (Date.now() - new Date(la + (la.endsWith('Z') ? '' : 'Z')).getTime()) / 1000;
    let cls, label;
    if (age > 300) { cls = 'heartbeat-dead'; label = `No activity for ${Math.floor(age / 60)}m — may be stalled`; }
    else if (age > 120) { cls = 'heartbeat-stale'; label = `Last activity ${Math.floor(age / 60)}m ago — possibly stale`; }
    else { cls = 'heartbeat-active'; label = `Active — last activity ${Math.floor(age)}s ago`; }

    return html`<${Tip} text=${label}>
        <span class="inline-block w-2 h-2 rounded-full ${cls}"></span>
    <//>`;
}

// ── Claude Chat URL ──────────────────────────────────────────
export function ClaudeChatLink({ url }) {
    if (!url) return null;
    const safeUrl = (typeof url === 'string' && (url.startsWith('https://') || url.startsWith('http://'))) ? url : '#';
    return html`<${Tip} text="Open this conversation in Claude">
        <a href=${safeUrl} target="_blank" rel="noopener"
            class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-orange-500/20 text-orange-400 hover:bg-orange-500/30">
            Open in Claude \u2197
        </a>
    <//>`;
}

// ── Action Buttons with tooltips ─────────────────────────────
export const BUTTON_TOOLTIPS = {
    cancel: 'Kill the running CC process. Code changes are preserved in the worktree.',
    retry: 'Start a fresh CC session. Previous review feedback will be injected.',
    resume: 'Continue the existing CC session with full conversation history.',
    close: 'Clean up worktree, delete branch, archive task.',
    'skip-gate': 'Bypass automated tests/review. Mark gate as passed manually.',
    'advance-chain': 'Dispatch the next dependent task in the chain.',
    'cancel-chain': 'Cancel this task and all dependent tasks in the chain.',
    'release-worktree': 'Detach worktree without closing the task. Frees the branch for new work.',
};

export function ActionButtons({ task, onAction, stopPropagation }) {
    const btn = (action, label, colorClass) => html`
        <${Tip} text=${BUTTON_TOOLTIPS[action] || action}>
            <button onClick=${(e) => { if (stopPropagation) e.stopPropagation(); onAction(action, task.id); }}
                class="px-2 py-1 text-xs rounded ${colorClass}">${label}</button>
        <//>`;

    const btns = [];
    if (task.status === 'working') {
        btns.push(btn('cancel', 'Cancel', 'bg-red-500/20 text-red-400 hover:bg-red-500/30'));
    }
    if (task.status === 'failed' || task.status === 'cancelled') {
        btns.push(btn('retry', 'Retry (fresh)', 'bg-amber-500/20 text-amber-400 hover:bg-amber-500/30'));
    }
    if (task.status === 'completed') {
        btns.push(btn('resume', 'Resume session', 'bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30'));
        btns.push(btn('retry', 'Retry (fresh)', 'bg-amber-500/20 text-amber-400 hover:bg-amber-500/30'));
        btns.push(btn('close', 'Close', 'bg-slate-500/20 text-slate-400 hover:bg-slate-500/30'));
    }
    if (task.status === 'needs-review' || task.status === 'turns-exhausted') {
        btns.push(btn('resume', 'Resume session', 'bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30'));
        btns.push(btn('retry', 'Retry (fresh)', 'bg-amber-500/20 text-amber-400 hover:bg-amber-500/30'));
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
    if (task.worktree_path) {
        btns.push(btn('release-worktree', 'Release Worktree', 'bg-orange-500/20 text-orange-400 hover:bg-orange-500/30'));
    }
    return html`<span class="flex gap-2 flex-wrap">${btns}</span>`;
}
