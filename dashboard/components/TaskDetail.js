import { useState, useEffect, useRef, useCallback } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { html, relativeTime, renderMarkdown, navigate, StatusBadge, GateBadge, PrUrlBadge, ActionButtons, Tip, WorktreeIndicator, HeartbeatIndicator, ClaudeChatLink, LoadingState, ErrorState, jiraUrl, jiraLabel } from './utils.js';
import { MessageThread } from './MessageThread.js';
import { DispatchLogPanel } from './SessionLog.js';

// ── Attempt grouping ────────────────────────────────────────
const ATTEMPT_BOUNDARIES = [
    'Task completed', 'Task failed', 'Dispatch error', 'Turns exhausted',
    'Session killed by signal', 'Rate limited', 'Wall clock timeout',
    'Recovery limit reached',
];

function groupMessagesByAttempt(messages) {
    if (!messages || messages.length === 0) return [];
    const attempts = [];
    let current = { messages: [], outcome: null };

    for (const msg of messages) {
        if (msg.type === 'plan') continue;
        current.messages.push(msg);
        if (msg.author === 'dispatcher' && msg.type === 'status'
            && ATTEMPT_BOUNDARIES.some(b => (msg.title || '').includes(b))) {
            current.outcome = msg.title || 'Completed';
            attempts.push(current);
            current = { messages: [], outcome: null };
        }
    }
    if (current.messages.length > 0) {
        if (current.messages.every(m => m.author === 'dispatcher')) {
            if (attempts.length > 0) {
                attempts[attempts.length - 1].messages.push(...current.messages);
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

// ── Per-Attempt Session Log ──────────────────────────────────
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
        <div class="mt-2 border-t border-slate-700/50 pt-2">
            <button onClick=${() => setExpanded(!expanded)}
                class="text-xs flex items-center gap-1 text-slate-500 hover:text-slate-300 cursor-pointer">
                ${expanded ? '\u25BE' : '\u25B8'} Session Log
            </button>
            ${expanded ? html`
                <div class="mt-1">
                    <button onClick=${() => setShowTools(!showTools)}
                        class="text-xs px-2 py-0.5 rounded mb-1 bg-slate-800 text-slate-400 hover:bg-slate-700">
                        ${showTools ? 'Text only' : 'Show tools'}
                    </button>
                    <pre ref=${logRef} class="text-xs overflow-y-auto whitespace-pre-wrap rounded p-2 bg-slate-950"
                        style="max-height: 400px; color: var(--text-muted)">
                        ${!loaded ? 'Loading...' : entries.length === 0 ? 'No session log' :
                            entries.map(e => {
                                if (e.type === 'AssistantMessage') {
                                    return (e.content || []).map(b => {
                                        if (b.type === 'text') return b.text + '\n';
                                        if (b.type === 'tool_use' && showTools) return '[TOOL] ' + b.name + ': ' + JSON.stringify(b.input).slice(0, 200) + '\n';
                                        return '';
                                    }).join('');
                                }
                                if (e.type === 'UserMessage' && showTools) {
                                    return (e.content || []).map(b => {
                                        if (b.type === 'tool_result') return '[RESULT] ' + (b.preview || '').slice(0, 200) + '\n';
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

// ── Review Verdict Badge ─────────────────────────────────────
function ReviewVerdictBadge({ subtasks }) {
    const reviews = (subtasks || []).filter(s => s.type === 'review');
    if (reviews.length === 0) return null;
    const latest = reviews[reviews.length - 1];

    let cls, label, icon;
    if (latest.status === 'completed') {
        const result = (latest.result || '').toLowerCase();
        if (result.includes('changes requested') || result.includes('changes_requested')) {
            cls = 'verdict-changes'; label = 'CHANGES REQUESTED'; icon = '\u2715';
        } else {
            cls = 'verdict-approved'; label = 'APPROVED'; icon = '\u2713';
        }
    } else if (latest.status === 'failed') {
        cls = 'verdict-changes'; label = 'REVIEW FAILED'; icon = '\u2715';
    } else if (latest.status === 'working') {
        cls = 'verdict-pending'; label = 'REVIEWING...'; icon = '\uD83D\uDC41';
    } else {
        return null;
    }

    return html`<${Tip} text="Latest verdict from automated code review">
        <span class="verdict-badge ${cls}">${icon} ${label}</span>
    <//>`;
}

function DetailHeader({ task, onAction, jiraBaseUrl }) {
    return html`
        <div class="bg-slate-900 border border-slate-800 rounded-lg p-4 mb-4">
            <div class="flex items-start justify-between">
                <div class="flex-1">
                    <div class="flex items-center gap-3 mb-2 flex-wrap">
                        <${StatusBadge} status=${task.status} task=${task} />
                        <${ReviewVerdictBadge} subtasks=${task.subtasks} />
                        <${HeartbeatIndicator} task=${task} />
                        <span class="font-mono text-lg text-slate-200">${task.id}</span>
                    </div>
                    <p class="text-slate-300 mb-3">${task.goal}</p>
                    <div class="flex flex-wrap gap-x-6 gap-y-1 text-sm text-slate-400">
                        <span>Branch: <span class="font-mono text-slate-300">${task.branch || '\u2014'}</span></span>
                        <span>Dispatches: <span class="text-slate-300">${task.dispatch_count || 0}</span></span>
                        <${Tip} text="Total API cost across all dispatches">
                            <span>Cost: <span class="text-slate-300">$${(task.total_cost_usd || 0).toFixed(2)}</span></span>
                        <//>
                        <${Tip} text="Input/output token count across all dispatches">
                            <span>Tokens: <span class="text-slate-300">${((task.total_input_tokens || 0) / 1000).toFixed(0)}K in / ${((task.total_output_tokens || 0) / 1000).toFixed(1)}K out</span></span>
                        <//>
                        ${task.model ? html`<span>Model: <span class="text-slate-300">${task.model}</span></span>` : null}
                        ${task.phase ? html`<span>Phase: <span class="text-slate-300">${task.phase}</span></span>` : null}
                        <${WorktreeIndicator} task=${task} />
                        <${PrUrlBadge} task=${task} />
                        ${task.jira_ticket ? html`<a href=${jiraUrl(task.jira_ticket, jiraBaseUrl)} target="_blank" rel="noopener"
                            class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-cyan-500/20 text-cyan-400 hover:bg-cyan-500/30">${jiraLabel(task.jira_ticket)}</a>` : null}
                        ${task.conversation_id ? html`<a href="#/conversations/${encodeURIComponent(task.conversation_id)}"
                            class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-indigo-500/20 text-indigo-400 hover:bg-indigo-500/30">Conv: ${task.conversation_id}</a>` : null}
                        <${ClaudeChatLink} url=${task.claude_chat_url} />
                    </div>
                    ${(task.tags || []).length > 0 ? html`<div class="flex gap-1 mt-2">${task.tags.map(t => html`<span class="px-2 py-0.5 rounded text-xs bg-slate-700 text-slate-300">${t}</span>`)}</div>` : null}
                </div>
                <div class="flex gap-2 ml-4"><${ActionButtons} task=${task} onAction=${onAction} /></div>
            </div>
        </div>
    `;
}

function GatePipeline({ task }) {
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
        const retries = task.gate_retries > 0 ? ` (attempt ${task.gate_retries + 1}/${task.max_gate_retries || 3})` : '';
        stages.push({ label: 'Tests', status: s, tip: `Automated tests${retries}` });
    }

    if (task.auto_review) {
        const gs = task.gate_status;
        let s = 'pending';
        if (gs === 'reviewing') s = 'active';
        else if (gs === 'review-failed') s = 'failed';
        else if (gs === 'passed') s = 'done';
        stages.push({ label: 'Review', status: s, tip: 'Automated code review' });
    }

    stages.push({ label: 'Advance', status: task.gate_status === 'passed' ? 'done' : 'pending', tip: 'Ready to advance to next task in chain' });

    const stageColors = {
        done: 'bg-emerald-500 text-white',
        active: 'bg-blue-500 text-white gate-pulse',
        failed: 'bg-red-500 text-white',
        pending: 'bg-slate-700 text-slate-400',
    };
    const stageIcons = { done: '\u2713', active: '\u25CF', failed: '\u2715', pending: '\u25CB' };

    return html`
        <div class="bg-slate-900 border border-slate-800 rounded-lg p-4 mb-4">
            <div class="flex items-center gap-2 overflow-x-auto">
                ${stages.map((st, i) => html`
                    <div key=${i} class="flex items-center gap-2 shrink-0">
                        <${Tip} text=${st.tip || st.label}>
                            <div class="flex flex-col items-center">
                                <div class="w-8 h-8 rounded-full flex items-center justify-center text-sm ${stageColors[st.status]}">
                                    ${stageIcons[st.status]}
                                </div>
                                <span class="text-xs text-slate-400 mt-1">${st.label}</span>
                            </div>
                        <//>
                        ${i < stages.length - 1 ? html`<div class="w-8 h-px bg-slate-600"></div>` : null}
                    </div>
                `)}
            </div>
            ${task.gate_retries > 0 ? html`<span class="text-xs text-slate-400 mt-1">Retries: ${task.gate_retries}/${task.max_gate_retries || 3}</span>` : null}
        </div>
    `;
}

function ChainStrip({ taskId }) {
    const [chain, setChain] = useState(null);
    const [currentIdx, setCurrentIdx] = useState(-1);

    useEffect(() => {
        api.getChain(taskId)
            .then(data => {
                if (data && data.chain && data.chain.length > 1) {
                    const filtered = data.chain.filter(t => !t.parent_task_id);
                    setChain(filtered);
                    const idx = filtered.findIndex(t => t.id === taskId);
                    setCurrentIdx(idx >= 0 ? idx : data.current_index);
                } else {
                    setChain(null);
                }
            })
            .catch(() => setChain(null));
    }, [taskId]);

    if (!chain || chain.length <= 1) return null;

    const dotColor = (status) => {
        if (status === 'completed') return '#22c55e';
        if (status === 'working') return '#f59e0b';
        if (status === 'failed') return '#ef4444';
        if (status === 'needs-review') return '#f59e0b';
        return '#64748b';
    };

    // Truncate to max 7 nodes, showing current in middle
    let displayChain = chain;
    let truncatedStart = false, truncatedEnd = false;
    if (chain.length > 7) {
        const start = Math.max(0, Math.min(currentIdx - 3, chain.length - 7));
        const end = start + 7;
        displayChain = chain.slice(start, end);
        truncatedStart = start > 0;
        truncatedEnd = end < chain.length;
    }

    return html`
        <div class="bg-slate-900 border border-slate-800 rounded-lg p-4 mb-4">
            <div class="flex items-center gap-1 overflow-x-auto">
                ${truncatedStart ? html`<span class="text-xs text-slate-500 shrink-0 px-1">\u2026</span>` : null}
                ${displayChain.map((t, i) => {
                    const isCurrent = t.id === taskId;
                    const shortName = (t.goal || t.id.split('/').pop() || '').slice(0, 20);
                    const color = dotColor(t.status);
                    return html`
                        <div key=${t.id} class="flex items-center gap-1 shrink-0">
                            <a href="#/tasks/${t.id}"
                                class="flex items-center gap-1.5 px-2 py-1 rounded text-xs hover:bg-slate-800/50"
                                style="cursor: pointer; ${isCurrent ? 'background: rgba(59, 130, 246, 0.15); border: 1px solid rgba(59, 130, 246, 0.3);' : ''}">
                                <span class="w-2 h-2 rounded-full shrink-0 ${t.status === 'working' ? 'status-dot-working' : ''}"
                                    style="background: ${color}"></span>
                                <span class="truncate" style="max-width: 120px; color: ${isCurrent ? '#60a5fa' : 'var(--text-muted)'}">${shortName}</span>
                            </a>
                            ${i < displayChain.length - 1 ? html`<span class="text-slate-600 shrink-0">\u2500</span>` : null}
                        </div>
                    `;
                })}
                ${truncatedEnd ? html`<span class="text-xs text-slate-500 shrink-0 px-1">\u2026</span>` : null}
            </div>
        </div>
    `;
}

function Subtasks({ subtasks }) {
    if (!subtasks || subtasks.length === 0) return null;

    const typeColors = {
        review:  { bg: 'bg-pink-500/20', text: 'text-pink-400', icon: '\uD83D\uDC41' },
        test:    { bg: 'bg-violet-500/20', text: 'text-violet-400', icon: '\u2699' },
        fix:     { bg: 'bg-amber-500/20', text: 'text-amber-400', icon: '\uD83D\uDD27' },
        default: { bg: 'bg-slate-500/20', text: 'text-slate-400', icon: '\u25CF' },
    };

    return html`
        <div class="bg-slate-900 border border-slate-800 rounded-lg p-4 mb-4">
            <h3 class="text-sm font-medium text-slate-300 mb-3">Subtasks</h3>
            <div class="flex flex-col gap-2">
                ${subtasks.map(sub => {
                    const tc = typeColors[sub.type] || typeColors.default;
                    const isWorking = sub.status === 'working';
                    const isFailed = sub.status === 'failed';
                    const isDone = sub.status === 'completed';

                    let statusIcon;
                    if (isWorking) statusIcon = html`<span class="status-dot-working text-blue-400">\u25CF</span>`;
                    else if (isDone) statusIcon = html`<span class="text-emerald-400">\u2713</span>`;
                    else if (isFailed) statusIcon = html`<span class="text-red-400">\u2715</span>`;
                    else statusIcon = html`<span class="text-slate-500">\u25CB</span>`;

                    let duration = '';
                    if (sub.duration_ms) {
                        const secs = Math.round(sub.duration_ms / 1000);
                        duration = secs >= 60 ? `${Math.floor(secs / 60)}m ${secs % 60}s` : `${secs}s`;
                    } else if (isWorking && sub.created_at) {
                        const elapsed = Math.floor((Date.now() - new Date(sub.created_at + (sub.created_at.endsWith('Z') ? '' : 'Z')).getTime()) / 1000);
                        duration = elapsed >= 60 ? `${Math.floor(elapsed / 60)}m ${elapsed % 60}s` : `${elapsed}s`;
                    }

                    const cost = sub.cost_usd ? `$${sub.cost_usd.toFixed(4)}` : '';
                    const shortId = sub.id.split('/').pop();

                    return html`
                        <details key=${sub.id} open=${isWorking} class="border border-slate-700/50 rounded-lg overflow-hidden ${isWorking ? 'ring-1 ring-blue-500/30' : ''}">
                            <summary class="px-4 py-2.5 text-sm cursor-pointer hover:bg-slate-800/50 flex items-center gap-2">
                                ${statusIcon}
                                <span class="px-2 py-0.5 rounded text-xs font-medium ${tc.bg} ${tc.text}">${tc.icon} ${sub.type.toUpperCase()}</span>
                                <span class="text-slate-400 text-xs font-mono">${shortId}</span>
                                <span class="text-slate-500 text-xs">${sub.model || ''}</span>
                                ${isWorking ? html`<span class="text-blue-400 text-xs status-dot-working">running...</span>` : null}
                                <span class="ml-auto flex items-center gap-3 text-xs text-slate-500">
                                    ${duration ? html`<span>${duration}</span>` : null}
                                    ${cost ? html`<span>${cost}</span>` : null}
                                    ${sub.completed_at ? html`<span>${relativeTime(sub.completed_at)}</span>` : null}
                                </span>
                            </summary>
                            ${sub.result && sub.result.trim() ? html`
                                <div class="px-4 pb-3 border-t border-slate-700/50 mt-2">
                                    <div class="prose-dark text-sm"
                                        dangerouslySetInnerHTML=${{ __html: renderMarkdown(sub.result) }}>
                                    </div>
                                </div>
                            ` : null}
                        </details>
                    `;
                })}
            </div>
        </div>
    `;
}

function Checklist({ task }) {
    const items = task.checklist || [];
    if (items.length === 0) {
        return html`<div class="bg-slate-900 border border-slate-800 rounded-lg p-4 mb-4">
            <p class="text-slate-500 text-sm">No checklist items</p>
        </div>`;
    }

    return html`
        <div class="bg-slate-900 border border-slate-800 rounded-lg p-4 mb-4">
            <h3 class="text-sm font-medium text-slate-300 mb-2">Checklist (${task.checklist_done}/${task.checklist_total})</h3>
            <div class="space-y-1">
                ${items.map(c => html`
                    <div key=${c.id} class="flex items-center gap-2 text-sm ${c.done ? 'text-slate-400' : 'text-slate-200'}">
                        <span>${c.done ? '\u2705' : '\u2B1C'}</span>
                        <span>${c.item}</span>
                    </div>
                `)}
            </div>
        </div>
    `;
}

function PlanSection({ messages }) {
    const planMsg = [...(messages || [])].reverse().find(m => m.type === 'plan');
    if (!planMsg) return null;

    const contentHtml = renderMarkdown(planMsg.content);
    const time = planMsg.created_at ? new Date(planMsg.created_at + (planMsg.created_at.endsWith('Z') ? '' : 'Z')).toLocaleTimeString() : '';

    return html`
        <details class="bg-slate-900 border border-slate-800 rounded-lg mb-4" open>
            <summary class="px-4 py-3 text-sm font-medium text-slate-300 cursor-pointer hover:text-slate-100">
                Implementation Plan <span class="text-xs text-slate-500 ml-2">${time}</span>
            </summary>
            <div class="px-4 pb-3 prose-dark text-sm border-t border-slate-700/50"
                dangerouslySetInnerHTML=${{ __html: contentHtml }}>
            </div>
        </details>
    `;
}

function MessageInput({ taskId, task, onAction, onMessageSent }) {
    const [msgContent, setMsgContent] = useState('');
    const [msgType, setMsgType] = useState('review');

    const handleSubmit = async (e) => {
        e.preventDefault();
        const content = msgContent.trim();
        if (!content) return;
        try {
            await api.postMessage(taskId, content, msgType);
            setMsgContent('');
            onMessageSent();
        } catch (err) {
            alert(`Error posting message: ${err.message}`);
        }
    };

    const resumable = ['completed', 'needs-review', 'turns-exhausted'].includes(task?.status);

    return html`
        <form onSubmit=${handleSubmit} class="mt-4 border-t border-slate-700 pt-3">
            <div class="flex gap-2">
                <select class="bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-sm text-slate-300 w-28"
                    value=${msgType} onChange=${(e) => setMsgType(e.target.value)}>
                    <option value="review">Review</option>
                    <option value="note">Note</option>
                    <option value="answer">Answer</option>
                </select>
                <input type="text" placeholder="Post a message..."
                    class="flex-1 bg-slate-800 border border-slate-700 rounded px-3 py-1.5 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-slate-500"
                    value=${msgContent} onInput=${(e) => setMsgContent(e.target.value)} />
                <button type="submit" class="px-4 py-1.5 text-sm rounded bg-blue-600 text-white hover:bg-blue-500">Send</button>
            </div>
            ${resumable ? html`
                <div class="mt-2">
                    <${Tip} text="Continue the existing CC session with full conversation history">
                        <button type="button" onClick=${() => onAction('resume', taskId)}
                            class="w-full px-3 py-2 text-sm rounded bg-emerald-600/20 border border-emerald-500/30 text-emerald-400 hover:bg-emerald-600/30">
                            Resume session with new messages
                        </button>
                    <//>
                </div>
            ` : null}
        </form>
    `;
}

export function TaskDetail({ taskId, jiraBaseUrl, onAction }) {
    const [task, setTask] = useState(null);
    const [error, setError] = useState(null);
    const [dispatchLogOpen, setDispatchLogOpen] = useState(false);
    const mountedRef = useRef(true);

    const loadTask = useCallback(async () => {
        try {
            const data = await api.getTask(taskId);
            if (mountedRef.current) { setTask(data); setError(null); }
        } catch (e) {
            if (mountedRef.current) {
                if (!task) setError(e.message);
                else console.warn('Poll error:', e.message);
            }
        }
    }, [taskId]);

    useEffect(() => {
        mountedRef.current = true;
        setTask(null);
        setError(null);
        setDispatchLogOpen(false);
        loadTask();
        return () => { mountedRef.current = false; };
    }, [taskId]);

    useEffect(() => {
        if (!task) return;
        const gateActive = ['testing', 'test-passed', 'reviewing'].includes(task.gate_status);
        const shouldPoll = task.status === 'working' || task.status === 'needs-review' || task.status === 'turns-exhausted' || gateActive;
        if (!shouldPoll) return;

        const timer = setInterval(loadTask, 5000);
        return () => clearInterval(timer);
    }, [task?.status, task?.gate_status, loadTask]);

    if (error) {
        return html`<div class="p-6">
            <div class="mb-4"><a href="#/" class="text-sm text-slate-400 hover:text-slate-200">\u2190 Board</a></div>
            <${ErrorState} message="Error loading task: ${error}" onRetry=${loadTask} />
        </div>`;
    }

    if (!task) {
        return html`<div class="p-6">
            <div class="mb-4"><a href="#/" class="text-sm text-slate-400 hover:text-slate-200">\u2190 Board</a></div>
            <${LoadingState} message="Loading task..." />
        </div>`;
    }

    return html`
        <div class="p-6">
            <div class="mb-4">
                <a href="#/" class="text-sm text-slate-400 hover:text-slate-200">\u2190 Board</a>
            </div>

            <${DetailHeader} task=${task} onAction=${onAction} jiraBaseUrl=${jiraBaseUrl} />
            <${GatePipeline} task=${task} />
            <${ChainStrip} taskId=${taskId} />
            <${Subtasks} subtasks=${task.subtasks} />
            <${Checklist} task=${task} />
            <${PlanSection} messages=${task.messages} />

            ${(() => {
                const attempts = groupMessagesByAttempt(task.messages);
                const nonPlanCount = (task.messages || []).filter(m => m.type !== 'plan').length;
                if (attempts.length <= 1) {
                    return html`
                        <div class="bg-slate-900 border border-slate-800 rounded-lg p-4 mb-4">
                            <h3 class="text-sm font-medium text-slate-300 mb-3">Messages (${nonPlanCount})</h3>
                            <${MessageThread} messages=${task.messages} filterPlan=${true} idPrefix="msg" />
                            ${attempts.length === 1 ? html`
                                <${AttemptSessionLog} taskId=${taskId} attemptNumber=${1} autoRefresh=${task.status === 'working'} />
                            ` : null}
                            <${MessageInput} taskId=${taskId} task=${task} onAction=${onAction} onMessageSent=${loadTask} />
                        </div>`;
                }
                return html`
                    <div class="bg-slate-900 border border-slate-800 rounded-lg p-4 mb-4">
                        <div class="text-sm font-medium text-slate-300 mb-3">
                            Messages (${nonPlanCount}) \u00B7 ${attempts.length} attempts
                        </div>
                        ${attempts.map((attempt, idx) => {
                            const isLast = idx === attempts.length - 1;
                            const outcomeColor = attempt.outcome === 'In Progress' ? 'text-blue-400' :
                                attempt.outcome?.toLowerCase().includes('fail') || attempt.outcome?.toLowerCase().includes('error') ? 'text-red-400' :
                                'text-emerald-400';
                            return html`
                                <details key=${idx} class="border border-slate-700 rounded mb-2" open=${isLast}>
                                    <summary class="px-3 py-2 text-xs cursor-pointer hover:bg-slate-800/50 flex items-center gap-2">
                                        <span class="text-slate-300 font-medium">Attempt ${idx + 1}</span>
                                        <span class="${outcomeColor}">${attempt.outcome}</span>
                                        ${isLast && task.status === 'working' ? html`<span class="text-amber-400 status-dot-working">\u25CF Claude is Coding</span>` : null}
                                        <span class="text-slate-500 ml-auto">${attempt.messages.length} msgs</span>
                                    </summary>
                                    <div class="px-2 pb-2">
                                        <${MessageThread} messages=${attempt.messages} idPrefix=${'attempt-' + idx} />
                                        <${AttemptSessionLog} taskId=${taskId} attemptNumber=${idx + 1}
                                            autoRefresh=${isLast && task.status === 'working'} />
                                    </div>
                                </details>`;
                        })}
                        <${MessageInput} taskId=${taskId} task=${task} onAction=${onAction} onMessageSent=${loadTask} />
                    </div>`;
            })()}

            <${DispatchLogPanel} taskId=${taskId} isOpen=${dispatchLogOpen}
                onToggle=${() => setDispatchLogOpen(!dispatchLogOpen)} />
        </div>
    `;
}
