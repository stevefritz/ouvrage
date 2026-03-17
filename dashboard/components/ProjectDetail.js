import { useState, useEffect, useCallback, useMemo, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { html, navigate } from './utils.js';
import {
    computeLayout, TaskNode, EdgePath, TagFilterBar, StateLegend,
    DEFAULT_STATE_COLORS, NODE_W, NODE_H,
} from './DagGraph.js';
import { GraphDetailPanel } from './GraphDetailPanel.js';

const COMPONENT_STATUS_COLORS = {
    planning:  { bg: 'bg-slate-500/20', text: 'text-slate-400', dot: '\u25CB' },
    active:    { bg: 'bg-emerald-500/20', text: 'text-emerald-400', dot: '\u25CF' },
    deployed:  { bg: 'bg-blue-500/20', text: 'text-blue-400', dot: '\u2713' },
    archived:  { bg: 'bg-slate-500/10', text: 'text-slate-500', dot: '\u2014' },
};

function ComponentStatusBadge({ status }) {
    const s = COMPONENT_STATUS_COLORS[status] || COMPONENT_STATUS_COLORS.planning;
    return html`<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-medium ${s.bg} ${s.text}">
        ${s.dot} ${(status || 'planning').toUpperCase()}
    </span>`;
}

function ProgressBar({ done, total }) {
    const pct = total > 0 ? Math.round(done / total * 100) : 0;
    return html`
        <div class="flex items-center gap-2">
            <div class="flex-1 bg-slate-700 rounded-full h-1.5 overflow-hidden">
                <div class="bg-emerald-500 h-1.5 rounded-full transition-all" style="width: ${pct}%"></div>
            </div>
            <span class="text-xs text-slate-400 tabular-nums">${done}/${total}</span>
        </div>
    `;
}

function ComponentCard({ comp }) {
    const hasActive = comp.active_tasks > 0;
    return html`
        <div class="bg-slate-900 border border-slate-700 rounded-lg p-4 hover:border-slate-500 cursor-pointer transition-colors"
            onClick=${() => navigate(`#/components/${encodeURIComponent(comp.id)}`)}>
            <div class="flex items-start justify-between mb-2">
                <h3 class="text-base font-medium text-slate-200">${comp.name}</h3>
                <${ComponentStatusBadge} status=${comp.status} />
            </div>
            ${comp.description && html`
                <p class="text-sm text-slate-400 mb-3 line-clamp-2">${comp.description}</p>
            `}
            <div class="text-xs font-mono text-slate-500 mb-3">${comp.base_branch || '\u2014'}</div>
            <${ProgressBar} done=${comp.done_tasks} total=${comp.total_tasks} />
            <div class="flex flex-wrap gap-3 mt-3 text-xs text-slate-400">
                <span class=${hasActive ? 'text-emerald-400 flex items-center gap-1' : ''}>
                    ${hasActive && html`<span class="status-dot-working">\u25CF</span>`}
                    ${comp.active_tasks} active
                </span>
                <span>$${(comp.total_cost || 0).toFixed(2)}</span>
                ${comp.conversation_count > 0 && html`
                    <span>${comp.conversation_count} conv${comp.conversation_count !== 1 ? 's' : ''}</span>
                `}
                ${comp.open_punchlist > 0 && html`
                    <span class="text-amber-400">${comp.open_punchlist} punchlist</span>
                `}
            </div>
        </div>
    `;
}

// ── Stub node — represents a task from another component ─────
function StubNode({ node, onHover, onUnhover, dimmed }) {
    const { task, x, y } = node;
    const shortId = task.id.includes('/') ? task.id.split('/').pop() : task.id;
    const compLabel = task.component_id
        ? (task.component_id.includes('/') ? task.component_id.split('/').pop() : task.component_id)
        : 'component';
    const opacity = dimmed ? 0.05 : 0.35;

    return html`
        <div style="position:absolute; left:${x}px; top:${y}px; width:${NODE_W}px; height:${NODE_H}px;
                    border-radius: 8px; border: 2px dashed #475569;
                    border-left: 4px dashed #475569;
                    background: #0f172a; padding: 10px 12px;
                    display: flex; flex-direction: column;
                    opacity: ${opacity}; z-index: 2; cursor: default;
                    transition: opacity 0.2s;"
            onMouseEnter=${() => onHover(node.id)}
            onMouseLeave=${onUnhover}>
            <div class="flex items-center gap-2 mb-1">
                <span class="text-xs font-medium text-slate-600">EXTERNAL</span>
            </div>
            <div class="text-sm font-mono text-slate-500 truncate mb-0.5" title=${task.id}>${shortId}</div>
            <div class="text-xs text-slate-600 truncate mb-1">in ${compLabel}</div>
            <div class="text-xs text-slate-600">${task.status || 'unknown'}</div>
        </div>
    `;
}

// ── Ungrouped DAG section ────────────────────────────────────
function UngroupedDagSection({ projectId, jiraBaseUrl, onAction }) {
    const [allTasks, setAllTasks] = useState(null);
    const [collapsed, setCollapsed] = useState(false);
    const [selectedTaskId, setSelectedTaskId] = useState(null);
    const [hoveredId, setHoveredId] = useState(null);
    const [activeTags, setActiveTags] = useState(new Set());
    const mountedRef = useRef(true);

    // Fetch all project tasks with polling
    const loadTasks = useCallback(async () => {
        try {
            const data = await api.getTasks({ project_id: projectId });
            if (mountedRef.current) setAllTasks(data);
        } catch (e) { /* silently ignore */ }
    }, [projectId]);

    useEffect(() => {
        mountedRef.current = true;
        loadTasks();
        const timer = setInterval(loadTasks, 5000);
        return () => { mountedRef.current = false; clearInterval(timer); };
    }, [loadTasks]);

    // Derive ungrouped tasks and layout tasks (ungrouped + cross-component stubs)
    const { ungroupedTasks, layoutTasks } = useMemo(() => {
        if (!allTasks) return { ungroupedTasks: [], layoutTasks: [] };

        const mainUngrouped = allTasks.filter(t => !t.component_id && !t.parent_task_id);
        const ungroupedIds = new Set(mainUngrouped.map(t => t.id));
        const allTaskMap = new Map(allTasks.map(t => [t.id, t]));

        const stubIds = new Set();

        // Upstream stubs: ungrouped task depends on a task in a component
        for (const t of mainUngrouped) {
            if (t.depends_on && !ungroupedIds.has(t.depends_on) && allTaskMap.has(t.depends_on)) {
                const dep = allTaskMap.get(t.depends_on);
                if (dep.component_id && !dep.parent_task_id) stubIds.add(t.depends_on);
            }
        }

        // Downstream stubs: component task depends on an ungrouped task
        for (const t of allTasks) {
            if (t.component_id && !t.parent_task_id && t.depends_on && ungroupedIds.has(t.depends_on)) {
                stubIds.add(t.id);
            }
        }

        const stubs = [...stubIds].map(id => ({ ...allTaskMap.get(id), _isStub: true }));

        return { ungroupedTasks: mainUngrouped, layoutTasks: [...mainUngrouped, ...stubs] };
    }, [allTasks]);

    const layout = useMemo(() => {
        if (!layoutTasks.length) return null;
        return computeLayout(layoutTasks, DEFAULT_STATE_COLORS);
    }, [layoutTasks]);

    // All unique tags from ungrouped tasks
    const allTags = useMemo(() => {
        const s = new Set();
        ungroupedTasks.forEach(t => (t.tags || []).forEach(tag => s.add(tag)));
        return [...s].sort();
    }, [ungroupedTasks]);

    const toggleTag = useCallback((tag) => {
        setActiveTags(prev => {
            const next = new Set(prev);
            if (next.has(tag)) next.delete(tag);
            else next.add(tag);
            return next;
        });
    }, []);

    const clearTags = useCallback(() => setActiveTags(new Set()), []);

    // Visible IDs after tag filtering — stubs always visible
    const visibleIds = useMemo(() => {
        if (!layout || activeTags.size === 0) return null;
        const ids = new Set();
        for (const node of layout.nodes) {
            if (node.task._isStub) { ids.add(node.id); continue; }
            const taskTags = node.task.tags || [];
            if (taskTags.some(t => activeTags.has(t))) ids.add(node.id);
        }
        return ids;
    }, [layout, activeTags]);

    // Connected nodes for hover highlighting
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

    if (!allTasks || ungroupedTasks.length === 0) return null;

    return html`
        <div class="mt-6">
            <!-- Collapsible header -->
            <button class="w-full flex items-center gap-2 px-4 py-3 bg-slate-800/50 border border-slate-700 rounded-t-lg hover:bg-slate-800 text-left"
                onClick=${() => setCollapsed(!collapsed)}>
                <span class="text-sm font-medium text-slate-300">Ungrouped</span>
                <span class="px-1.5 py-0.5 rounded bg-slate-700 text-slate-300 text-xs">${ungroupedTasks.length} tasks</span>
                <span class="ml-auto text-slate-500 text-xs">${collapsed ? '▶' : '▼'}</span>
            </button>

            ${!collapsed && layout && html`
                <div class="border border-t-0 border-slate-700 rounded-b-lg overflow-hidden">
                    <div class="${selectedTaskId ? 'graph-layout graph-layout-split' : 'graph-layout'}"
                        style="min-height: 0;">
                        <div class="graph-main">
                            <div class="dag-container" style="border-radius: 0; border: none;">

                                ${allTags.length > 0 && html`
                                    <${TagFilterBar} tags=${allTags} activeTags=${activeTags}
                                        onToggleTag=${toggleTag} onClear=${clearTags} />
                                `}

                                <div class="dag-scroll" onClick=${() => handleSelect(null)}>
                                    <div class="dag-canvas"
                                        style="width:${layout.width}px; height:${layout.height}px; position:relative;">

                                        <!-- SVG edge layer -->
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

                                        <!-- Node layer -->
                                        ${layout.nodes.map(node => {
                                            const isDimmed = (connectedIds && !connectedIds.has(node.id)) ||
                                                (visibleIds && !visibleIds.has(node.id));

                                            if (node.task._isStub) {
                                                return html`<${StubNode} key=${node.id} node=${node}
                                                    onHover=${setHoveredId}
                                                    onUnhover=${() => setHoveredId(null)}
                                                    dimmed=${isDimmed} />`;
                                            }

                                            const isSelected = selectedTaskId === node.id;
                                            const isHovered = hoveredId === node.id;
                                            return html`<${TaskNode} key=${node.id} node=${node}
                                                selected=${isSelected} hovered=${isHovered} dimmed=${isDimmed}
                                                onSelect=${handleSelect}
                                                onHover=${setHoveredId}
                                                onUnhover=${() => setHoveredId(null)}
                                                allNodes=${layout.nodes} />`;
                                        })}
                                    </div>
                                </div>

                                <${StateLegend} tasks=${ungroupedTasks} stateColors=${DEFAULT_STATE_COLORS} />
                            </div>
                        </div>

                        ${selectedTaskId ? html`
                            <div class="panel-backdrop" onClick=${handleClose}></div>
                            <${GraphDetailPanel}
                                key=${selectedTaskId}
                                taskId=${selectedTaskId}
                                allTasks=${allTasks}
                                jiraBaseUrl=${jiraBaseUrl}
                                onClose=${handleClose}
                                onAction=${onAction} />
                        ` : null}
                    </div>
                </div>
            `}

            ${!collapsed && !layout && html`
                <div class="border border-t-0 border-slate-700 rounded-b-lg p-8 text-center">
                    <p class="text-slate-500 text-sm">Loading graph...</p>
                </div>
            `}
        </div>
    `;
}

export function ProjectDetail({ projectId, jiraBaseUrl, onAction }) {
    const [project, setProject] = useState(null);
    const [components, setComponents] = useState(null);
    const [error, setError] = useState(null);

    useEffect(() => {
        async function load() {
            try {
                const [proj, comps] = await Promise.all([
                    api.getProject(projectId),
                    api.getComponents(projectId),
                ]);
                setProject(proj);
                setComponents(comps);
            } catch (e) {
                setError(e.message);
            }
        }
        load();
    }, [projectId]);

    if (error) {
        return html`<div class="p-6"><p class="text-red-400">Error: ${error}</p></div>`;
    }
    if (!project || components === null) {
        return html`<div class="p-6"><p class="text-slate-500">Loading...</p></div>`;
    }

    return html`
        <div class="p-6">
            <!-- Breadcrumb -->
            <div class="flex items-center gap-2 text-sm text-slate-500 mb-4">
                <a href="#/projects" class="hover:text-slate-300">Projects</a>
                <span>/</span>
                <span class="text-slate-300">${projectId}</span>
            </div>

            <div class="flex items-center justify-between mb-6">
                <div>
                    <h2 class="text-xl font-semibold text-slate-100">${projectId}</h2>
                    <div class="text-sm text-slate-400 mt-1">
                        <span class="font-mono">${project.repo}</span>
                        <span class="mx-2">·</span>
                        branch: <span class="font-mono">${project.default_branch}</span>
                    </div>
                </div>
                <a href=${`#/?project_id=${encodeURIComponent(projectId)}`}
                    class="px-3 py-1.5 text-sm rounded bg-slate-800 text-slate-300 hover:bg-slate-700">
                    View All Tasks
                </a>
            </div>

            <!-- Component cards -->
            ${components.length === 0
                ? html`<div class="rounded-lg border border-slate-700 border-dashed p-8 text-center">
                    <p class="text-slate-500 mb-1">No components defined</p>
                    <p class="text-xs text-slate-600">Create components via the MCP tools to group tasks</p>
                  </div>`
                : html`
                    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 mb-8">
                        ${components.map(comp => html`<${ComponentCard} key=${comp.id} comp=${comp} />`)}
                    </div>
                `
            }

            <!-- Ungrouped tasks — DAG view -->
            <${UngroupedDagSection}
                projectId=${projectId}
                jiraBaseUrl=${jiraBaseUrl}
                onAction=${onAction} />
        </div>
    `;
}
