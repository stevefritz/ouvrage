// ActivityBar — persistent global bar showing live activity across all projects
import { useState, useEffect, useCallback, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { html, relativeTime } from './utils.js';

// Derive short component name from task ID (strips version suffix)
function deriveComponent(taskId) {
    if (!taskId) return '';
    const slug = taskId.split('/').pop() || taskId;
    return slug.replace(/-v\d+.*$/, '') || slug;
}

// Shorten a goal string to fit inline
function shortGoal(goal, maxLen = 40) {
    if (!goal) return '(untitled)';
    return goal.length > maxLen ? goal.slice(0, maxLen - 1) + '…' : goal;
}

// Format event as a brief inline description
function eventLabel(ev) {
    if (ev.source === 'diff') {
        const name = shortGoal(ev.taskGoal, 32);
        if (ev.type === 'completed') return `✓ ${name} completed${ev.cost ? ` — $${ev.cost.toFixed(2)}` : ''}`;
        if (ev.type === 'failed') return `✕ ${name} failed`;
        if (ev.type === 'needs-review') return `⚠ ${name} needs review`;
        if (ev.type === 'gate') {
            const gs = ev.gateStatus;
            if (gs === 'testing') return `⚙ ${name}: testing started`;
            if (gs === 'test-passed') return `✓ ${name}: tests passed`;
            if (gs === 'test-failed') return `✕ ${name}: tests failed`;
            if (gs === 'reviewing') return `● ${name}: review started`;
            if (gs === 'review-failed') return `✕ ${name}: review failed`;
            return `● ${name}: ${gs}`;
        }
        // Generic status change
        return `● ${name}: ${ev.from} → ${ev.to}`;
    }
    // Server-sourced message event
    const name = shortGoal(ev.taskGoal, 32);
    if (ev.event_type === 'result') {
        const status = ev.task_status;
        if (status === 'completed') return `✓ ${name} completed${ev.total_cost_usd ? ` — $${Number(ev.total_cost_usd).toFixed(2)}` : ''}`;
        if (status === 'failed') return `✕ ${name} failed`;
        return `✓ ${name} result`;
    }
    if (ev.event_type === 'test-result') return `⚙ ${name}: ${ev.title || 'gate result'}`;
    if (ev.event_type === 'review') return `👁 ${name}: ${ev.title || 'review'}`;
    if (ev.event_type === 'handoff') return `→ ${name}: handoff`;
    if (ev.event_type === 'status') return `● ${name}: ${ev.title || 'status'}`;
    return `● ${name}`;
}

// Color class for event dot
function eventDotClass(ev) {
    const type = ev.source === 'diff' ? ev.type : ev.event_type;
    if (type === 'completed' || type === 'test-passed') return 'text-blue-400';
    if (type === 'failed' || type === 'test-failed' || type === 'review-failed') return 'text-red-400';
    if (type === 'needs-review') return 'text-amber-400';
    if (type === 'result') {
        if (ev.task_status === 'completed') return 'text-blue-400';
        if (ev.task_status === 'failed') return 'text-red-400';
        return 'text-slate-400';
    }
    if (type === 'review') return 'text-pink-400';
    if (type === 'test-result') return 'text-violet-400';
    return 'text-slate-400';
}

const MAX_EVENTS = 20;
const POLL_INTERVAL_MS = 10000;

export function ActivityBar() {
    const [tasks, setTasks] = useState([]);
    const [events, setEvents] = useState([]);
    const [expanded, setExpanded] = useState(false);
    const prevMapRef = useRef(new Map());
    const mountedRef = useRef(true);

    useEffect(() => {
        mountedRef.current = true;
        return () => { mountedRef.current = false; };
    }, []);

    const load = useCallback(async () => {
        try {
            const all = await api.getTasks();
            if (!mountedRef.current) return;

            const newEvents = [];
            const now = new Date().toISOString();

            const isFirstLoad = prevMapRef.current.size === 0;

            for (const t of all) {
                const prev = prevMapRef.current.get(t.id);
                if (!prev) {
                    // First load: seed events for tasks already in actionable states
                    // so the bar isn't empty when the user opens the dashboard mid-activity.
                    // Skip working (shown as spinners) and completed/failed (stale history).
                    if (isFirstLoad && (t.status === 'needs-review' || t.status === 'turns-exhausted')) {
                        newEvents.push({
                            id: `${t.id}:init:${Date.now()}`,
                            source: 'diff',
                            type: t.status,
                            taskId: t.id,
                            taskGoal: t.goal,
                            from: null,
                            to: t.status,
                            cost: t.total_cost_usd,
                            ts: t.last_activity || t.updated_at || now,
                        });
                    }
                    continue;
                }

                if (prev.status !== t.status) {
                    const type = t.status === 'completed' ? 'completed'
                        : t.status === 'failed' ? 'failed'
                        : t.status === 'needs-review' ? 'needs-review'
                        : 'status-change';
                    newEvents.push({
                        id: `${t.id}:s:${Date.now()}`,
                        source: 'diff',
                        type,
                        taskId: t.id,
                        taskGoal: t.goal,
                        from: prev.status,
                        to: t.status,
                        cost: t.total_cost_usd,
                        ts: now,
                    });
                }

                if (prev.gate_status !== t.gate_status && t.gate_status && t.gate_status !== 'passed') {
                    newEvents.push({
                        id: `${t.id}:g:${Date.now()}`,
                        source: 'diff',
                        type: 'gate',
                        taskId: t.id,
                        taskGoal: t.goal,
                        gateStatus: t.gate_status,
                        ts: now,
                    });
                }
            }

            prevMapRef.current = new Map(all.map(t => [t.id, t]));
            setTasks(all);

            if (newEvents.length > 0) {
                setEvents(prev => [...newEvents, ...prev].slice(0, MAX_EVENTS));
            }
        } catch (e) {
            console.warn('ActivityBar load error:', e);
        }
    }, []);

    useEffect(() => {
        load();
        const timer = setInterval(load, POLL_INTERVAL_MS);
        return () => clearInterval(timer);
    }, [load]);

    const working = tasks.filter(t => t.status === 'working');
    const recentEvents = events.slice(0, 3);

    // Hide the bar entirely when there's nothing to show
    if (working.length === 0 && events.length === 0) return null;

    return html`
        <div class="activity-bar">
            <div class="activity-bar-inner" onClick=${() => events.length > 0 && setExpanded(e => !e)}
                style="cursor: ${events.length > 0 ? 'pointer' : 'default'}">
                <div class="activity-bar-left">
                    ${working.map(t => html`
                        <a href=${'#/tasks/' + encodeURIComponent(t.id)}
                            class="activity-working-task" title=${t.goal}
                            onClick=${(e) => e.stopPropagation()}>
                            <span class="activity-spinner"></span>
                            <span class="activity-task-name">${shortGoal(t.goal, 28)}</span>
                            <span class="activity-elapsed">${relativeTime(t.last_activity || t.updated_at)}</span>
                        </a>
                    `)}
                    ${working.length > 0 && recentEvents.length > 0
                        ? html`<span class="activity-divider">|</span>` : null}
                    ${!expanded && recentEvents.map(ev => html`
                        <span class="activity-event ${eventDotClass(ev)}" key=${ev.id}
                            title=${relativeTime(ev.ts)}>
                            ${eventLabel(ev)}
                        </span>
                    `)}
                </div>
                <div class="activity-bar-right">
                    ${events.length > 0 ? html`
                        <span class="activity-expand-btn">
                            ${expanded ? 'Hide \u25B4' : 'Show \u25BE'}
                        </span>
                    ` : null}
                </div>
            </div>

            ${expanded ? html`
                <div class="activity-feed">
                    ${events.length === 0
                        ? html`<div class="activity-feed-empty">No recent events</div>`
                        : events.map(ev => html`
                        <div class="activity-feed-row" key=${ev.id}>
                            <span class="activity-feed-ts">${relativeTime(ev.ts || ev.created_at)}</span>
                            <span class="activity-feed-label ${eventDotClass(ev)}">${eventLabel(ev)}</span>
                            ${ev.taskId ? html`
                                <a class="activity-feed-task-link"
                                    href=${'#/tasks/' + encodeURIComponent(ev.taskId)}
                                    onClick=${() => setExpanded(false)}>
                                    ${deriveComponent(ev.taskId)}
                                </a>
                            ` : null}
                        </div>
                    `)}
                </div>
            ` : null}
        </div>
    `;
}
