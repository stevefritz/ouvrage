// ActivityTimeline — per-project chronological event feed
import { useState, useEffect, useCallback, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { html, relativeTime } from './utils.js';

const LIMIT = 30;

// Icon and color for each event type
function eventStyle(ev) {
    const type = ev.event_type;
    if (type === 'result') {
        if (ev.task_status === 'completed') return { icon: '✓', cls: 'tl-dot-completed', label: 'Completed' };
        if (ev.task_status === 'failed') return { icon: '✕', cls: 'tl-dot-failed', label: 'Failed' };
        return { icon: '✓', cls: 'tl-dot-done', label: 'Result' };
    }
    if (type === 'test-result') return { icon: '⚙', cls: 'tl-dot-gate', label: 'Gate' };
    if (type === 'review') return { icon: '👁', cls: 'tl-dot-review', label: 'Review' };
    if (type === 'handoff') return { icon: '→', cls: 'tl-dot-handoff', label: 'Handoff' };
    if (type === 'status') return { icon: '●', cls: 'tl-dot-status', label: 'Status' };
    return { icon: '●', cls: 'tl-dot-status', label: type };
}

function shortGoal(goal, max = 48) {
    if (!goal) return '(untitled)';
    return goal.length > max ? goal.slice(0, max - 1) + '…' : goal;
}

// Extract first meaningful line from content for a brief description
function briefContent(content, maxLen = 80) {
    if (!content) return '';
    const firstLine = content.split('\n').find(l => l.trim()) || '';
    const clean = firstLine.replace(/^#+\s*/, '').replace(/\*\*/g, '').trim();
    return clean.length > maxLen ? clean.slice(0, maxLen - 1) + '…' : clean;
}

export function ActivityTimeline({ projectId }) {
    const [events, setEvents] = useState([]);
    const [loading, setLoading] = useState(true);
    const [collapsed, setCollapsed] = useState(false);
    const [offset, setOffset] = useState(0);
    const [hasMore, setHasMore] = useState(false);
    const mountedRef = useRef(true);

    useEffect(() => {
        mountedRef.current = true;
        return () => { mountedRef.current = false; };
    }, []);

    const load = useCallback(async (off = 0, append = false) => {
        try {
            const data = await api.getActivity({ project_id: projectId, limit: LIMIT, offset: off });
            if (!mountedRef.current) return;

            if (append) {
                setEvents(prev => [...prev, ...data]);
            } else {
                setEvents(data);
            }
            setHasMore(data.length === LIMIT);
            setLoading(false);
        } catch (e) {
            console.warn('ActivityTimeline load error:', e);
            setLoading(false);
        }
    }, [projectId]);

    // Initial load + auto-refresh
    useEffect(() => {
        setLoading(true);
        load(0);
        const timer = setInterval(() => load(0), 10000);
        return () => clearInterval(timer);
    }, [load]);

    const loadMore = useCallback(() => {
        const newOffset = offset + LIMIT;
        setOffset(newOffset);
        load(newOffset, true);
    }, [offset, load]);

    if (!loading && events.length === 0) return null;

    return html`
        <div class="activity-timeline">
            <div class="tl-header">
                <h3 class="tl-title">
                    <span class="tl-title-icon">◷</span>
                    Activity
                    ${!loading && events.length > 0
                        ? html`<span class="tl-count">${events.length}${hasMore ? '+' : ''}</span>`
                        : null}
                </h3>
                <button class="tl-collapse-btn" onClick=${() => setCollapsed(c => !c)}>
                    ${collapsed ? 'Show ▾' : 'Hide ▴'}
                </button>
            </div>

            ${!collapsed ? html`
                <div class="tl-body">
                    ${loading && events.length === 0
                        ? html`<div class="tl-loading">Loading…</div>`
                        : events.map(ev => {
                            const s = eventStyle(ev);
                            const desc = ev.title || briefContent(ev.content);
                            const cost = ev.event_type === 'result' && ev.total_cost_usd
                                ? ` — $${Number(ev.total_cost_usd).toFixed(2)}`
                                : '';
                            return html`
                                <div class="tl-row" key=${ev.id}>
                                    <div class="tl-left">
                                        <div class="tl-dot ${s.cls}">${s.icon}</div>
                                        <div class="tl-line"></div>
                                    </div>
                                    <div class="tl-content">
                                        <div class="tl-row-header">
                                            <a class="tl-task-link"
                                                href=${'#/tasks/' + encodeURIComponent(ev.task_id)}>
                                                ${shortGoal(ev.task_goal)}
                                            </a>
                                            <span class="tl-ts">${relativeTime(ev.created_at)}</span>
                                        </div>
                                        ${desc ? html`
                                            <div class="tl-desc">${desc}${cost}</div>
                                        ` : cost ? html`<div class="tl-desc">${cost.slice(4)}</div>` : null}
                                    </div>
                                </div>
                            `;
                        })
                    }

                    ${hasMore ? html`
                        <button class="tl-load-more" onClick=${loadMore}>
                            Load more
                        </button>
                    ` : null}
                </div>
            ` : null}
        </div>
    `;
}
