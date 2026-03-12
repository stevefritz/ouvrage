import { api } from './api.js';

// ── Sanitization guard ──────────────────────────────────────────────────
const sanitize = typeof DOMPurify !== 'undefined'
    ? (html) => DOMPurify.sanitize(html)
    : (html) => html;

// ── State ────────────────────────────────────────────────────────────────
let pollTimer = null;
let currentView = null;

// Track UI toggle states across poll re-renders
const uiState = {
    expandedMessages: new Set(),   // IDs of expanded long messages
    sessionLogOpen: false,
    dispatchLogOpen: false,
    sessionLogLoaded: false,
    dispatchLogLoaded: false,
};

// ── Router ───────────────────────────────────────────────────────────────
function getRoute() {
    const hash = location.hash.slice(1) || '/';
    if (hash.startsWith('/tasks/')) return { view: 'detail', taskId: hash.slice(7) };
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

    if (r.view === 'board') showBoard(r.params);
    else if (r.view === 'detail') showDetail(r.taskId);
    else if (r.view === 'projects') showProjects();

    // Update nav active state
    document.querySelectorAll('[data-nav]').forEach(el => {
        el.classList.toggle('text-slate-100', el.dataset.nav === r.view);
        el.classList.toggle('text-slate-400', el.dataset.nav !== r.view);
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
        cancelled:    { bg: 'bg-slate-500/20', text: 'text-slate-400', icon: '—' },
        ready:        { bg: 'bg-slate-500/20', text: 'text-slate-300', icon: '○' },
    };
    const s = map[status] || map.ready;
    const dotClass = s.dot ? 'status-dot-working' : '';
    return `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-medium ${s.bg} ${s.text}">
        <span class="${dotClass}">${s.icon}</span> ${status.toUpperCase()}
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
        btns.push(`<button onclick="window._action('retry','${task.id}')" class="px-2 py-1 text-xs rounded bg-amber-500/20 text-amber-400 hover:bg-amber-500/30">Retry</button>`);
        btns.push(`<button onclick="window._action('close','${task.id}')" class="px-2 py-1 text-xs rounded bg-slate-500/20 text-slate-400 hover:bg-slate-500/30">Close</button>`);
    }
    if (task.status === 'needs-review') {
        btns.push(`<button onclick="window._action('resume','${task.id}')" class="px-2 py-1 text-xs rounded bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30">Resume</button>`);
        btns.push(`<button onclick="window._action('retry','${task.id}')" class="px-2 py-1 text-xs rounded bg-amber-500/20 text-amber-400 hover:bg-amber-500/30">Retry</button>`);
        btns.push(`<button onclick="window._action('cancel','${task.id}')" class="px-2 py-1 text-xs rounded bg-red-500/20 text-red-400 hover:bg-red-500/30">Cancel</button>`);
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

function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// ── Actions ──────────────────────────────────────────────────────────────
window._action = async (action, taskId) => {
    const labels = { cancel: 'Cancel', retry: 'Retry', resume: 'Resume', close: 'Close' };
    const msg = action === 'close'
        ? `Close task "${taskId}"? This will clean up the worktree and branch.`
        : `${labels[action]} task "${taskId}"?`;
    if (!confirm(msg)) return;
    try {
        if (action === 'cancel') await api.cancelTask(taskId);
        else if (action === 'retry') await api.retryTask(taskId);
        else if (action === 'resume') await api.resumeTask(taskId);
        else if (action === 'close') await api.closeTask(taskId);
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
                    <td class="p-3">${statusBadge(t.status)}</td>
                    <td class="p-3">
                        <div class="flex items-center gap-2">
                            <span class="font-mono text-sm text-slate-200">${escapeHtml(t.id)}</span>
                            ${t.pr_url ? `<a href="${escapeHtml(t.pr_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs bg-purple-500/20 text-purple-400 hover:bg-purple-500/30" title="View PR">PR</a>` : ''}
                        </div>
                        <div class="text-sm text-slate-400 truncate max-w-md">${escapeHtml(t.goal)}</div>
                        ${t.phase ? `<div class="text-xs text-slate-500 mt-0.5">${escapeHtml(t.phase)}</div>` : ''}
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
            renderChecklist(task);
            renderMessages(task);
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

    // Poll — refresh header/checklist/messages but not log panels or message input
    const task = await api.getTask(taskId).catch(() => null);
    if (task && (task.status === 'working' || task.status === 'needs-review')) {
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
                    <span class="font-mono text-lg text-slate-200">${escapeHtml(task.id)}</span>
                </div>
                <p class="text-slate-300 mb-3">${escapeHtml(task.goal)}</p>
                <div class="flex flex-wrap gap-x-6 gap-y-1 text-sm text-slate-400">
                    <span>Branch: <span class="font-mono text-slate-300">${escapeHtml(task.branch || '—')}</span></span>
                    <span>Dispatches: <span class="text-slate-300">${task.dispatch_count || 0}</span></span>
                    <span>Cost: <span class="text-slate-300">$${(task.total_cost_usd || 0).toFixed(2)}</span></span>
                    <span>Tokens: <span class="text-slate-300">${((task.total_input_tokens || 0) / 1000).toFixed(0)}K in / ${((task.total_output_tokens || 0) / 1000).toFixed(1)}K out</span></span>
                    ${task.phase ? `<span>Phase: <span class="text-slate-300">${escapeHtml(task.phase)}</span></span>` : ''}
                    ${prUrlBadge(task)}
                </div>
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

function renderMessages(task) {
    const el = document.getElementById('detail-messages');
    const msgs = task.messages || [];
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

function renderSessionLogHtml(entries) {
    if (entries.length === 0) {
        return '<p class="text-slate-500 text-sm p-2">No session log</p>';
    }
    return entries.map(e => {
        const ts = e.timestamp ? new Date(e.timestamp).toLocaleTimeString() : '';
        const type = e.type || '';

        if (type === 'SystemMessage') {
            return `<div class="log-system text-xs py-0.5"><span class="text-slate-600 mr-2">${ts}</span>SYSTEM ${e.subtype || ''}</div>`;
        }
        if (type === 'AssistantMessage') {
            const blocks = e.content || [];
            return blocks.map(b => {
                if (b.type === 'text') {
                    const preview = (b.text || '').slice(0, 120);
                    return `<div class="log-text text-xs py-0.5"><span class="text-slate-600 mr-2">${ts}</span>TEXT  ${escapeHtml(preview)}</div>`;
                }
                if (b.type === 'tool_use') {
                    return `<div class="log-tool text-xs py-0.5"><span class="text-slate-600 mr-2">${ts}</span>TOOL  ${escapeHtml(b.name || '')} → ${escapeHtml((b.input || '').slice(0, 80))}</div>`;
                }
                return '';
            }).join('');
        }
        if (type === 'UserMessage') {
            const blocks = e.content || [];
            return blocks.map(b => {
                if (b.type === 'tool_result') {
                    const status = b.is_error ? '(error)' : `(${(b.preview || '').length}B)`;
                    return `<div class="log-result text-xs py-0.5"><span class="text-slate-600 mr-2">${ts}</span>RESULT ${status}</div>`;
                }
                return '';
            }).join('');
        }
        if (type === 'ResultMessage') {
            const cls = e.is_error ? 'log-error' : 'log-done';
            return `<div class="${cls} text-xs py-0.5 font-medium"><span class="text-slate-600 mr-2">${ts}</span>DONE  ${e.num_turns || '?'} turns | $${(e.cost_usd || 0).toFixed(2)}</div>`;
        }
        return '';
    }).join('');
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

// ── Init ─────────────────────────────────────────────────────────────────
startRouter();
