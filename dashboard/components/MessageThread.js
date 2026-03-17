import { useState, useCallback } from 'https://esm.sh/preact@10.25.4/hooks';
import { html, escapeHtml, renderMarkdown } from './utils.js';

const BORDER_COLORS = {
    spec: 'border-l-blue-500',
    progress: 'border-l-emerald-500',
    question: 'border-l-amber-500',
    status: 'border-l-slate-500',
    result: 'border-l-purple-500',
    review: 'border-l-pink-500',
    answer: 'border-l-cyan-500',
    'test-result': 'border-l-violet-500',
    handoff: 'border-l-teal-500',
    plan: 'border-l-teal-500',
    note: 'border-l-slate-600',
};

function Message({ msg, index, expandedMessages, onToggle, idPrefix = 'msg' }) {
    const border = BORDER_COLORS[msg.type] || 'border-l-slate-600';
    const pinIcon = msg.pinned || msg._pinned_marker ? '\uD83D\uDCCC ' : '';
    const type = (msg.type || 'note').toUpperCase();
    const time = msg.created_at ? new Date(msg.created_at + (msg.created_at.endsWith('Z') ? '' : 'Z')).toLocaleTimeString() : '';
    const contentHtml = renderMarkdown(msg.content);
    const isLong = (msg.content || '').length > 500;
    const collapseId = `${idPrefix}-${msg.id || index}`;
    const isExpanded = expandedMessages.has(collapseId);

    return html`
        <div class="border-l-2 ${border} bg-slate-800/50 rounded-r mb-3">
            <div class="flex items-center gap-2 px-3 py-1.5 text-xs text-slate-400 border-b border-slate-700/50">
                <span>${pinIcon}${type}</span>
                <span>\u2014</span>
                <span>${escapeHtml(msg.author || '')}</span>
                <span>\u2014</span>
                <span>${time}</span>
                ${msg.title ? html`<span class="text-slate-300 ml-1">${escapeHtml(msg.title)}</span>` : null}
            </div>
            <div class="px-3 py-2 prose-dark text-sm ${isLong && !isExpanded ? 'msg-collapsed' : ''}"
                dangerouslySetInnerHTML=${{ __html: contentHtml }}>
            </div>
            ${isLong ? html`
                <button onClick=${() => onToggle(collapseId)}
                    class="px-3 py-1 text-xs text-slate-400 hover:text-slate-200">
                    ${isExpanded ? 'Collapse \u25B4' : 'Expand \u25BE'}
                </button>
            ` : null}
        </div>
    `;
}

export function MessageThread({ messages, filterPlan = false, idPrefix = 'msg' }) {
    const [expandedMessages, setExpandedMessages] = useState(new Set());

    const onToggle = useCallback((collapseId) => {
        setExpandedMessages(prev => {
            const next = new Set(prev);
            if (next.has(collapseId)) next.delete(collapseId);
            else next.add(collapseId);
            return next;
        });
    }, []);

    const msgs = filterPlan ? (messages || []).filter(m => m.type !== 'plan') : (messages || []);

    if (msgs.length === 0) {
        return html`<p class="text-slate-500 text-sm">No messages yet</p>`;
    }

    return html`
        ${msgs.map((m, i) => html`
            <${Message} key=${m.id || i} msg=${m} index=${i}
                expandedMessages=${expandedMessages} onToggle=${onToggle} idPrefix=${idPrefix} />
        `)}
    `;
}
