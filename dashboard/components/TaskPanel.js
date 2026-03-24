// TaskPanel — standalone slide-out panel for task detail
// Used in graph views, project views, and component views
import { useState, useEffect, useRef, useCallback, useMemo } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { html, relativeTime, renderMarkdown, navigate, StatusBadge, ActionButtons, Tip, PrUrlBadge, LoadingState, ErrorState } from './utils.js';
import { MessageThread } from './MessageThread.js';

// ── Status colors for dots ───────────────────────────────────
const DOT_COLORS = {
    done: '#22c55e',
    active: '#3b82f6',
    failed: '#ef4444',
    pending: '#475569',
};

// ── Gate Pipeline Dots ───────────────────────────────────────
function GateDots({ task }) {
    if (!task.auto_test && !task.auto_review) return null;

    const stages = [];
    stages.push({ label: 'Task', status: task.status === 'completed' ? 'done' : task.status === 'working' ? 'active' : 'pending' });

    if (task.auto_test) {
        const gs = task.gate_status;
        let s = 'pending';
        if (gs === 'testing') s = 'active';
        else if (gs === 'test-failed') s = 'failed';
        else if (['test-passed', 'reviewing', 'review-failed', 'passed'].includes(gs)) s = 'done';
        else if (task.status === 'completed') s = 'done';
        stages.push({ label: 'Tests', status: s });
    }

    if (task.auto_review) {
        const gs = task.gate_status;
        let s = 'pending';
        if (gs === 'reviewing') s = 'active';
        else if (gs === 'review-failed') s = 'failed';
        else if (gs === 'passed') s = 'done';
        stages.push({ label: 'Review', status: s });
    }

    stages.push({ label: 'Advance', status: task.gate_status === 'passed' ? 'done' : 'pending' });

    return html`
        <div class="flex items-center gap-3 py-3 border-t border-b" style="border-color: var(--border-primary)">
            ${stages.map((st, i) => html`
                <div key=${i} class="flex items-center gap-2">
                    <div class="flex flex-col items-center gap-1">
                        <div class="w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold"
                            style="background: ${DOT_COLORS[st.status]}20; color: ${DOT_COLORS[st.status]}; ${st.status === 'active' ? 'animation: pulse 2s ease-in-out infinite;' : ''}">
                            ${st.status === 'done' ? '\u2713' : st.status === 'failed' ? '\u2715' : st.status === 'active' ? '\u25CF' : '\u25CB'}
                        </div>
                        <span class="text-xs" style="color: var(--text-faint)">${st.label}</span>
                    </div>
                    ${i < stages.length - 1 ? html`<div class="w-6 h-px" style="background: var(--border-secondary)"></div>` : null}
                </div>
            `)}
        </div>
    `;
}

// ── Test Result Line ─────────────────────────────────────────
function TestResult({ subtasks }) {
    const tests = (subtasks || []).filter(s => s.type === 'test');
    if (tests.length === 0) return null;
    const latest = tests[tests.length - 1];

    if (latest.status === 'completed') {
        return html`<div class="text-sm" style="color: #4ade80">\u2713 Tests passed</div>`;
    }
    if (latest.status === 'failed') {
        const excerpt = (latest.result || '').slice(0, 80);
        return html`<div class="text-sm" style="color: #f87171">\u2715 Tests failed${excerpt ? ` \u2014 ${excerpt}` : ''}</div>`;
    }
    if (latest.status === 'working') {
        return html`<div class="text-sm" style="color: #fbbf24">\u25CF Tests running...</div>`;
    }
    return null;
}

// ── Review Verdict Line ──────────────────────────────────────
function ReviewVerdict({ subtasks }) {
    const reviews = (subtasks || []).filter(s => s.type === 'review');
    if (reviews.length === 0) return null;
    const latest = reviews[reviews.length - 1];

    if (latest.status === 'completed') {
        const result = (latest.result || '').toLowerCase();
        if (result.includes('changes requested') || result.includes('changes_requested')) {
            const excerpt = (latest.result || '').slice(0, 80);
            return html`<div class="text-sm" style="color: #f87171">\u2715 REJECTED${excerpt ? ` \u2014 ${excerpt}` : ''}</div>`;
        }
        return html`<div class="text-sm" style="color: #4ade80">\u2713 APPROVED</div>`;
    }
    if (latest.status === 'failed') {
        return html`<div class="text-sm" style="color: #f87171">\u2715 REVIEW FAILED</div>`;
    }
    if (latest.status === 'working') {
        return html`<div class="text-sm" style="color: #fbbf24">\u25CF Review running...</div>`;
    }
    return null;
}

// ── Chain Position ───────────────────────────────────────────
function ChainPosition({ taskId, onSelectTask }) {
    const [chain, setChain] = useState(null);
    const [currentIdx, setCurrentIdx] = useState(-1);

    useEffect(() => {
        api.getChain(taskId)
            .then(data => {
                if (data && data.chain && data.chain.length > 1) {
                    setChain(data.chain.filter(t => !t.parent_task_id));
                    // Recalculate index after filtering
                    const filtered = data.chain.filter(t => !t.parent_task_id);
                    const idx = filtered.findIndex(t => t.id === taskId);
                    setCurrentIdx(idx >= 0 ? idx : data.current_index);
                } else {
                    setChain(null);
                }
            })
            .catch(() => setChain(null));
    }, [taskId]);

    if (!chain || chain.length <= 1) return null;

    const prev = currentIdx > 0 ? chain[currentIdx - 1] : null;
    const next = currentIdx < chain.length - 1 ? chain[currentIdx + 1] : null;
    const nav = (task) => {
        if (onSelectTask) onSelectTask(task.id);
        else navigate(`#/tasks/${task.id}`);
    };

    return html`
        <div class="flex items-center gap-2 py-2 border-t" style="border-color: var(--border-primary)">
            ${prev ? html`<button onClick=${() => nav(prev)}
                class="text-xs px-2 py-1 rounded" style="background: var(--bg-secondary); color: var(--text-muted)">\u2190</button>` : null}
            <span class="text-sm" style="color: var(--text-muted)">Step ${currentIdx + 1} of ${chain.length}</span>
            ${next ? html`<button onClick=${() => nav(next)}
                class="text-xs px-2 py-1 rounded" style="background: var(--bg-secondary); color: var(--text-muted)">\u2192</button>` : null}
        </div>
    `;
}

// ── Session Log Per Attempt ──────────────────────────────────
function AttemptSessionLog({ taskId, attemptNumber, autoRefresh }) {
    const [expanded, setExpanded] = useState(false);
    const [entries, setEntries] = useState([]);
    const [loaded, setLoaded] = useState(false);
    const [showTools, setShowTools] = useState(false);
    const logRef = useRef(null);

    useEffect(() => {
        if (!expanded) return;
        let cancelled = false;
        const load = () => {
            api.getSessionLog(taskId, { attempt: attemptNumber })
                .then(data => {
                    if (cancelled) return;
                    setEntries(data);
                    setLoaded(true);
                    // Auto-scroll to bottom if near bottom
                    if (logRef.current) {
                        const el = logRef.current;
                        const wasAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
                        if (wasAtBottom) {
                            requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
                        }
                    }
                })
                .catch(() => { if (!cancelled) setLoaded(true); });
        };
        load();
        let timer;
        if (autoRefresh) {
            timer = setInterval(load, 5000);
        }
        return () => { cancelled = true; if (timer) clearInterval(timer); };
    }, [expanded, taskId, attemptNumber, autoRefresh]);

    return html`
        <div class="mt-2">
            <button onClick=${() => setExpanded(!expanded)}
                class="text-xs flex items-center gap-1" style="color: var(--text-faint); cursor: pointer">
                ${expanded ? '\u25BE' : '\u25B8'} Session Log
            </button>
            ${expanded ? html`
                <div class="mt-1">
                    <button onClick=${() => setShowTools(!showTools)}
                        class="text-xs px-2 py-0.5 rounded mb-1" style="background: var(--bg-secondary); color: var(--text-faint)">
                        ${showTools ? 'Text only' : 'Show tools'}
                    </button>
                    <pre ref=${logRef} class="text-xs overflow-y-auto whitespace-pre-wrap rounded p-2"
                        style="max-height: 400px; background: var(--bg-primary); color: var(--text-muted)">
                        ${!loaded ? 'Loading...' : entries.length === 0 ? 'No session log' :
                            entries.map(e => {
                                if (e.type === 'AssistantMessage') {
                                    return (e.content || []).map(b => {
                                        if (b.type === 'text') return b.text + '\n';
                                        if (b.type === 'tool_use' && showTools) return `[TOOL] ${b.name}: ${JSON.stringify(b.input).slice(0, 200)}\n`;
                                        return '';
                                    }).join('');
                                }
                                if (e.type === 'UserMessage' && showTools) {
                                    return (e.content || []).map(b => {
                                        if (b.type === 'tool_result') return `[RESULT] ${(b.preview || '').slice(0, 200)}\n`;
                                        return '';
                                    }).join('');
                                }
                                return '';
                            }).join('')
                        }
                    </pre>
                </div>
            ` : null}
        </div>
    `;
}

// ── Attempt Grouping ─────────────────────────────────────────
const ATTEMPT_BOUNDARIES = [
    'Task completed', 'Task failed', 'Dispatch error', 'Turns exhausted',
    'Session killed by signal', 'Rate limited', 'Wall clock timeout',
    'Recovery limit reached',
];

function groupMessagesByAttempt(messages) {
    if (!messages || messages.length === 0) return [];
    const attempts = [];
    let current = { messages: [], outcome: null, number: 1 };

    for (const msg of messages) {
        if (msg.type === 'plan') continue;
        current.messages.push(msg);
        if (msg.author === 'dispatcher' && msg.type === 'status'
            && ATTEMPT_BOUNDARIES.some(b => (msg.title || '').includes(b))) {
            current.outcome = msg.title || 'Completed';
            attempts.push(current);
            current = { messages: [], outcome: null, number: attempts.length + 1 };
        }
    }
    if (current.messages.length > 0) {
        if (current.messages.every(m => m.author === 'dispatcher')) {
            if (attempts.length > 0) {
                const last = attempts[attempts.length - 1];
                last.messages.push(...current.messages);
            } else {
                current.outcome = 'Status';
                attempts.push(current);
            }
        } else {
            current.outcome = 'In Progress';
            attempts.push(current);
        }
    }
    return attempts;
}

// ── Main Panel ──────────────────────────────────────────────
export function TaskPanel({ taskId, allTasks, onClose, onAction, onSelectTask }) {
    const [task, setTask] = useState(null);
    const [error, setError] = useState(null);
    const mountedRef = useRef(true);

    const loadTask = useCallback(async () => {
        try {
            const data = await api.getTask(taskId);
            if (mountedRef.current) { setTask(data); setError(null); }
        } catch (e) {
            if (mountedRef.current && !task) setError(e.message);
        }
    }, [taskId]);

    useEffect(() => {
        mountedRef.current = true;
        setTask(null);
        setError(null);
        loadTask();
        return () => { mountedRef.current = false; };
    }, [taskId]);

    useEffect(() => {
        if (!task) return;
        const gateActive = ['testing', 'test-passed', 'reviewing'].includes(task.gate_status);
        const shouldPoll = task.status === 'working' || task.status === 'needs-review' || gateActive;
        const interval = shouldPoll ? 3000 : 10000;
        const timer = setInterval(loadTask, interval);
        return () => clearInterval(timer);
    }, [task?.status, task?.gate_status, loadTask]);

    if (error) {
        return html`<div class="task-panel">
            <div class="flex items-center justify-between px-4 py-3" style="border-bottom: 1px solid var(--border-primary)">
                <span class="text-sm" style="color: #f87171">Error: ${error}</span>
                <button onClick=${onClose} class="text-lg" style="color: var(--text-faint)">\u00D7</button>
            </div>
        </div>`;
    }

    if (!task) {
        return html`<div class="task-panel">
            <div class="flex items-center justify-between px-4 py-3" style="border-bottom: 1px solid var(--border-primary)">
                <${LoadingState} message="Loading..." />
                <button onClick=${onClose} class="text-lg" style="color: var(--text-faint)">\u00D7</button>
            </div>
        </div>`;
    }

    // Derive display values
    const statusLabel = (task.status || 'ready').toUpperCase();
    const statusDotColor = task.status === 'completed' ? '#3b82f6'
        : task.status === 'working' ? '#f59e0b'
        : task.status === 'failed' ? '#ef4444'
        : task.status === 'needs-review' ? '#f59e0b'
        : '#64748b';

    const attempts = groupMessagesByAttempt(task.messages);
    const attemptCount = Math.max(task.dispatch_count || 1, attempts.length);

    // Attempt summary
    let attemptSummary = null;
    if (attemptCount > 1) {
        if (task.status === 'working') {
            attemptSummary = `Attempt ${attemptCount} \u2014 running`;
        } else if (task.status === 'completed' || task.status === 'failed') {
            attemptSummary = `${attemptCount} attempts`;
        } else {
            attemptSummary = `Attempt ${attemptCount} of ${attemptCount}`;
        }
    }

    // Git flow
    const branch = task.branch;
    const target = task.base_branch || task.branch_target;
    const prUrl = task.pr_url || (task.artifacts && task.artifacts.find(a => a.type === 'pr_url')?.ref);

    // Cost
    const cost = task.total_cost_usd || 0;

    // BlockedBy
    const blockerTask = task.depends_on && allTasks ? allTasks.find(t => t.id === task.depends_on) : null;
    const isActuallyBlocked = blockerTask && !['completed', 'merged', 'cancelled'].includes(blockerTask.status);

    // Action buttons logic
    const actions = [];
    if ((task.status === 'failed' || task.status === 'needs-review' || task.status === 'completed') && task.status !== 'cancelled') {
        actions.push('retry');
    }
    if (task.gate_status === 'advance' || (task.status === 'completed' && task.gate_status === 'passed')) {
        actions.push('advance-chain');
    }
    if (task.status === 'completed' || task.status === 'failed') {
        actions.push('close');
    }
    if (task.status === 'working' || task.status === 'needs-review' || task.status === 'turns-exhausted' || task.status === 'completed') {
        actions.push('resume');
    }

    return html`
        <div class="task-panel">
            <!-- Header: status dot + label + timestamp + close -->
            <div class="flex items-center gap-2 px-4 py-3 sticky top-0 z-10" style="background: var(--bg-card); border-bottom: 1px solid var(--border-primary)">
                <span class="w-2.5 h-2.5 rounded-full shrink-0 ${task.status === 'working' ? 'status-dot-working' : ''}" style="background: ${statusDotColor}"></span>
                <span class="text-xs font-medium uppercase" style="color: ${statusDotColor}">${statusLabel}</span>
                <span class="text-xs" style="color: var(--text-faint)">${relativeTime(task.last_activity || task.updated_at)}</span>
                <button onClick=${onClose} class="ml-auto text-lg shrink-0" style="color: var(--text-faint); cursor: pointer">\u00D7</button>
            </div>

            <!-- Scrollable content -->
            <div class="overflow-y-auto flex-1 px-4 py-3">
                <!-- Goal -->
                <p class="text-sm mb-1" style="color: var(--text-secondary); word-wrap: break-word; overflow-wrap: break-word">${task.goal}</p>
                <div class="text-xs font-mono mb-3" style="color: var(--text-faint)">${task.id}</div>

                <!-- Attempt summary -->
                ${attemptSummary ? html`<div class="text-xs mb-3" style="color: var(--text-muted)">${attemptSummary}</div>` : null}

                <!-- Gate pipeline -->
                <${GateDots} task=${task} />

                <!-- Test result -->
                <${TestResult} subtasks=${task.subtasks} />

                <!-- Review verdict -->
                <${ReviewVerdict} subtasks=${task.subtasks} />

                <!-- Chain position -->
                <${ChainPosition} taskId=${task.id} onSelectTask=${onSelectTask} />

                <!-- Blocked-by banner (only when actually blocked) -->
                ${isActuallyBlocked ? html`
                    <div class="rounded p-2 my-2" style="background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.2)">
                        <div class="text-xs font-medium" style="color: #f59e0b">Blocked by</div>
                        <div class="flex items-center gap-2 mt-1">
                            <${StatusBadge} status=${blockerTask.status} task=${blockerTask} />
                            <span class="text-xs font-mono" style="color: var(--text-muted)">${blockerTask.id.split('/').pop()}</span>
                        </div>
                    </div>
                ` : null}

                <!-- Git flow -->
                ${(branch || prUrl) ? html`
                    <div class="flex items-center gap-2 flex-wrap py-2 border-t" style="border-color: var(--border-primary)">
                        ${branch ? html`<span class="text-xs font-mono px-2 py-0.5 rounded" style="background: var(--bg-secondary); color: var(--text-muted)">${branch}</span>` : null}
                        ${branch && target ? html`<span class="text-xs" style="color: var(--text-faint)">\u2192</span>` : null}
                        ${target ? html`<span class="text-xs font-mono px-2 py-0.5 rounded" style="background: var(--bg-secondary); color: var(--text-muted)">${target}</span>` : null}
                        <${PrUrlBadge} task=${task} />
                    </div>
                ` : null}

                <!-- Cost -->
                ${cost > 0 ? html`<div class="text-xs font-mono py-1" style="color: var(--text-faint)">$${cost.toFixed(2)}</div>` : null}

                <!-- Actions -->
                <div class="py-3 border-t" style="border-color: var(--border-primary)">
                    <${ActionButtons} task=${task} onAction=${onAction} />
                </div>

                <!-- Open full page link -->
                <a href="#/tasks/${task.id}" class="block text-xs py-2 text-center rounded" style="color: var(--link-color); cursor: pointer">
                    Open full page \u2192
                </a>

                <!-- Messages grouped by attempt with per-attempt session logs -->
                ${attempts.length > 0 ? html`
                    <div class="mt-3 pt-3 border-t" style="border-color: var(--border-primary)">
                        <div class="text-xs font-medium mb-2" style="color: var(--text-muted)">
                            Messages${attempts.length > 1 ? ` \u00B7 ${attempts.length} attempts` : ''}
                        </div>
                        ${attempts.map((attempt, idx) => {
                            const isLast = idx === attempts.length - 1;
                            const outcomeColor = attempt.outcome === 'In Progress' ? '#3b82f6' :
                                attempt.outcome?.toLowerCase().includes('fail') || attempt.outcome?.toLowerCase().includes('error') ? '#ef4444' :
                                '#22c55e';
                            return html`
                                <details key=${idx} class="rounded mb-2 overflow-hidden" style="border: 1px solid var(--border-primary)" open=${isLast}>
                                    <summary class="px-3 py-2 text-xs cursor-pointer flex items-center gap-2" style="color: var(--text-secondary)">
                                        <span class="font-medium">Attempt ${idx + 1}</span>
                                        <span style="color: ${outcomeColor}">${attempt.outcome}</span>
                                        <span class="ml-auto" style="color: var(--text-faint)">${attempt.messages.length} msgs</span>
                                    </summary>
                                    <div class="px-2 pb-2">
                                        <${MessageThread} messages=${attempt.messages} idPrefix=${'panel-attempt-' + idx} />
                                        <${AttemptSessionLog} taskId=${taskId} attemptNumber=${idx + 1}
                                            autoRefresh=${isLast && task.status === 'working'} />
                                    </div>
                                </details>
                            `;
                        })}
                    </div>
                ` : null}
            </div>
        </div>
    `;
}
