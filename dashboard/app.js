import { api } from './api.js';

// ── Sanitization guard ──────────────────────────────────────────────────
const sanitize = typeof DOMPurify !== 'undefined'
    ? (html) => DOMPurify.sanitize(html)
    : (html) => html;

// ── State ────────────────────────────────────────────────────────────────
let pollTimer = null;
let currentView = null;
let jiraBaseUrl = null;  // Set from /api/system response

// Track UI toggle states across poll re-renders
const uiState = {
    expandedMessages: new Set(),   // IDs of expanded long messages
    sessionLogOpen: false,
    dispatchLogOpen: false,
    sessionLogLoaded: false,
    dispatchLogLoaded: false,
    sessionLogFilters: new Set(['text', 'tool', 'error']),  // all on by default
};

// ── Router ───────────────────────────────────────────────────────────────
function getRoute() {
    const hash = location.hash.slice(1) || '/';
    if (hash.startsWith('/tasks/')) return { view: 'detail', taskId: hash.slice(7) };
    if (hash.startsWith('/conversations/')) return { view: 'conversation-detail', convId: decodeURIComponent(hash.slice(15)) };
    if (hash === '/conversations') return { view: 'conversations' };
    if (hash === '/projects') return { view: 'projects' };
    return { view: 'board', params: Object.fromEntries(new URLSearchParams(hash.slice(2))) };
}

function navigate(hash) {
    location.hash = hash;
}

function startRouter() {
    window.addEventListener('hashchange', route);
    route();
}

function route() {
    stopPolling();
    const r = getRoute();
    currentView = r.view;
    document.getElementById('view-board').classList.add('hidden');
    document.getElementById('view-detail').classList.add('hidden');
    document.getElementById('view-projects').classList.add('hidden');
    document.getElementById('view-conversations').classList.add('hidden');
    document.getElementById('view-conversation-detail').classList.add('hidden');

    if (r.view === 'board') showBoard(r.params);
    else if (r.view === 'detail') showDetail(r.taskId);
    else if (r.view === 'projects') showProjects();
    else if (r.view === 'conversations') showConversations();
    else if (r.view === 'conversation-detail') showConversationDetail(r.convId);

    // Update nav active state
    document.querySelectorAll('[data-nav]').forEach(el => {
        const navView = el.dataset.nav;
        const isActive = navView === r.view || (navView === 'conversations' && r.view === 'conversation-detail');
        el.classList.toggle('text-slate-100', isActive);
        el.classList.toggle('text-slate-400', !isActive);
    });
}

// ── Utilities ────────────────────────────────────────────────────────────
function relativeTime(iso) {
    if (!iso) return '—';
    const diff = Math.max(0, (Date.now() - new Date(iso + (iso.endsWith('Z') ? '' : 'Z')).getTime()) / 1000);
    if (diff < 5) return 'just now';
    if (diff < 60) return `${Math.floor(diff)}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}

function progressBar(done, total, len = 10) {
    if (total === 0) return '░'.repeat(len);
    const filled = Math.round(done / total * len);
    return '▓'.repeat(filled) + '░'.repeat(len - filled);
}

function statusBadge(status) {
    const map = {
        working:      { bg: 'bg-emerald-500/20', text: 'text-emerald-400', icon: '●', dot: true },
        completed:    { bg: 'bg-blue-500/20', text: 'text-blue-400', icon: '✓' },
        failed:       { bg: 'bg-red-500/20', text: 'text-red-400', icon: '✕' },
        'needs-review': { bg: 'bg-amber-500/20', text: 'text-amber-400', icon: '⚠' },
        'turns-exhausted': { bg: 'bg-orange-500/20', text: 'text-orange-400', icon: '⏳' },
        cancelled:    { bg: 'bg-slate-500/20', text: 'text-slate-400', icon: '—' },
        ready:        { bg: 'bg-slate-500/20', text: 'text-slate-300', icon: '○' },
    };
    const s = map[status] || map.ready;
    const dotClass = s.dot ? 'status-dot-working' : '';
    return `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-medium ${s.bg} ${s.text}">
        <span class="${dotClass}">${s.icon}</span> ${status.toUpperCase()}
    </span>`;
}

function gateBadge(task) {
    if (!task.gate_status || task.gate_status === 'passed') return '';
    const map = {
        testing:        { bg: 'bg-violet-500/20', text: 'text-violet-400', icon: '⚙', pulse: true },
        reviewing:      { bg: 'bg-pink-500/20', text: 'text-pink-400', icon: '👁', pulse: true },
        'test-failed':  { bg: 'bg-red-500/20', text: 'text-red-400', icon: '✕' },
        'review-failed': { bg: 'bg-red-500/20', text: 'text-red-400', icon: '✕' },
    };
    const s = map[task.gate_status];
    if (!s) return '';
    const retries = task.gate_retries > 0 ? ` (${task.gate_retries}/${task.max_gate_retries || 3})` : '';
    const pulseClass = s.pulse ? 'status-dot-working' : '';
    return `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-medium ${s.bg} ${s.text}">
        <span class="${pulseClass}">${s.icon}</span> GATE: ${task.gate_status.toUpperCase()}${retries}
    </span>`;
}

function actionButtons(task) {
    const btns = [];
    if (task.status === 'working') {
        btns.push(`<button onclick="window._action('cancel','${task.id}')" class="px-2 py-1 text-xs rounded bg-red-500/20 text-red-400 hover:bg-red-500/30">Cancel</button>`);
    }
    if (task.status === 'failed' || task.status === 'cancelled') {
        btns.push(`<button onclick="window._action('retry','${task.id}')" class="px-2 py-1 text-xs rounded bg-amber-500/20 text-amber-400 hover:bg-amber-500/30">Retry</button>`);
    }
    if (task.status === 'completed') {
        btns.push(`<button onclick="window._action('resume','${task.id}')" class="px-2 py-1 text-xs rounded bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30">Resume</button>`);
        btns.push(`<button onclick="window._action('retry','${task.id}')" class="px-2 py-1 text-xs rounded bg-amber-500/20 text-amber-400 hover:bg-amber-500/30">Retry</button>`);
        btns.push(`<button onclick="window._action('close','${task.id}')" class="px-2 py-1 text-xs rounded bg-slate-500/20 text-slate-400 hover:bg-slate-500/30">Close</button>`);
    }
    if (task.status === 'needs-review' || task.status === 'turns-exhausted') {
        btns.push(`<button onclick="window._action('resume','${task.id}')" class="px-2 py-1 text-xs rounded bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30">Resume</button>`);
        btns.push(`<button onclick="window._action('retry','${task.id}')" class="px-2 py-1 text-xs rounded bg-amber-500/20 text-amber-400 hover:bg-amber-500/30">Retry</button>`);
        btns.push(`<button onclick="window._action('cancel','${task.id}')" class="px-2 py-1 text-xs rounded bg-red-500/20 text-red-400 hover:bg-red-500/30">Cancel</button>`);
    }
    // Gate actions
    if (task.gate_status && ['testing', 'reviewing', 'test-failed', 'review-failed'].includes(task.gate_status)) {
        btns.push(`<button onclick="window._action('skip-gate','${task.id}')" class="px-2 py-1 text-xs rounded bg-violet-500/20 text-violet-400 hover:bg-violet-500/30">Skip Gate</button>`);
    }
    // Chain actions
    if (task.status === 'completed' && task.gate_status === 'passed') {
        btns.push(`<button onclick="window._action('advance-chain','${task.id}')" class="px-2 py-1 text-xs rounded bg-indigo-500/20 text-indigo-400 hover:bg-indigo-500/30">Advance Chain</button>`);
    }
    if (task.depends_on || task.gate_status) {
        btns.push(`<button onclick="window._action('cancel-chain','${task.id}')" class="px-2 py-1 text-xs rounded bg-red-500/10 text-red-400/70 hover:bg-red-500/20">Cancel Chain</button>`);
    }
    return btns.join(' ');
}

function prUrlBadge(task) {
    const prUrl = task.pr_url || (task.artifacts && task.artifacts.find(a => a.type === 'pr_url')?.ref);
    if (!prUrl) return '';
    return `<a href="${escapeHtml(prUrl)}" target="_blank" rel="noopener" class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-purple-500/20 text-purple-400 hover:bg-purple-500/30">PR ↗</a>`;
}


function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function jiraUrl(ticket) {
    if (!ticket) return '#';
    // If it's already a URL, use it directly
    if (ticket.startsWith('http')) return escapeHtml(ticket);
    // Build URL from configured base (e.g. https://redrhino.atlassian.net)
    if (jiraBaseUrl) return escapeHtml(`${jiraBaseUrl}/browse/${ticket}`);
    return '#';
}

function jiraLabel(ticket) {
    if (!ticket) return '';
    // If full URL, extract ticket ID from path
    if (ticket.startsWith('http')) {
        const parts = ticket.split('/');
        return parts[parts.length - 1] || ticket;
    }
    return ticket;
}

function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// ── Actions ──────────────────────────────────────────────────────────────
window._action = async (action, taskId) => {
    const labels = { cancel: 'Cancel', retry: 'Retry', resume: 'Resume', close: 'Close', 'skip-gate': 'Skip Gate', 'advance-chain': 'Advance Chain', 'cancel-chain': 'Cancel Chain' };
    const msg = action === 'close'
        ? `Close task "${taskId}"? This will clean up the worktree and branch.`
        : action === 'skip-gate'
        ? `Skip gate for "${taskId}"? This bypasses automated checks.`
        : action === 'advance-chain'
        ? `Dispatch next dependent task in the chain?`
        : action === 'cancel-chain'
        ? `Cancel "${taskId}" and ALL dependent tasks?`
        : `${labels[action]} task "${taskId}"?`;
    if (!confirm(msg)) return;
    try {
        if (action === 'cancel') await api.cancelTask(taskId);
        else if (action === 'retry') await api.retryTask(taskId);
        else if (action === 'resume') await api.resumeTask(taskId);
        else if (action === 'close') await api.closeTask(taskId);
        else if (action === 'skip-gate') await api.skipGate(taskId);
        else if (action === 'advance-chain') await api.advanceChain(taskId);
        else if (action === 'cancel-chain') await api.cancelChain(taskId);
        route(); // Refresh current view
    } catch (e) {
        alert(`Error: ${e.message}`);
    }
};

window._navigate = (hash) => navigate(hash);

window._toggleMsg = (collapseId) => {
    if (uiState.expandedMessages.has(collapseId)) {
        uiState.expandedMessages.delete(collapseId);
    } else {
        uiState.expandedMessages.add(collapseId);
    }
    const el = document.getElementById(collapseId);
    if (el) el.classList.toggle('msg-collapsed');
    const btn = el?.parentElement?.querySelector('button');
    if (btn) btn.textContent = uiState.expandedMessages.has(collapseId) ? 'Collapse ▴' : 'Expand ▾';
};

// ── Board View ───────────────────────────────────────────────────────────
async function showBoard(params = {}) {
    const container = document.getElementById('view-board');
    container.classList.remove('hidden');

    async function render() {
        try {
            const [tasks, sys] = await Promise.all([api.getTasks(params), api.getSystem()]);
            if (sys.jira_base_url) jiraBaseUrl = sys.jira_base_url;
            document.getElementById('header-active').textContent = `${sys.active_tasks} active`;
            document.getElementById('header-cost').textContent = `$${sys.total_cost_usd.toFixed(2)} total`;

            const tbody = document.getElementById('task-table-body');
            if (tasks.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" class="p-8 text-center text-slate-500">No tasks found</td></tr>';
                return;
            }

            tbody.innerHTML = tasks.map(t => `

                <tr class="border-b border-slate-800 hover:bg-slate-800/50 cursor-pointer"
                    onclick="window._navigate('#/tasks/${t.id}')">
                    <td class="p-3">${statusBadge(t.status)} ${gateBadge(t)}</td>
                    <td class="p-3">
                        <div class="flex items-center gap-2">
                            <span class="font-mono text-sm text-slate-200">${escapeHtml(t.id)}</span>
                            ${t.pr_url ? `<a href="${escapeHtml(t.pr_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs bg-purple-500/20 text-purple-400 hover:bg-purple-500/30" title="View PR">PR</a>` : ''}
                            ${t.jira_ticket ? `<a href="${jiraUrl(t.jira_ticket)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs bg-cyan-500/20 text-cyan-400 hover:bg-cyan-500/30" title="Jira">${escapeHtml(jiraLabel(t.jira_ticket))}</a>` : ''}
                        </div>
                        <div class="text-sm text-slate-400 truncate max-w-md">${escapeHtml(t.goal)}</div>
                        <div class="flex items-center gap-1 mt-0.5">
                            ${t.phase ? `<span class="text-xs text-slate-500">${escapeHtml(t.phase)}</span>` : ''}
                            ${(t.tags || []).map(tag => `<span class="px-1.5 py-0 rounded text-xs bg-slate-700 text-slate-300">${escapeHtml(tag)}</span>`).join('')}
                        </div>
                    </td>
                    <td class="p-3">
                        <span class="font-mono text-xs text-slate-400 progress-bar">${progressBar(t.checklist_done, t.checklist_total)}</span>
                        <span class="text-xs text-slate-400 ml-1">${t.checklist_done}/${t.checklist_total}</span>
                    </td>
                    <td class="p-3 text-sm text-slate-400">$${(t.total_cost_usd || 0).toFixed(2)}</td>
                    <td class="p-3 text-xs text-slate-500">${relativeTime(t.last_activity || t.updated_at)}</td>
                    <td class="p-3" onclick="event.stopPropagation()">${actionButtons(t)}</td>
                </tr>
            `).join('');
        } catch (e) {
            // Silently skip poll errors — don't clear the page
            console.warn('Board poll error (skipping):', e.message);
        }
    }

    await render();
    pollTimer = setInterval(render, 10000);

    // Setup filters
    setupFilters(params);
}

async function setupFilters(currentParams) {
    try {
        const [tasks, projects] = await Promise.all([api.getTasks(), api.getProjects()]);

        // Status filter
        const statuses = [...new Set(tasks.map(t => t.status))];
        const statusSelect = document.getElementById('filter-status');
        statusSelect.innerHTML = '<option value="">All statuses</option>' +
            statuses.map(s => `<option value="${s}" ${currentParams.status === s ? 'selected' : ''}>${s}</option>`).join('');

        // Project filter
        const projectSelect = document.getElementById('filter-project');
        projectSelect.innerHTML = '<option value="">All projects</option>' +
            projects.map(p => `<option value="${p.id}" ${currentParams.project_id === p.id ? 'selected' : ''}>${p.id}</option>`).join('');

        const applyFilter = () => {
            const params = {};
            if (statusSelect.value) params.status = statusSelect.value;
            if (projectSelect.value) params.project_id = projectSelect.value;
            const qs = new URLSearchParams(params).toString();
            navigate('#/' + (qs ? '?' + qs : ''));
        };

        statusSelect.onchange = applyFilter;
        projectSelect.onchange = applyFilter;
    } catch (e) {
        console.error('Filter setup error:', e);
    }
}

// ── Task Detail View ─────────────────────────────────────────────────────
async function showDetail(taskId) {
    const container = document.getElementById('view-detail');
    container.classList.remove('hidden');

    // Reset log state and clear stale log DOM for new task view
    uiState.sessionLogOpen = false;
    uiState.dispatchLogOpen = false;
    uiState.sessionLogLoaded = false;
    uiState.dispatchLogLoaded = false;
    uiState.expandedMessages.clear();
    const slp = document.getElementById('session-log-content');
    const dlp = document.getElementById('dispatch-log-content');
    if (slp) slp.innerHTML = '';
    if (dlp) dlp.innerHTML = '';

    let initialLoad = true;
    async function render() {
        try {
            const task = await api.getTask(taskId);
            renderDetailHeader(task);
            renderGateSection(task);
            renderChecklist(task);
            renderPlan(task);
            renderMessages(task);
            // Show resume button by message area for resumable tasks
            window._currentTaskId = taskId;
            const resumeBtn = document.getElementById('message-resume');
            if (resumeBtn) {
                const resumable = ['completed', 'needs-review', 'turns-exhausted'].includes(task.status);
                resumeBtn.classList.toggle('hidden', !resumable);
            }
            // Refresh open log panels during poll
            if (!initialLoad) {
                await refreshOpenLogPanels(taskId);
            }
        } catch (e) {
            // On poll errors, silently skip — don't blow up the page
            if (!initialLoad) { console.warn('Poll error (skipping):', e.message); return; }
            document.getElementById('detail-header').innerHTML = `<div class="p-8 text-red-400">Error loading task: ${e.message}</div>`;
        }
        initialLoad = false;
    }

    await render();

    // Render chain and review sub-task (less frequent, not on every poll)
    await Promise.all([renderChainSection(taskId), renderReviewSubTask(taskId)]);

    // Poll — refresh header/checklist/messages but not log panels or message input
    const task = await api.getTask(taskId).catch(() => null);
    const gateActive = task && ['testing', 'reviewing'].includes(task.gate_status);
    if (task && (task.status === 'working' || task.status === 'needs-review' || task.status === 'turns-exhausted' || gateActive)) {
        pollTimer = setInterval(render, 5000);
    }

    // Reset log panel DOM on task change (uiState already reset above)
    const sessionPanel = document.getElementById('session-log-content');
    const dispatchPanel = document.getElementById('dispatch-log-content');
    sessionPanel.innerHTML = '';
    sessionPanel.classList.add('hidden');
    dispatchPanel.innerHTML = '';
    dispatchPanel.classList.add('hidden');

    // Setup session/dispatch log toggles (one-time, not affected by poll)
    setupLogPanels(taskId);

    // Setup message input (one-time)
    setupMessageInput(taskId);
}

function renderDetailHeader(task) {
    const el = document.getElementById('detail-header');
    el.innerHTML = `
        <div class="flex items-start justify-between">
            <div class="flex-1">
                <div class="flex items-center gap-3 mb-2">
                    ${statusBadge(task.status)}
                    ${gateBadge(task)}
                    <span class="font-mono text-lg text-slate-200">${escapeHtml(task.id)}</span>
                </div>
                <p class="text-slate-300 mb-3">${escapeHtml(task.goal)}</p>
                <div class="flex flex-wrap gap-x-6 gap-y-1 text-sm text-slate-400">
                    <span>Branch: <span class="font-mono text-slate-300">${escapeHtml(task.branch || '—')}</span></span>
                    <span>Dispatches: <span class="text-slate-300">${task.dispatch_count || 0}</span></span>
                    <span>Cost: <span class="text-slate-300">$${(task.total_cost_usd || 0).toFixed(2)}</span></span>
                    <span>Tokens: <span class="text-slate-300">${((task.total_input_tokens || 0) / 1000).toFixed(0)}K in / ${((task.total_output_tokens || 0) / 1000).toFixed(1)}K out</span></span>
                    ${task.model ? `<span>Model: <span class="text-slate-300">${escapeHtml(task.model)}</span></span>` : ''}
                    ${task.phase ? `<span>Phase: <span class="text-slate-300">${escapeHtml(task.phase)}</span></span>` : ''}
                    ${prUrlBadge(task)}
                    ${task.jira_ticket ? `<a href="${jiraUrl(task.jira_ticket)}" target="_blank" rel="noopener" class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-cyan-500/20 text-cyan-400 hover:bg-cyan-500/30">${escapeHtml(jiraLabel(task.jira_ticket))}</a>` : ''}
                    ${task.conversation_id ? `<a href="#/conversations/${encodeURIComponent(task.conversation_id)}" class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-indigo-500/20 text-indigo-400 hover:bg-indigo-500/30">Conv: ${escapeHtml(task.conversation_id)}</a>` : ''}
                </div>
                ${(task.tags || []).length > 0 ? `<div class="flex gap-1 mt-2">${task.tags.map(t => `<span class="px-2 py-0.5 rounded text-xs bg-slate-700 text-slate-300">${escapeHtml(t)}</span>`).join('')}</div>` : ''}
            </div>
            <div class="flex gap-2 ml-4">${actionButtons(task)}</div>
        </div>
    `;
}

function renderChecklist(task) {
    const el = document.getElementById('detail-checklist');
    const items = task.checklist || [];
    if (items.length === 0) {
        el.innerHTML = '<p class="text-slate-500 text-sm">No checklist items</p>';
        return;
    }
    el.innerHTML = `
        <h3 class="text-sm font-medium text-slate-300 mb-2">Checklist (${task.checklist_done}/${task.checklist_total})</h3>
        <div class="space-y-1">
            ${items.map(c => `
                <div class="flex items-center gap-2 text-sm ${c.done ? 'text-slate-400' : 'text-slate-200'}">
                    <span>${c.done ? '✅' : '⬜'}</span>
                    <span>${escapeHtml(c.item)}</span>
                </div>
            `).join('')}
        </div>
    `;
}

function renderPlan(task) {
    const el = document.getElementById('detail-plan');
    if (!el) return;
    const msgs = task.messages || [];
    // Find most recent plan message
    const planMsg = [...msgs].reverse().find(m => m.type === 'plan');
    if (!planMsg) {
        el.innerHTML = '';
        return;
    }
    const contentHtml = sanitize(marked.parse(planMsg.content || ''));
    const time = planMsg.created_at ? new Date(planMsg.created_at + (planMsg.created_at.endsWith('Z') ? '' : 'Z')).toLocaleTimeString() : '';
    el.innerHTML = `
        <details class="bg-slate-900 border border-slate-800 rounded-lg mb-4" open>
            <summary class="px-4 py-3 text-sm font-medium text-slate-300 cursor-pointer hover:text-slate-100">
                Implementation Plan <span class="text-xs text-slate-500 ml-2">${time}</span>
            </summary>
            <div class="px-4 pb-3 prose-dark text-sm border-t border-slate-700/50">
                ${contentHtml}
            </div>
        </details>
    `;
}

function renderMessages(task) {
    const el = document.getElementById('detail-messages');
    const msgs = (task.messages || []).filter(m => m.type !== 'plan');
    if (msgs.length === 0) {
        el.innerHTML = '<p class="text-slate-500 text-sm">No messages yet</p>';
        return;
    }

    const borderColors = {
        spec: 'border-l-blue-500',
        progress: 'border-l-emerald-500',
        question: 'border-l-amber-500',
        status: 'border-l-slate-500',
        result: 'border-l-purple-500',
        review: 'border-l-pink-500',
        answer: 'border-l-cyan-500',
        'test-result': 'border-l-violet-500',
        handoff: 'border-l-teal-500',
    };

    el.innerHTML = msgs.map((m, i) => {
        const border = borderColors[m.type] || 'border-l-slate-600';
        const pinIcon = m.pinned || m._pinned_marker ? '📌 ' : '';
        const type = (m.type || 'note').toUpperCase();
        const time = m.created_at ? new Date(m.created_at + (m.created_at.endsWith('Z') ? '' : 'Z')).toLocaleTimeString() : '';
        const contentHtml = sanitize(marked.parse(m.content || ''));
        const isLong = (m.content || '').length > 500;
        const collapseId = `msg-${m.id || i}`;

        return `
            <div class="border-l-2 ${border} bg-slate-800/50 rounded-r mb-3">
                <div class="flex items-center gap-2 px-3 py-1.5 text-xs text-slate-400 border-b border-slate-700/50">
                    <span>${pinIcon}${type}</span>
                    <span>—</span>
                    <span>${escapeHtml(m.author || '')}</span>
                    <span>—</span>
                    <span>${time}</span>
                    ${m.title ? `<span class="text-slate-300 ml-1">${escapeHtml(m.title)}</span>` : ''}
                </div>
                <div id="${collapseId}" class="px-3 py-2 prose-dark text-sm ${isLong && !uiState.expandedMessages.has(collapseId) ? 'msg-collapsed' : ''}">
                    ${contentHtml}
                </div>
                ${isLong ? `<button onclick="window._toggleMsg('${collapseId}')" class="px-3 py-1 text-xs text-slate-400 hover:text-slate-200">${uiState.expandedMessages.has(collapseId) ? 'Collapse ▴' : 'Expand ▾'}</button>` : ''}
            </div>
        `;
    }).join('');
}

function _logExpandable(ts, label, labelCls, preview, full, logType) {
    const dt = logType ? ` data-log-type="${logType}"` : '';
    // If full content is same as preview or empty, no expand needed
    if (!full || full === preview) {
        return `<div${dt} class="${labelCls} text-xs py-0.5 log-entry"><span class="text-slate-600 mr-2">${ts}</span>${label} ${escapeHtml(preview)}</div>`;
    }
    const id = `log-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    return `<div${dt} class="${labelCls} text-xs py-0.5 cursor-pointer log-entry" onclick="document.getElementById('${id}').classList.toggle('hidden')"><span class="text-slate-600 mr-2">${ts}</span>${label} ${escapeHtml(preview)} <span class="text-slate-600">▸</span></div><div${dt} id="${id}" class="hidden text-xs ml-8 py-1 px-2 mb-1 bg-slate-800/50 rounded whitespace-pre-wrap text-slate-300 max-h-96 overflow-y-auto log-entry">${escapeHtml(full)}</div>`;
}

function _sessionLogFilterBar() {
    const f = uiState.sessionLogFilters;
    const btn = (key, label) => {
        const on = f.has(key);
        const cls = on ? 'bg-slate-600 text-slate-200' : 'bg-slate-800 text-slate-400 hover:bg-slate-700';
        return `<button data-filter="${key}" class="px-2 py-0.5 text-xs rounded ${cls}" onclick="window._toggleSessionLogFilter('${key}')">${label}</button>`;
    };
    return `<div class="flex gap-1 mb-2 pb-2 border-b border-slate-700/50 sticky top-0 bg-slate-900 z-10 pt-1" id="session-log-filters">
        ${btn('text', 'Text')}${btn('tool', 'Tools')}${btn('error', 'Errors')}
    </div>`;
}

window._toggleSessionLogFilter = function(key) {
    const f = uiState.sessionLogFilters;
    if (f.has(key)) {
        f.delete(key);
    } else {
        f.add(key);
    }
    // If none selected, turn all back on
    if (f.size === 0) {
        f.add('text'); f.add('tool'); f.add('error');
    }
    _applySessionLogFilters();
};

function _applySessionLogFilters() {
    const panel = document.getElementById('session-log-content');
    if (!panel) return;
    const f = uiState.sessionLogFilters;
    // Update button styles
    panel.querySelectorAll('#session-log-filters button').forEach(btn => {
        const on = f.has(btn.dataset.filter);
        btn.className = `px-2 py-0.5 text-xs rounded ${on ? 'bg-slate-600 text-slate-200' : 'bg-slate-800 text-slate-400 hover:bg-slate-700'}`;
    });
    // Toggle entry visibility
    panel.querySelectorAll('.log-entry').forEach(el => {
        const type = el.dataset.logType || '';
        el.style.display = (f.has(type) || !type) ? '' : 'none';
    });
}

function renderSessionLogHtml(entries) {
    if (entries.length === 0) {
        return '<p class="text-slate-500 text-sm p-2">No session log</p>';
    }
    const filter = uiState.sessionLogFilter || 'all';
    const lines = entries.map(e => {
        const ts = e.timestamp ? new Date(e.timestamp).toLocaleTimeString() : '';
        const type = e.type || '';

        if (type === 'SystemMessage') {
            return `<div data-log-type="system" class="log-system text-xs py-0.5 log-entry"><span class="text-slate-600 mr-2">${ts}</span>SYSTEM ${e.subtype || ''}</div>`;
        }
        if (type === 'AssistantMessage') {
            const blocks = e.content || [];
            return blocks.map(b => {
                if (b.type === 'text') {
                    const full = b.text || '';
                    const preview = full.slice(0, 150);
                    return _logExpandable(ts, 'TEXT ', 'log-text', preview + (full.length > 150 ? '…' : ''), full.length > 150 ? full : null, 'text');
                }
                if (b.type === 'tool_use') {
                    const input = b.input || '';
                    const preview = `${b.name || ''} → ${input.slice(0, 100)}`;
                    return _logExpandable(ts, 'TOOL ', 'log-tool', preview + (input.length > 100 ? '…' : ''), input.length > 100 ? input : null, 'tool');
                }
                return '';
            }).join('');
        }
        if (type === 'UserMessage') {
            const blocks = e.content || [];
            return blocks.map(b => {
                if (b.type === 'tool_result') {
                    const content = b.preview || '';
                    if (b.is_error) {
                        return _logExpandable(ts, 'RESULT', 'log-result text-red-400', '(error)', content || null, 'error');
                    }
                    const preview = content.slice(0, 120);
                    return _logExpandable(ts, 'RESULT', 'log-result', preview ? preview + (content.length > 120 ? '…' : '') : `(${content.length}B)`, content.length > 120 ? content : null, 'tool');
                }
                return '';
            }).join('');
        }
        if (type === 'ResultMessage') {
            const cls = e.is_error ? 'log-error' : 'log-done';
            const result = e.result || '';
            const summary = `${e.num_turns || '?'} turns | $${(e.cost_usd || 0).toFixed(2)}`;
            return _logExpandable(ts, 'DONE ', cls + ' font-medium', summary, result || null, 'text');
        }
        return '';
    }).join('');
    const html = _sessionLogFilterBar() + `<div id="session-log-entries">${lines}</div>`;
    return html;
}

function renderDispatchLogHtml(text) {
    return text
        ? `<pre class="text-xs text-slate-400 whitespace-pre-wrap">${escapeHtml(text)}</pre>`
        : '<p class="text-slate-500 text-sm">No dispatch log</p>';
}

function updatePanelWithScrollPin(panel, html) {
    const wasAtBottom = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 30;
    panel.innerHTML = html;
    if (wasAtBottom) {
        panel.scrollTop = panel.scrollHeight;
    }
}

async function refreshOpenLogPanels(taskId) {
    if (uiState.sessionLogOpen) {
        try {
            const entries = await api.getSessionLog(taskId);
            const panel = document.getElementById('session-log-content');
            updatePanelWithScrollPin(panel, renderSessionLogHtml(entries));
            // Re-apply active filter after re-render
            if (uiState.sessionLogFilters.size < 3) {
                _applySessionLogFilters();
            }
            uiState.sessionLogLoaded = true;
        } catch (e) {
            console.warn('Session log poll error:', e.message);
        }
    }
    if (uiState.dispatchLogOpen) {
        try {
            const text = await api.getDispatchLog(taskId);
            const panel = document.getElementById('dispatch-log-content');
            updatePanelWithScrollPin(panel, renderDispatchLogHtml(text));
            uiState.dispatchLogLoaded = true;
        } catch (e) {
            console.warn('Dispatch log poll error:', e.message);
        }
    }
}

async function setupLogPanels(taskId) {
    // Session log toggle
    const sessionBtn = document.getElementById('session-log-toggle');
    const sessionPanel = document.getElementById('session-log-content');

    sessionBtn.onclick = async () => {
        uiState.sessionLogOpen = !uiState.sessionLogOpen;
        sessionPanel.classList.toggle('hidden');
        if (!uiState.sessionLogLoaded) {
            uiState.sessionLogLoaded = true;
            try {
                const entries = await api.getSessionLog(taskId);
                sessionPanel.innerHTML = renderSessionLogHtml(entries);
                if (uiState.sessionLogFilter && uiState.sessionLogFilter !== 'all') {
                    window._filterSessionLog(uiState.sessionLogFilter);
                }
            } catch (e) {
                sessionPanel.innerHTML = `<p class="text-red-400 text-sm p-2">Error: ${e.message}</p>`;
            }
        }
    };

    // Dispatch log toggle
    const dispatchBtn = document.getElementById('dispatch-log-toggle');
    const dispatchPanel = document.getElementById('dispatch-log-content');

    dispatchBtn.onclick = async () => {
        uiState.dispatchLogOpen = !uiState.dispatchLogOpen;
        dispatchPanel.classList.toggle('hidden');
        if (!uiState.dispatchLogLoaded) {
            uiState.dispatchLogLoaded = true;
            try {
                const text = await api.getDispatchLog(taskId);
                dispatchPanel.innerHTML = renderDispatchLogHtml(text);
            } catch (e) {
                dispatchPanel.innerHTML = `<p class="text-red-400 text-sm">Error: ${e.message}</p>`;
            }
        }
    };
}

function setupMessageInput(taskId) {
    const form = document.getElementById('message-form');
    const input = document.getElementById('message-input');
    const typeSelect = document.getElementById('message-type');

    form.onsubmit = async (e) => {
        e.preventDefault();
        const content = input.value.trim();
        if (!content) return;

        try {
            await api.postMessage(taskId, content, typeSelect.value);
            input.value = '';
            // Refresh messages
            const task = await api.getTask(taskId);
            renderMessages(task);
        } catch (err) {
            alert(`Error posting message: ${err.message}`);
        }
    };
}

// ── Projects View ────────────────────────────────────────────────────────
async function showProjects() {
    const container = document.getElementById('view-projects');
    container.classList.remove('hidden');

    try {
        const projects = await api.getProjects();
        const el = document.getElementById('projects-list');
        if (projects.length === 0) {
            el.innerHTML = '<p class="text-slate-500 text-center p-8">No projects registered</p>';
            return;
        }

        el.innerHTML = projects.map(p => `
            <div class="bg-slate-900 border border-slate-700 rounded-lg p-4 hover:border-slate-600 cursor-pointer"
                 onclick="window._navigate('#/?project_id=${p.id}')">
                <h3 class="text-lg font-medium text-slate-200 mb-1">${escapeHtml(p.id)}</h3>
                <div class="text-sm text-slate-400 mb-2">
                    <span class="font-mono">${escapeHtml(p.repo)}</span>
                    <span class="mx-2">·</span>
                    branch: <span class="font-mono">${escapeHtml(p.default_branch)}</span>
                </div>
                <div class="flex gap-4 text-sm">
                    <span class="${p.active_task_count > 0 ? 'text-emerald-400' : 'text-slate-500'}">${p.active_task_count} active</span>
                    <span class="text-slate-500">${p.total_tasks} total</span>
                    <span class="text-slate-500">$${p.total_cost.toFixed(2)}</span>
                </div>
            </div>
        `).join('');
    } catch (e) {
        document.getElementById('projects-list').innerHTML = `<p class="text-red-400 p-4">Error: ${e.message}</p>`;
    }
}

// ── Conversations View ───────────────────────────────────────────────────
async function showConversations() {
    const container = document.getElementById('view-conversations');
    container.classList.remove('hidden');

    try {
        const conversations = await api.getConversations();
        const el = document.getElementById('conversations-list');
        if (conversations.length === 0) {
            el.innerHTML = '<p class="text-slate-500 text-center p-8">No conversations</p>';
            return;
        }

        el.innerHTML = `
            <div class="bg-slate-900 border border-slate-800 rounded-lg overflow-hidden">
                <table class="w-full">
                    <thead>
                        <tr class="border-b border-slate-800 text-xs text-slate-500 uppercase">
                            <th class="p-3 text-left">Conversation</th>
                            <th class="p-3 text-left w-32">Project</th>
                            <th class="p-3 text-left w-20">Messages</th>
                            <th class="p-3 text-left w-24">Activity</th>
                            <th class="p-3 text-left w-16">Pinned</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${conversations.map(c => `
                            <tr class="border-b border-slate-800 hover:bg-slate-800/50 cursor-pointer"
                                onclick="window._navigate('#/conversations/${encodeURIComponent(c.id)}')">
                                <td class="p-3">
                                    <div class="font-mono text-sm text-slate-200">${escapeHtml(c.id)}</div>
                                    <div class="text-sm text-slate-400 truncate max-w-md">${escapeHtml(c.goal || '')}</div>
                                </td>
                                <td class="p-3 text-sm text-slate-400">${escapeHtml(c.project || '')}</td>
                                <td class="p-3 text-sm text-slate-400">${c.message_count || 0}</td>
                                <td class="p-3 text-xs text-slate-500">${relativeTime(c.last_message_at || c.updated_at)}</td>
                                <td class="p-3 text-sm">${c.has_pinned ? '📌' : ''}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        `;
    } catch (e) {
        document.getElementById('conversations-list').innerHTML = `<p class="text-red-400 p-4">Error: ${e.message}</p>`;
    }
}

// ── Conversation Detail View ─────────────────────────────────────────────
async function showConversationDetail(convId) {
    const container = document.getElementById('view-conversation-detail');
    container.classList.remove('hidden');

    try {
        const thread = await api.getConversation(convId);
        const msgs = thread.messages || [];

        document.getElementById('conversation-header').innerHTML = `
            <div class="flex items-center gap-3 mb-1">
                <span class="font-mono text-lg text-slate-200">${escapeHtml(convId)}</span>
            </div>
            <div class="text-sm text-slate-400">${msgs.length} messages</div>
        `;

        const borderColors = {
            spec: 'border-l-blue-500',
            plan: 'border-l-teal-500',
            question: 'border-l-amber-500',
            status: 'border-l-slate-500',
            note: 'border-l-slate-600',
        };

        const el = document.getElementById('conversation-messages');
        if (msgs.length === 0) {
            el.innerHTML = '<p class="text-slate-500 text-sm">No messages</p>';
            return;
        }

        el.innerHTML = msgs.map((m, i) => {
            const border = borderColors[m.type] || 'border-l-slate-600';
            const pinIcon = m.pinned || m._pinned_marker ? '📌 ' : '';
            const type = (m.type || 'note').toUpperCase();
            const time = m.created_at ? new Date(m.created_at + (m.created_at.endsWith('Z') ? '' : 'Z')).toLocaleTimeString() : '';
            const contentHtml = sanitize(marked.parse(m.content || ''));
            const isLong = (m.content || '').length > 500;
            const collapseId = `conv-msg-${m.id || i}`;

            return `
                <div class="border-l-2 ${border} bg-slate-800/50 rounded-r mb-3">
                    <div class="flex items-center gap-2 px-3 py-1.5 text-xs text-slate-400 border-b border-slate-700/50">
                        <span>${pinIcon}${type}</span>
                        <span>—</span>
                        <span>${escapeHtml(m.author || '')}</span>
                        <span>—</span>
                        <span>${time}</span>
                        ${m.title ? `<span class="text-slate-300 ml-1">${escapeHtml(m.title)}</span>` : ''}
                    </div>
                    <div id="${collapseId}" class="px-3 py-2 prose-dark text-sm ${isLong && !uiState.expandedMessages.has(collapseId) ? 'msg-collapsed' : ''}">
                        ${contentHtml}
                    </div>
                    ${isLong ? `<button onclick="window._toggleMsg('${collapseId}')" class="px-3 py-1 text-xs text-slate-400 hover:text-slate-200">${uiState.expandedMessages.has(collapseId) ? 'Collapse ▴' : 'Expand ▾'}</button>` : ''}
                </div>
            `;
        }).join('');
    } catch (e) {
        document.getElementById('conversation-header').innerHTML = `<p class="text-red-400 p-4">Error: ${e.message}</p>`;
    }
}

// ── Gate Pipeline Visualization ──────────────────────────────────────────
function renderGateSection(task) {
    const el = document.getElementById('detail-gates');
    if (!el) return;

    // Only show if task has gate info
    if (!task.auto_test && !task.auto_review) {
        el.innerHTML = '';
        return;
    }

    const stages = [];
    stages.push({ label: 'Task', status: task.status === 'completed' ? 'done' : task.status === 'working' ? 'active' : 'pending' });

    if (task.auto_test) {
        const gs = task.gate_status;
        let s = 'pending';
        if (gs === 'testing') s = 'active';
        else if (gs === 'test-failed') s = 'failed';
        else if (['reviewing', 'review-failed', 'passed'].includes(gs)) s = 'done';
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

    const stageColors = {
        done: 'bg-emerald-500 text-white',
        active: 'bg-blue-500 text-white gate-pulse',
        failed: 'bg-red-500 text-white',
        pending: 'bg-slate-700 text-slate-400',
    };
    const stageIcons = { done: '✓', active: '●', failed: '✕', pending: '○' };

    const retries = task.gate_retries > 0 ? `<span class="text-xs text-slate-400 mt-1">Retries: ${task.gate_retries}/${task.max_gate_retries || 3}</span>` : '';

    el.innerHTML = `
        <div class="bg-slate-900 border border-slate-800 rounded-lg p-4 mb-4">
            <div class="flex items-center gap-2 overflow-x-auto">
                ${stages.map((st, i) => `
                    <div class="flex items-center gap-2 shrink-0">
                        <div class="flex flex-col items-center">
                            <div class="w-8 h-8 rounded-full flex items-center justify-center text-sm ${stageColors[st.status]}">
                                ${stageIcons[st.status]}
                            </div>
                            <span class="text-xs text-slate-400 mt-1">${st.label}</span>
                        </div>
                        ${i < stages.length - 1 ? '<div class="w-8 h-px bg-slate-600"></div>' : ''}
                    </div>
                `).join('')}
            </div>
            ${retries}
        </div>
    `;
}

// ── Chain Visualization ──────────────────────────────────────────────────
async function renderChainSection(taskId) {
    const el = document.getElementById('detail-chain');
    if (!el) return;

    try {
        const data = await api.getChain(taskId);
        if (!data || !data.chain || data.chain.length <= 1) {
            el.innerHTML = '';
            return;
        }

        const chain = data.chain;
        const currentIdx = data.current_index;

        el.innerHTML = `
            <div class="bg-slate-900 border border-slate-800 rounded-lg p-4 mb-4">
                <h3 class="text-sm font-medium text-slate-300 mb-3">⛓ Task Chain</h3>
                <div class="flex items-center gap-2 overflow-x-auto pb-2">
                    ${chain.map((t, i) => {
                        if (t.parent_task_id) return '';  // Skip review sub-tasks in chain viz
                        const isCurrent = i === currentIdx;
                        const border = isCurrent ? 'border-blue-500 ring-1 ring-blue-500/50' : 'border-slate-700';
                        const shortId = t.id.split('/').pop();
                        return `
                            <a href="#/tasks/${t.id}" class="shrink-0 block p-2 rounded border ${border} bg-slate-800/50 hover:bg-slate-800 min-w-[120px] max-w-[180px]">
                                <div class="flex items-center gap-1 mb-1">
                                    ${statusBadge(t.status)}
                                    ${gateBadge(t)}
                                </div>
                                <div class="text-xs font-mono text-slate-300 truncate">${escapeHtml(shortId)}</div>
                                <div class="text-xs text-slate-500 truncate">${escapeHtml(t.goal || '').slice(0, 40)}</div>
                            </a>
                            ${i < chain.length - 1 && !chain[i+1]?.parent_task_id ? '<span class="text-slate-600 shrink-0">→</span>' : ''}
                        `;
                    }).join('')}
                </div>
            </div>
        `;
    } catch (e) {
        el.innerHTML = '';
    }
}

// ── Review Sub-task ──────────────────────────────────────────────────────
async function renderReviewSubTask(taskId) {
    const el = document.getElementById('detail-review-task');
    if (!el) return;

    try {
        const review = await api.getReviewTask(taskId);
        if (!review) {
            el.innerHTML = '';
            return;
        }

        const reviewContent = review.review_message
            ? sanitize(marked.parse(review.review_message.content || ''))
            : '<p class="text-slate-500 text-sm">Review in progress...</p>';

        el.innerHTML = `
            <details class="bg-slate-900 border border-slate-800 rounded-lg mb-4">
                <summary class="px-4 py-3 text-sm cursor-pointer hover:bg-slate-800/50 flex items-center gap-2">
                    <span class="px-2 py-0.5 rounded text-xs font-medium bg-pink-500/20 text-pink-400">REVIEW</span>
                    ${statusBadge(review.status)}
                    <span class="text-slate-400 text-xs">${escapeHtml(review.model || 'opus')}</span>
                    <span class="text-slate-500 text-xs">${relativeTime(review.updated_at)}</span>
                    <a href="#/tasks/${review.id}" onclick="event.stopPropagation()" class="text-xs text-blue-400 hover:text-blue-300 ml-auto">View full →</a>
                </summary>
                <div class="px-4 pb-3 prose-dark text-sm border-t border-slate-700/50">
                    ${reviewContent}
                </div>
            </details>
        `;
    } catch (e) {
        el.innerHTML = '';
    }
}

// ── Init ─────────────────────────────────────────────────────────────────
startRouter();
