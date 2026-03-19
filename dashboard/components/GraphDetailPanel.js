// Graph Detail Panel — slide-in task detail for DAG graph view
import { useState, useEffect, useRef, useCallback } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { html, relativeTime, renderMarkdown, StatusBadge, GateBadge, PrUrlBadge, ActionButtons, Tip, WorktreeIndicator, HeartbeatIndicator, ClaudeChatLink, LoadingState, ErrorState, jiraUrl, jiraLabel, BUTTON_TOOLTIPS } from './utils.js';
import { MessageThread } from './MessageThread.js';
import { SessionLogPanel, DispatchLogPanel } from './SessionLog.js';
import { GitFlowSummary } from './GitFlowSummary.js';

// ── Chain Visualization ─────────────────────────────────────
function ChainVisualization({ taskId, onSelectTask }) {
    const [chain, setChain] = useState(null);
    const [currentIdx, setCurrentIdx] = useState(-1);

    useEffect(() => {
        api.getChain(taskId)
            .then(data => {
                if (data && data.chain && data.chain.length > 1) {
                    setChain(data.chain);
                    setCurrentIdx(data.current_index);
                } else {
                    setChain(null);
                }
            })
            .catch(() => setChain(null));
    }, [taskId]);

    if (!chain) return null;

    return html`
        <div class="bg-slate-900 border border-slate-800 rounded-lg p-3 mb-3">
            <h3 class="text-xs font-medium text-slate-400 mb-2">\u26D3 Task Chain</h3>
            <div class="flex items-center gap-1.5 overflow-x-auto pb-1">
                ${chain.map((t, i) => {
                    if (t.parent_task_id) return null;
                    const isCurrent = i === currentIdx;
                    const border = isCurrent ? 'border-blue-500 ring-1 ring-blue-500/50' : 'border-slate-700';
                    const shortId = t.id.split('/').pop();
                    return html`
                        <a key=${t.id}
                            href="#/tasks/${t.id}"
                            onClick=${(e) => { if (onSelectTask) { e.preventDefault(); onSelectTask(t.id); } }}
                            class="shrink-0 block p-1.5 rounded border ${border} bg-slate-800/50 hover:bg-slate-800 min-w-[100px] max-w-[150px] cursor-pointer">
                            <div class="flex items-center gap-1 mb-0.5">
                                <${StatusBadge} status=${t.status} task=${t} />
                            </div>
                            <div class="text-xs font-mono text-slate-300 truncate">${shortId}</div>
                            <div class="text-[10px] text-slate-500 truncate">${(t.goal || '').slice(0, 35)}</div>
                        </a>
                        ${i < chain.length - 1 && !chain[i + 1]?.parent_task_id ? html`<span class="text-slate-600 shrink-0 text-xs">\u2192</span>` : null}
                    `;
                })}
            </div>
        </div>
    `;
}

// ── Attempt grouping for messages ────────────────────────────
function groupMessagesByAttempt(messages) {
    if (!messages || messages.length === 0) return [];
    const attempts = [];
    let current = { messages: [], outcome: null, startTime: null, endTime: null };

    // These dispatcher status messages end an attempt — everything else stays in the current group
    const ATTEMPT_BOUNDARIES = [
        'Task completed', 'Task failed', 'Dispatch error', 'Turns exhausted',
        'Session killed by signal', 'Rate limited', 'Wall clock timeout',
        'Recovery limit reached',
    ];

    for (const msg of messages) {
        if (msg.type === 'plan') continue;
        if (!current.startTime) current.startTime = msg.created_at;
        current.endTime = msg.created_at;
        current.messages.push(msg);
        if (msg.author === 'dispatcher' && msg.type === 'status'
            && ATTEMPT_BOUNDARIES.some(b => (msg.title || '').includes(b))) {
            current.outcome = msg.title || 'Completed';
            attempts.push(current);
            current = { messages: [], outcome: null, startTime: null, endTime: null };
        }
    }
    if (current.messages.length > 0) {
        // Remaining messages after last boundary — could be post-gate events or in-progress
        if (current.messages.every(m => m.author === 'dispatcher')) {
            // All dispatcher messages (Auto-merged, Tests passed, etc.) — append to last attempt
            if (attempts.length > 0) {
                const last = attempts[attempts.length - 1];
                last.messages.push(...current.messages);
                last.endTime = current.endTime;
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

// Action buttons — use shared ActionButtons from utils.js

// ── Gate Pipeline (inline) ───────────────────────────────────
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

    stages.push({ label: 'Advance', status: task.gate_status === 'passed' ? 'done' : 'pending' });

    const colors = {
        done: 'bg-emerald-500 text-white',
        active: 'bg-blue-500 text-white gate-pulse',
        failed: 'bg-red-500 text-white',
        pending: 'bg-slate-700 text-slate-400',
    };
    const icons = { done: '\u2713', active: '\u25CF', failed: '\u2715', pending: '\u25CB' };

    return html`
        <div class="flex items-center gap-2 flex-wrap mb-3">
            ${stages.map((st, i) => html`
                <div key=${i} class="flex items-center gap-2 shrink-0">
                    <${Tip} text=${st.tip || st.label}>
                        <div class="flex flex-col items-center">
                            <div class="w-7 h-7 rounded-full flex items-center justify-center text-xs ${colors[st.status]}">
                                ${icons[st.status]}
                            </div>
                            <span class="text-xs text-slate-500 mt-0.5">${st.label}</span>
                        </div>
                    <//>
                    ${i < stages.length - 1 ? html`<div class="w-6 h-px bg-slate-600"></div>` : null}
                </div>
            `)}
            ${task.gate_retries > 0 ? html`<span class="text-xs text-slate-500 ml-1">Retries: ${task.gate_retries}/${task.max_gate_retries || 3}</span>` : null}
        </div>
    `;
}

// ── Blockers section ────────────────────────────────────────
function BlockersSection({ task, allTasks }) {
    if (!task.depends_on) return null;
    const parent = allTasks && allTasks.find(t => t.id === task.depends_on);
    if (!parent) return null;
    if (['completed', 'merged'].includes(parent.status) && (!parent.gate_status || parent.gate_status === 'passed')) return null;

    return html`
        <div class="bg-slate-800/50 border border-slate-700 rounded p-3 mb-3">
            <h4 class="text-xs font-medium text-amber-400 mb-2">Blocked By</h4>
            <div class="flex items-center gap-2">
                <${StatusBadge} status=${parent.status} />
                <span class="text-sm font-mono text-slate-300">${parent.id.split('/').pop()}</span>
                ${parent.gate_status && parent.gate_status !== 'passed' ? html`<${GateBadge} task=${parent} />` : null}
            </div>
            <p class="text-xs text-slate-500 mt-1">${parent.goal}</p>
        </div>
    `;
}

// ── Held task notice ─────────────────────────────────────────
function HeldSection({ task, allTasks }) {
    if (!task.held) return null;
    const parent = allTasks && task.depends_on && allTasks.find(t => t.id === task.depends_on);
    const isAlsoBlocked = parent && (
        !['completed', 'merged'].includes(parent.status) ||
        (parent.gate_status && parent.gate_status !== 'passed')
    );
    const parentShortId = parent ? parent.id.split('/').pop() : '';
    return html`
        <div class="bg-yellow-900/20 border border-yellow-600/30 rounded p-3 mb-3">
            <div class="flex items-center gap-2 mb-1">
                <span class="text-yellow-400 font-medium text-sm">\uD83D\uDD12 HELD</span>
                ${isAlsoBlocked ? html`<span class="text-slate-400 text-xs">\u2014 waiting on <span class="font-mono text-slate-300">${parentShortId}</span></span>` : null}
            </div>
            <p class="text-xs text-slate-400">This task is held. It won't auto-dispatch when dependencies complete. Approve to release it.</p>
        </div>
    `;
}

// ── Review output section ───────────────────────────────────
function ReviewSection({ subtasks }) {
    const reviews = (subtasks || []).filter(s => s.type === 'review');
    if (reviews.length === 0) return null;
    const latest = reviews[reviews.length - 1];

    let verdictCls, verdictLabel;
    if (latest.status === 'completed') {
        const result = (latest.result || '').toLowerCase();
        if (result.includes('changes requested') || result.includes('changes_requested')) {
            verdictCls = 'verdict-changes'; verdictLabel = 'CHANGES REQUESTED';
        } else {
            verdictCls = 'verdict-approved'; verdictLabel = 'APPROVED';
        }
    } else if (latest.status === 'failed') {
        verdictCls = 'verdict-changes'; verdictLabel = 'REVIEW FAILED';
    } else if (latest.status === 'working') {
        verdictCls = 'verdict-pending'; verdictLabel = 'REVIEWING...';
    } else {
        verdictCls = 'bg-slate-500/20 text-slate-400'; verdictLabel = latest.status.toUpperCase();
    }

    return html`
        <details class="bg-slate-800/50 border border-slate-700 rounded mb-3" open=${latest.status === 'working'}>
            <summary class="px-3 py-2 text-sm cursor-pointer hover:bg-slate-800">
                <span class="inline-flex items-center gap-2">
                    Review Output
                    <span class="verdict-badge ${verdictCls}">${verdictLabel}</span>
                    ${latest.model ? html`<span class="text-xs text-slate-500">${latest.model}</span>` : null}
                    <a href="#/tasks/${latest.id}" class="text-xs text-blue-400 hover:text-blue-300 ml-1" title="View reviewer session log">Session Log \u2197</a>
                </span>
            </summary>
            ${latest.result ? html`
                <div class="px-3 pb-3 border-t border-slate-700/50 prose-dark text-sm"
                    dangerouslySetInnerHTML=${{ __html: renderMarkdown(latest.result) }}></div>
            ` : null}
        </details>
    `;
}

// ── Test output section ─────────────────────────────────────
function TestSection({ subtasks }) {
    const tests = (subtasks || []).filter(s => s.type === 'test');
    if (tests.length === 0) return null;
    const latest = tests[tests.length - 1];

    const exitColor = latest.status === 'completed' ? 'bg-emerald-500/20 text-emerald-400' :
                      latest.status === 'failed' ? 'bg-red-500/20 text-red-400' :
                      latest.status === 'working' ? 'bg-blue-500/20 text-blue-400' :
                      'bg-slate-500/20 text-slate-400';

    return html`
        <details class="bg-slate-800/50 border border-slate-700 rounded mb-3" open=${latest.status === 'failed'}>
            <summary class="px-3 py-2 text-sm cursor-pointer hover:bg-slate-800">
                <span class="inline-flex items-center gap-2">
                    Test Output
                    <span class="px-2 py-0.5 rounded text-xs font-medium ${exitColor}">
                        ${latest.status === 'completed' ? 'PASSED' : latest.status === 'failed' ? 'FAILED' : latest.status.toUpperCase()}
                    </span>
                    <span class="text-xs text-slate-500">Attempt ${tests.length}</span>
                </span>
            </summary>
            ${latest.result ? html`
                <div class="px-3 pb-3 border-t border-slate-700/50">
                    <pre class="text-xs text-slate-400 whitespace-pre-wrap">${latest.result}</pre>
                </div>
            ` : null}
        </details>
    `;
}

// ── Checklist ───────────────────────────────────────────────
function Checklist({ task }) {
    const items = task.checklist || [];
    if (items.length === 0) return null;

    return html`
        <div class="mb-3">
            <h4 class="text-xs font-medium text-slate-400 mb-1">Checklist (${task.checklist_done}/${task.checklist_total})</h4>
            <div class="space-y-0.5">
                ${items.map(c => html`
                    <div key=${c.id} class="flex items-center gap-2 text-xs ${c.done ? 'text-slate-500' : 'text-slate-300'}">
                        <span>${c.done ? '\u2705' : '\u2B1C'}</span>
                        <span>${c.item}</span>
                    </div>
                `)}
            </div>
        </div>
    `;
}


// ── Spec section ─────────────────────────────────────────────
function SpecSection({ messages }) {
    const specMsg = (messages || []).find(m => m.type === 'spec');
    if (!specMsg) return null;
    return html`
        <details class="bg-blue-950/30 border border-blue-800/50 rounded mb-3" open>
            <summary class="px-3 py-2 text-sm cursor-pointer hover:bg-blue-950/50">
                <span class="inline-flex items-center gap-2">
                    <span class="text-blue-400 font-medium">Spec</span>
                    ${specMsg.title ? html`<span class="text-slate-500 text-xs">${specMsg.title}</span>` : null}
                </span>
            </summary>
            <div class="px-3 pb-3 prose-dark text-sm border-t border-blue-800/30"
                dangerouslySetInnerHTML=${{ __html: renderMarkdown(specMsg.content) }}></div>
        </details>
    `;
}

// ── Plan section ────────────────────────────────────────────
function PlanSection({ messages }) {
    const planMsg = [...(messages || [])].reverse().find(m => m.type === 'plan');
    if (!planMsg) return null;
    return html`
        <details class="bg-slate-800/50 border border-slate-700 rounded mb-3">
            <summary class="px-3 py-2 text-sm text-slate-300 cursor-pointer hover:bg-slate-800">
                Implementation Plan
            </summary>
            <div class="px-3 pb-3 prose-dark text-sm border-t border-slate-700/50"
                dangerouslySetInnerHTML=${{ __html: renderMarkdown(planMsg.content) }}></div>
        </details>
    `;
}

// ── Message input ───────────────────────────────────────────
function MessageInput({ taskId, task, onAction, onRefresh }) {
    const [content, setContent] = useState('');
    const [type, setType] = useState('review');

    const submit = async (e) => {
        e.preventDefault();
        if (!content.trim()) return;
        try {
            await api.postMessage(taskId, content.trim(), type);
            setContent('');
            onRefresh();
        } catch (err) { alert(`Error: ${err.message}`); }
    };

    const resumable = ['completed', 'needs-review', 'turns-exhausted'].includes(task?.status);

    return html`
        <form onSubmit=${submit} class="mt-3 border-t border-slate-700 pt-2">
            <div class="flex gap-1">
                <select class="bg-slate-800 border border-slate-700 rounded px-1.5 py-1 text-xs text-slate-300 w-20"
                    value=${type} onChange=${(e) => setType(e.target.value)}>
                    <option value="review">Review</option>
                    <option value="note">Note</option>
                    <option value="answer">Answer</option>
                </select>
                <input type="text" placeholder="Post a message..."
                    class="flex-1 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-slate-500"
                    value=${content} onInput=${(e) => setContent(e.target.value)} />
                <${Tip} text="Send message to task thread">
                    <button type="submit" class="px-3 py-1 text-xs rounded bg-blue-600 text-white hover:bg-blue-500">Send</button>
                <//>
            </div>
            ${resumable ? html`
                <${Tip} text="Continue the existing CC session with full conversation history">
                    <button type="button" onClick=${() => onAction('resume', taskId)}
                        class="w-full mt-1.5 px-2 py-1.5 text-xs rounded bg-emerald-600/20 border border-emerald-500/30 text-emerald-400 hover:bg-emerald-600/30">
                        Resume session with new messages
                    </button>
                <//>
            ` : null}
        </form>
    `;
}

// ── Proof of Life ───────────────────────────────────────────
function ProofOfLife({ task }) {
    if (task.status !== 'working') return null;
    const alive = task.alive;
    const pid = task.pid;
    const la = task.last_activity;
    const age = la ? (Date.now() - new Date(la + (la.endsWith('Z') ? '' : 'Z')).getTime()) / 1000 : null;

    let indicator, label;
    if (age === null) { indicator = 'bg-slate-500'; label = 'No activity data'; }
    else if (age > 300) { indicator = 'bg-red-500'; label = `Stale — last activity ${Math.floor(age / 60)}m ago`; }
    else if (age > 120) { indicator = 'bg-amber-500'; label = `Idle — ${Math.floor(age)}s ago`; }
    else { indicator = 'bg-emerald-500 status-dot-working'; label = `Active — ${Math.floor(age)}s ago`; }

    return html`
        <div class="flex items-center gap-2 text-xs text-slate-400 mb-2">
            <${Tip} text=${age !== null ? `Last activity ${Math.floor(age)}s ago` : 'No activity data'}>
                <span class="w-2 h-2 rounded-full ${indicator}"></span>
            <//>
            <span>${label}</span>
            ${task.total_cost_usd ? html`
                <span class="ml-auto">
                    <${Tip} text="Total API cost | Input/output tokens">
                        <span>$${task.total_cost_usd.toFixed(4)} | ${((task.total_input_tokens || 0) / 1000).toFixed(0)}K in / ${((task.total_output_tokens || 0) / 1000).toFixed(1)}K out</span>
                    <//>
                </span>
            ` : null}
        </div>
    `;
}

// ── Main Panel ──────────────────────────────────────────────
export function GraphDetailPanel({ taskId, allTasks, jiraBaseUrl, onClose, onAction }) {
    const [task, setTask] = useState(null);
    const [error, setError] = useState(null);
    const [sessionLogOpen, setSessionLogOpen] = useState(false);
    const [dispatchLogOpen, setDispatchLogOpen] = useState(false);
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
        setSessionLogOpen(false);
        setDispatchLogOpen(false);
        loadTask();
        return () => { mountedRef.current = false; };
    }, [taskId]);

    useEffect(() => {
        if (!task) return;
        const gateActive = ['testing', 'test-passed', 'reviewing'].includes(task.gate_status);
        const activePoll = task.status === 'working' || task.status === 'ready' || task.status === 'needs-review' || task.status === 'turns-exhausted' || gateActive;
        // Active tasks poll fast, idle tasks poll slow (catch post-action updates)
        const interval = activePoll ? 3000 : 10000;
        const timer = setInterval(loadTask, interval);
        return () => clearInterval(timer);
    }, [task?.status, task?.gate_status, loadTask]);

    if (error) {
        return html`<div class="graph-detail-panel">
            <div class="flex items-center justify-between px-4 py-3 border-b border-slate-700">
                <span class="text-sm text-red-400">Error: ${error}</span>
                <button onClick=${onClose} class="text-slate-400 hover:text-slate-200 text-lg">\u00D7</button>
            </div>
        </div>`;
    }

    if (!task) {
        return html`<div class="graph-detail-panel">
            <div class="flex items-center justify-between px-4 py-3 border-b border-slate-700">
                <${LoadingState} message="Loading..." />
                <button onClick=${onClose} class="text-slate-400 hover:text-slate-200 text-lg">\u00D7</button>
            </div>
        </div>`;
    }

    const shortId = task.id.includes('/') ? task.id.split('/').pop() : task.id;
    const autoRefreshLogs = task.status === 'working';

    return html`
        <div class="graph-detail-panel">
            <!-- Header -->
            <div class="flex items-center justify-between px-4 py-3 border-b border-slate-700 sticky top-0 bg-slate-900 z-10">
                <div class="flex items-center gap-2 min-w-0">
                    <${StatusBadge} status=${task.status} task=${task} />
                    <${HeartbeatIndicator} task=${task} />
                    <span class="font-mono text-sm text-slate-200 truncate">${shortId}</span>
                    <${Tip} text="Open full task view">
                        <a href="#/tasks/${task.id}" class="text-xs text-blue-400 hover:text-blue-300 shrink-0">\u2197</a>
                    <//>
                </div>
                <button onClick=${onClose} class="text-slate-400 hover:text-slate-200 text-lg ml-2 shrink-0">\u00D7</button>
            </div>

            <!-- Action bar -->
            <div class="px-4 py-2 border-b border-slate-700 bg-slate-900">
                <${ActionButtons} task=${task} onAction=${onAction} />
            </div>

            <!-- Content -->
            <div class="overflow-y-auto flex-1 px-4 py-3">
                <!-- Goal & metadata -->
                <p class="text-sm text-slate-300 mb-2">${task.goal}</p>
                ${task.branch ? html`<div class="text-xs text-slate-500 mb-1">Branch: <span class="font-mono text-slate-400">${task.branch}</span></div>` : null}
                <${GitFlowSummary} task=${task} compact=${true} />
                <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500 mb-3">
                    ${task.model ? html`<span>Model: <span class="text-slate-400">${task.model}</span></span>` : null}
                    <${Tip} text="Total API cost across all dispatches">
                        <span>Cost: <span class="text-slate-400">$${(task.total_cost_usd || 0).toFixed(2)}</span></span>
                    <//>
                    <${Tip} text="Input/output token count">
                        <span>Tokens: <span class="text-slate-400">${((task.total_input_tokens || 0) / 1000).toFixed(0)}K in / ${((task.total_output_tokens || 0) / 1000).toFixed(1)}K out</span></span>
                    <//>
                    <span>Dispatches: <span class="text-slate-400">${task.dispatch_count || 0}</span></span>
                    ${task.phase ? html`<span>Phase: <span class="text-slate-400">${task.phase}</span></span>` : null}
                    <${PrUrlBadge} task=${task} />
                    ${task.jira_ticket ? html`<a href=${jiraUrl(task.jira_ticket, jiraBaseUrl)} target="_blank" rel="noopener"
                        class="px-1.5 py-0.5 rounded text-xs bg-cyan-500/20 text-cyan-400 hover:bg-cyan-500/30">${jiraLabel(task.jira_ticket)}</a>` : null}
                    ${task.conversation_id ? html`<a href="#/conversations/${encodeURIComponent(task.conversation_id)}"
                        class="px-1.5 py-0.5 rounded text-xs bg-indigo-500/20 text-indigo-400 hover:bg-indigo-500/30">\u{1F4AC} ${task.conversation_id}</a>` : null}
                    <${ClaudeChatLink} url=${task.claude_chat_url} />
                </div>

                <!-- Component badge + tags -->
                <div class="flex flex-wrap gap-1 mb-3">
                    ${task.project_id ? html`<span class="px-2 py-0.5 rounded text-xs bg-indigo-500/20 text-indigo-400">${task.project_id}</span>` : null}
                    ${(task.tags || []).map(t => html`<span key=${t} class="px-2 py-0.5 rounded text-xs bg-slate-700 text-slate-300">${t}</span>`)}
                </div>

                <!-- Spec — always visible, above attempt timeline -->
                <${SpecSection} messages=${task.messages} />

                <!-- Worktree indicator -->
                <${WorktreeIndicator} task=${task} />

                <${ChainVisualization} taskId=${task.id} onSelectTask=${(id) => onAction('select-task', id)} />
                <${ProofOfLife} task=${task} />
                <${GatePipeline} task=${task} />
                <${BlockersSection} task=${task} allTasks=${allTasks} />
                <${HeldSection} task=${task} allTasks=${allTasks} />
                <${ReviewSection} subtasks=${task.subtasks} />
                <${TestSection} subtasks=${task.subtasks} />
                <${Checklist} task=${task} />
                <${PlanSection} messages=${task.messages} />

                <!-- Messages grouped by attempt -->
                ${(() => {
                    const attempts = groupMessagesByAttempt(task.messages);
                    const nonPlanCount = (task.messages || []).filter(m => m.type !== 'plan').length;
                    if (attempts.length <= 1) {
                        return html`
                            <details class="mb-3" open>
                                <summary class="text-sm font-medium text-slate-400 cursor-pointer hover:text-slate-300 mb-2">
                                    Messages (${nonPlanCount})
                                </summary>
                                <div>
                                    <${MessageThread} messages=${task.messages} filterPlan=${true} idPrefix="panel-msg" />
                                </div>
                                <${MessageInput} taskId=${taskId} task=${task} onAction=${onAction} onRefresh=${loadTask} />
                            </details>`;
                    }
                    return html`
                        <div class="mb-3">
                            <div class="text-sm font-medium text-slate-400 mb-2">
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
                                            ${isLast && task.gate_status === 'testing' ? html`<span class="text-violet-400 status-dot-working">\u2699 Tests Running</span>` : null}
                                            ${isLast && task.gate_status === 'reviewing' ? html`<span class="text-pink-400 status-dot-working">\uD83D\uDC41 Claude is Reviewing</span>` : null}
                                            <span class="text-slate-500 ml-auto">${attempt.messages.length} msgs</span>
                                        </summary>
                                        <div class="px-2 pb-2">
                                            <${MessageThread} messages=${attempt.messages} idPrefix=${'attempt-' + idx} />
                                        </div>
                                    </details>`;
                            })}
                            <${MessageInput} taskId=${taskId} task=${task} onAction=${onAction} onRefresh=${loadTask} />
                        </div>`;
                })()}

                <!-- Session & Dispatch logs -->
                <${SessionLogPanel} taskId=${taskId} isOpen=${sessionLogOpen}
                    onToggle=${() => setSessionLogOpen(!sessionLogOpen)} autoRefresh=${autoRefreshLogs} />
                <${DispatchLogPanel} taskId=${taskId} isOpen=${dispatchLogOpen}
                    onToggle=${() => setDispatchLogOpen(!dispatchLogOpen)} />
            </div>
        </div>
    `;
}
