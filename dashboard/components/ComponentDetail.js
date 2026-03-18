import { useState, useEffect, useRef, useCallback, useMemo } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { html, navigate, StatusBadge, relativeTime } from './utils.js';
import {
    computeLayout, TaskNode, EdgePath, TagFilterBar, StateLegend, DEFAULT_STATE_COLORS,
} from './DagGraph.js';
import { GraphDetailPanel } from './GraphDetailPanel.js';

// ── Component status badge ────────────────────────────────────────────────

const COMP_STATUS = {
    planning:  { bg: 'bg-slate-500/20', text: 'text-slate-400' },
    active:    { bg: 'bg-emerald-500/20', text: 'text-emerald-400' },
    deployed:  { bg: 'bg-blue-500/20', text: 'text-blue-400' },
    archived:  { bg: 'bg-slate-500/10', text: 'text-slate-500' },
};

function CompStatusBadge({ status }) {
    const s = COMP_STATUS[status] || COMP_STATUS.planning;
    return html`<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${s.bg} ${s.text}">
        ${(status || 'planning').toUpperCase()}
    </span>`;
}

// ── Progress bar ──────────────────────────────────────────────────────────

function ProgressBar({ done, total }) {
    const pct = total > 0 ? Math.round(done / total * 100) : 0;
    return html`
        <div class="flex items-center gap-2">
            <div class="flex-1 bg-slate-700 rounded-full h-1.5">
                <div class="bg-emerald-500 h-1.5 rounded-full" style="width: ${pct}%"></div>
            </div>
            <span class="text-xs text-slate-400 tabular-nums">${done}/${total}</span>
        </div>
    `;
}

// ── DAG graph section ─────────────────────────────────────────────────────

function ComponentDagSection({ tasks, onAction, jiraBaseUrl }) {
    const [selectedTaskId, setSelectedTaskId] = useState(null);
    const [hoveredId, setHoveredId] = useState(null);
    const [activeTags, setActiveTags] = useState(new Set());

    const layout = useMemo(() => {
        if (!tasks || tasks.length === 0) return null;
        return computeLayout(tasks, DEFAULT_STATE_COLORS);
    }, [tasks]);

    const allTags = useMemo(() => {
        if (!tasks) return [];
        const s = new Set();
        tasks.forEach(t => (t.tags || []).forEach(tag => s.add(tag)));
        return [...s].sort();
    }, [tasks]);

    const toggleTag = useCallback((tag) => {
        setActiveTags(prev => {
            const next = new Set(prev);
            if (next.has(tag)) next.delete(tag); else next.add(tag);
            return next;
        });
    }, []);

    const clearTags = useCallback(() => setActiveTags(new Set()), []);

    const visibleIds = useMemo(() => {
        if (!layout || activeTags.size === 0) return null;
        const ids = new Set();
        for (const node of layout.nodes) {
            if ((node.task.tags || []).some(t => activeTags.has(t))) ids.add(node.id);
        }
        return ids;
    }, [layout, activeTags]);

    const connectedIds = useMemo(() => {
        if (!hoveredId || !layout) return null;
        const ids = new Set([hoveredId]);
        for (const edge of layout.edges) {
            if (edge.fromId === hoveredId) ids.add(edge.toId);
            if (edge.toId === hoveredId) ids.add(edge.fromId);
        }
        return ids;
    }, [hoveredId, layout]);

    const handleSelect = useCallback((id) => setSelectedTaskId(id), []);
    const handleClose = useCallback(() => setSelectedTaskId(null), []);

    if (!layout || layout.nodes.length === 0) {
        return html`<p class="text-slate-500 text-sm py-4">No tasks in this component yet.</p>`;
    }

    return html`
        <div class="${selectedTaskId ? 'graph-layout graph-layout-split' : 'graph-layout'}"
            style="min-height: 0;">
            <div class="graph-main">
                <div class="dag-container" style="border-radius: 8px;">

                    ${allTags.length > 0 && html`
                        <${TagFilterBar} tags=${allTags} activeTags=${activeTags}
                            onToggleTag=${toggleTag} onClear=${clearTags}
                            componentColors=${layout.componentColors} />
                    `}

                    <div class="dag-scroll" onClick=${() => handleSelect(null)}>
                        <div class="dag-canvas"
                            style="width:${layout.width}px; height:${layout.height}px; position:relative;">

                            <svg class="dag-edges" width=${layout.width} height=${layout.height}
                                style="position:absolute; top:0; left:0; pointer-events:none; z-index:1;">
                                ${layout.edges.map(edge => {
                                    const isHighlighted = connectedIds && (connectedIds.has(edge.fromId) && connectedIds.has(edge.toId));
                                    const isDimmed = (connectedIds && !isHighlighted) ||
                                        (visibleIds && (!visibleIds.has(edge.fromId) || !visibleIds.has(edge.toId)));
                                    return html`<${EdgePath} key=${edge.fromId + '-' + edge.toId}
                                        edge=${edge} highlighted=${isHighlighted} dimmed=${isDimmed} />`;
                                })}
                            </svg>

                            ${layout.nodes.map(node => {
                                const isSelected = selectedTaskId === node.id;
                                const isHovered = hoveredId === node.id;
                                const isDimmed = (connectedIds && !connectedIds.has(node.id)) ||
                                    (visibleIds && !visibleIds.has(node.id));
                                return html`<${TaskNode} key=${node.id} node=${node}
                                    selected=${isSelected} hovered=${isHovered} dimmed=${isDimmed}
                                    onSelect=${handleSelect}
                                    onHover=${setHoveredId}
                                    onUnhover=${() => setHoveredId(null)}
                                    allNodes=${layout.nodes} />`;
                            })}
                        </div>
                    </div>

                    <${StateLegend} tasks=${tasks.filter(t => !t.parent_task_id)} stateColors=${DEFAULT_STATE_COLORS} />
                </div>
            </div>

            ${selectedTaskId ? html`
                <div class="panel-backdrop" onClick=${handleClose}></div>
                <${GraphDetailPanel}
                    key=${selectedTaskId}
                    taskId=${selectedTaskId}
                    allTasks=${tasks}
                    jiraBaseUrl=${jiraBaseUrl}
                    onClose=${handleClose}
                    onAction=${onAction} />
            ` : null}
        </div>
    `;
}

// ── Task list (grouped by state) ──────────────────────────────────────────

function TaskGroup({ title, tasks, colorClass }) {
    if (!tasks.length) return null;
    return html`
        <div class="mb-4">
            <h4 class="text-xs font-medium uppercase tracking-wide mb-2 ${colorClass}">${title} (${tasks.length})</h4>
            <div class="bg-slate-900 border border-slate-700 rounded-lg divide-y divide-slate-800">
                ${tasks.map(t => html`
                    <div key=${t.id} class="px-4 py-3 flex items-center gap-3 hover:bg-slate-800/50 cursor-pointer"
                        onClick=${() => navigate(`#/tasks/${encodeURIComponent(t.id)}`)}>
                        <${StatusBadge} status=${t.status} />
                        <span class="flex-1 text-sm text-slate-300 truncate" title=${t.goal}>${t.goal}</span>
                        <div class="flex items-center gap-3 text-xs text-slate-500 shrink-0">
                            ${t.model && html`<span class="font-mono">${t.model}</span>`}
                            ${t.total_cost_usd > 0 && html`<span>$${t.total_cost_usd.toFixed(2)}</span>`}
                        </div>
                    </div>
                `)}
            </div>
        </div>
    `;
}

function TaskArea({ tasks }) {
    if (!tasks || tasks.length === 0) {
        return html`<p class="text-slate-500 text-sm py-4">No tasks in this component yet.</p>`;
    }
    const working = tasks.filter(t => t.status === 'working');
    const active = tasks.filter(t => ['ready', 'blocked', 'needs-review', 'turns-exhausted', 'failed'].includes(t.status));
    const done = tasks.filter(t => t.status === 'completed');
    const other = tasks.filter(t => t.status === 'cancelled');
    return html`
        <${TaskGroup} title="Working" tasks=${working} colorClass="text-emerald-400" />
        <${TaskGroup} title="Active / Blocked" tasks=${active} colorClass="text-amber-400" />
        <${TaskGroup} title="Completed" tasks=${done} colorClass="text-blue-400" />
        <${TaskGroup} title="Cancelled" tasks=${other} colorClass="text-slate-500" />
    `;
}

// ── Punchlist ─────────────────────────────────────────────────────────────

const PUNCHLIST_STATUS = {
    open:    { icon: '\u25A1', colorClass: 'text-slate-400', label: 'open' },
    claimed: { icon: '\u25A0', colorClass: 'text-amber-400', label: 'claimed' },
    done:    { icon: '\u2713', colorClass: 'text-emerald-400', label: 'done' },
};

function PunchlistItem({ item, componentId, onUpdated, onDispatched }) {
    const s = PUNCHLIST_STATUS[item.status] || PUNCHLIST_STATUS.open;
    const cycleStatus = async () => {
        const next = item.status === 'open' ? 'claimed' : item.status === 'claimed' ? 'done' : 'open';
        try {
            const updated = await api.updatePunchlistItem(componentId, item.id, { status: next });
            onUpdated(updated);
        } catch (e) {
            alert(`Error: ${e.message}`);
        }
    };
    const dispatch = async () => {
        if (!confirm(`Dispatch "${item.item}" as a new task?`)) return;
        try {
            const result = await api.dispatchPunchlistItem(componentId, item.id);
            onDispatched(item.id, result.task_id);
        } catch (e) {
            alert(`Error: ${e.message}`);
        }
    };

    return html`
        <div class="flex items-start gap-3 py-2.5 border-b border-slate-800 last:border-0">
            <button onClick=${cycleStatus}
                class="mt-0.5 text-base ${s.colorClass} hover:scale-110 transition-transform shrink-0">
                ${s.icon}
            </button>
            <span class="flex-1 text-sm text-slate-300 ${item.status === 'done' ? 'line-through text-slate-500' : ''}">
                ${item.item}
            </span>
            <div class="flex items-center gap-2 shrink-0 text-xs text-slate-500">
                ${item.claimed_by && html`
                    <a href=${`#/tasks/${encodeURIComponent(item.claimed_by)}`}
                        class="text-amber-400/70 hover:text-amber-400 font-mono truncate max-w-24"
                        title=${item.claimed_by}
                        onClick=${e => e.stopPropagation()}>
                        ${item.claimed_by.split('/').pop()}
                    </a>
                `}
                ${item.status === 'open' && html`
                    <button onClick=${dispatch}
                        class="px-2 py-0.5 rounded bg-slate-700 hover:bg-slate-600 text-slate-300">
                        dispatch
                    </button>
                `}
            </div>
        </div>
    `;
}

function PunchlistSection({ componentId }) {
    const [items, setItems] = useState(null);
    const [open, setOpen] = useState(true);
    const [newItem, setNewItem] = useState('');
    const [adding, setAdding] = useState(false);
    const inputRef = useRef(null);

    useEffect(() => {
        api.getPunchlist(componentId)
            .then(setItems)
            .catch(e => console.warn('Punchlist error:', e));
    }, [componentId]);

    const addItem = async (e) => {
        e.preventDefault();
        if (!newItem.trim()) return;
        setAdding(true);
        try {
            const item = await api.addPunchlistItem(componentId, newItem.trim());
            setItems(prev => [...(prev || []), item]);
            setNewItem('');
        } catch (e) {
            alert(`Error: ${e.message}`);
        } finally {
            setAdding(false);
        }
    };

    const onUpdated = (updated) => {
        setItems(prev => prev.map(i => i.id === updated.id ? updated : i));
    };

    const onDispatched = (itemId, taskId) => {
        setItems(prev => prev.map(i => i.id === itemId ? { ...i, status: 'claimed', claimed_by: taskId } : i));
    };

    const openCount = (items || []).filter(i => i.status !== 'done').length;

    return html`
        <div class="border border-slate-700 rounded-lg overflow-hidden">
            <button class="w-full flex items-center justify-between px-4 py-3 bg-slate-900 hover:bg-slate-800/50 text-left"
                onClick=${() => setOpen(o => !o)}>
                <span class="text-sm font-medium text-slate-300">
                    Punchlist
                    ${openCount > 0 && html`<span class="ml-2 px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400 text-xs">${openCount} open</span>`}
                </span>
                <span class="text-slate-500 text-xs">${open ? '\u25B2' : '\u25BC'}</span>
            </button>
            ${open && html`
                <div class="bg-slate-900/50 border-t border-slate-800 px-4 pt-3 pb-4">
                    <!-- Quick-add -->
                    <form onSubmit=${addItem} class="flex gap-2 mb-3">
                        <input ref=${inputRef}
                            type="text"
                            value=${newItem}
                            onInput=${e => setNewItem(e.target.value)}
                            placeholder="Add punchlist item..."
                            class="flex-1 bg-slate-800 border border-slate-600 rounded px-3 py-1.5 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-slate-400"
                        />
                        <button type="submit" disabled=${adding || !newItem.trim()}
                            class="px-3 py-1.5 text-sm rounded bg-emerald-600 text-white hover:bg-emerald-500 disabled:opacity-40 disabled:cursor-not-allowed">
                            Add
                        </button>
                    </form>
                    <!-- Items -->
                    ${items === null
                        ? html`<p class="text-slate-500 text-sm">Loading...</p>`
                        : items.length === 0
                        ? html`<p class="text-slate-500 text-sm">No items yet.</p>`
                        : items.map(item => html`
                            <${PunchlistItem}
                                key=${item.id}
                                item=${item}
                                componentId=${componentId}
                                onUpdated=${onUpdated}
                                onDispatched=${onDispatched}
                            />
                        `)
                    }
                </div>
            `}
        </div>
    `;
}

// ── Activity timeline ─────────────────────────────────────────────────────

const EVENT_ICONS = {
    result:   { icon: '\u2713', bg: 'bg-blue-500/20', text: 'text-blue-400' },
    question: { icon: '\u003F', bg: 'bg-amber-500/20', text: 'text-amber-400' },
    handoff:  { icon: '\u27A1', bg: 'bg-purple-500/20', text: 'text-purple-400' },
    plan:     { icon: '\u2318', bg: 'bg-indigo-500/20', text: 'text-indigo-400' },
    note:     { icon: '\u2022', bg: 'bg-slate-500/20', text: 'text-slate-400' },
    progress: { icon: '\u25B6', bg: 'bg-slate-500/20', text: 'text-slate-400' },
};

function ActivityTimeline({ componentId }) {
    const [events, setEvents] = useState(null);

    useEffect(() => {
        api.getComponentActivity(componentId)
            .then(setEvents)
            .catch(e => console.warn('Activity error:', e));
    }, [componentId]);

    if (events === null) return html`<p class="text-slate-500 text-sm">Loading...</p>`;
    if (events.length === 0) return html`<p class="text-slate-500 text-sm">No activity yet.</p>`;

    return html`
        <div class="space-y-2">
            ${events.map(ev => {
                const s = EVENT_ICONS[ev.type] || EVENT_ICONS.note;
                const taskSlug = ev.task_id ? ev.task_id.split('/').pop() : '';
                return html`
                    <div key=${ev.id} class="flex gap-3 items-start">
                        <div class="shrink-0 mt-0.5 w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold ${s.bg} ${s.text}">
                            ${s.icon}
                        </div>
                        <div class="flex-1 min-w-0">
                            <div class="flex items-baseline gap-2 mb-0.5">
                                <span class="text-xs font-medium uppercase tracking-wide ${s.text}">${ev.type}</span>
                                ${ev.task_id && html`
                                    <a href=${`#/tasks/${encodeURIComponent(ev.task_id)}`}
                                        class="text-xs text-slate-500 hover:text-slate-300 font-mono truncate"
                                        title=${ev.task_id}>
                                        ${taskSlug}
                                    </a>
                                `}
                                <span class="text-xs text-slate-600 ml-auto shrink-0">${relativeTime(ev.created_at)}</span>
                            </div>
                            ${ev.title && html`<p class="text-sm font-medium text-slate-300 mb-0.5">${ev.title}</p>`}
                            <p class="text-sm text-slate-400 line-clamp-3">${ev.summary}</p>
                        </div>
                    </div>
                `;
            })}
        </div>
    `;
}

// ── Main component ────────────────────────────────────────────────────────

export function ComponentDetail({ componentId, jiraBaseUrl, onAction }) {
    const [comp, setComp] = useState(null);
    const [error, setError] = useState(null);
    const [taskView, setTaskView] = useState('dag'); // 'dag' | 'list'

    const loadComponent = useCallback(() => {
        api.getComponent(componentId)
            .then(setComp)
            .catch(e => setError(e.message));
    }, [componentId]);

    useEffect(() => {
        loadComponent();
        const timer = setInterval(loadComponent, 5000);
        return () => clearInterval(timer);
    }, [loadComponent]);

    if (error) {
        return html`<div class="p-6"><p class="text-red-400">Error: ${error}</p></div>`;
    }
    if (!comp) {
        return html`<div class="p-6"><p class="text-slate-500">Loading...</p></div>`;
    }

    const projectId = comp.project_id;

    return html`
        <div class="p-6">
            <!-- Breadcrumb -->
            <div class="flex items-center gap-2 text-sm text-slate-500 mb-4">
                <a href="#/projects" class="hover:text-slate-300">Projects</a>
                <span>/</span>
                <a href=${`#/projects/${encodeURIComponent(projectId)}`} class="hover:text-slate-300">${projectId}</a>
                <span>/</span>
                <span class="text-slate-300">${comp.name}</span>
            </div>

            <!-- Component header -->
            <div class="bg-slate-900 border border-slate-700 rounded-lg p-5 mb-6">
                <div class="flex items-start justify-between mb-3">
                    <div>
                        <div class="flex items-center gap-3 mb-1">
                            <h2 class="text-xl font-semibold text-slate-100">${comp.name}</h2>
                            <${CompStatusBadge} status=${comp.status} />
                        </div>
                        ${comp.description && html`<p class="text-slate-400 text-sm">${comp.description}</p>`}
                    </div>
                </div>
                <div class="flex flex-wrap gap-x-6 gap-y-2 text-sm text-slate-400">
                    <span>Branch: <span class="font-mono text-slate-300">${comp.base_branch || '\u2014'}</span></span>
                    <span>$${(comp.total_cost || 0).toFixed(2)}</span>
                    ${comp.active_tasks > 0 && html`
                        <span class="text-emerald-400 flex items-center gap-1">
                            <span class="status-dot-working">\u25CF</span>
                            ${comp.active_tasks} active
                        </span>
                    `}
                </div>
                <div class="mt-3">
                    <div class="flex items-center gap-2 text-sm text-slate-400 mb-1">
                        <span>Progress</span>
                    </div>
                    <div class="flex items-center gap-3">
                        <div class="flex-1 bg-slate-700 rounded-full h-2">
                            <div class="bg-emerald-500 h-2 rounded-full"
                                style="width: ${comp.total_tasks > 0 ? Math.round(comp.done_tasks / comp.total_tasks * 100) : 0}%">
                            </div>
                        </div>
                        <span class="text-sm text-slate-400 tabular-nums">${comp.done_tasks}/${comp.total_tasks} tasks</span>
                    </div>
                </div>
            </div>

            <!-- Tasks -->
            <section class="mb-6">
                <div class="flex items-center justify-between mb-3">
                    <h3 class="text-sm font-semibold text-slate-400 uppercase tracking-wide">Tasks</h3>
                    <div class="flex gap-1">
                        <button onClick=${() => setTaskView('dag')}
                            class="px-2 py-1 text-xs rounded transition-colors ${taskView === 'dag'
                                ? 'bg-blue-600 text-white'
                                : 'bg-slate-800 text-slate-400 hover:bg-slate-700'}">
                            Graph
                        </button>
                        <button onClick=${() => setTaskView('list')}
                            class="px-2 py-1 text-xs rounded transition-colors ${taskView === 'list'
                                ? 'bg-blue-600 text-white'
                                : 'bg-slate-800 text-slate-400 hover:bg-slate-700'}">
                            List
                        </button>
                    </div>
                </div>
                ${taskView === 'dag'
                    ? html`<${ComponentDagSection} tasks=${comp.tasks} onAction=${onAction} jiraBaseUrl=${jiraBaseUrl} />`
                    : html`<${TaskArea} tasks=${comp.tasks} />`
                }
            </section>

            <!-- Punchlist + Activity side-by-side on wide screens -->
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
                <section>
                    <${PunchlistSection} componentId=${componentId} />
                </section>

                <section>
                    <div class="border border-slate-700 rounded-lg overflow-hidden">
                        <div class="px-4 py-3 bg-slate-900 border-b border-slate-800">
                            <span class="text-sm font-medium text-slate-300">Activity Timeline</span>
                        </div>
                        <div class="bg-slate-900/50 px-4 py-4">
                            <${ActivityTimeline} componentId=${componentId} />
                        </div>
                    </div>
                </section>
            </div>

            <!-- Linked conversations -->
            ${comp.conversations && comp.conversations.length > 0 && html`
                <section>
                    <h3 class="text-sm font-semibold text-slate-400 uppercase tracking-wide mb-3">Linked Conversations</h3>
                    <div class="bg-slate-900 border border-slate-700 rounded-lg divide-y divide-slate-800">
                        ${comp.conversations.map(conv => html`
                            <div key=${conv.id} class="px-4 py-3 flex items-center gap-3 hover:bg-slate-800/50 cursor-pointer"
                                onClick=${() => navigate(`#/conversations/${encodeURIComponent(conv.id)}`)}>
                                <span class="text-sm text-slate-300 flex-1">${conv.goal || conv.id}</span>
                                <span class="text-xs text-slate-500 font-mono">${conv.id}</span>
                            </div>
                        `)}
                    </div>
                </section>
            `}
        </div>
    `;
}
