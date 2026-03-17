// DAG Graph Visualization — dependency graph as primary component view
import { useState, useEffect, useRef, useCallback, useMemo } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { html, relativeTime, StatusBadge, GateBadge, navigate } from './utils.js';

// ── Default state colors (can be overridden by project config) ──
const DEFAULT_STATE_COLORS = {
    ready:          { bg: '#3b82f620', border: '#3b82f6', text: '#93c5fd', dot: '#3b82f6', label: 'Ready' },
    blocked:        { bg: '#33415520', border: '#475569', text: '#94a3b8', dot: '#475569', label: 'Blocked' },
    working:        { bg: '#f59e0b20', border: '#f59e0b', text: '#fbbf24', dot: '#f59e0b', label: 'Working', pulse: true },
    testing:        { bg: '#3b82f620', border: '#3b82f6', text: '#93c5fd', dot: '#3b82f6', label: 'Testing', pulse: true },
    reviewing:      { bg: '#ec489920', border: '#ec4899', text: '#f472b6', dot: '#ec4899', label: 'Reviewing', pulse: true },
    'needs-review': { bg: '#8b5cf620', border: '#8b5cf6', text: '#a78bfa', dot: '#8b5cf6', label: 'Needs Review' },
    completed:      { bg: '#22c55e20', border: '#22c55e', text: '#4ade80', dot: '#22c55e', label: 'Completed' },
    merged:         { bg: '#14b8a620', border: '#14b8a6', text: '#2dd4bf', dot: '#14b8a6', label: 'Merged' },
    failed:         { bg: '#ef444420', border: '#ef4444', text: '#f87171', dot: '#ef4444', label: 'Failed' },
    cancelled:      { bg: '#64748b20', border: '#64748b', text: '#94a3b8', dot: '#64748b', label: 'Cancelled' },
    'turns-exhausted': { bg: '#f9731620', border: '#f97316', text: '#fb923c', dot: '#f97316', label: 'Turns Exhausted' },
};

// ── Component colors (for left border grouping) ──────────────
const COMPONENT_PALETTE = [
    '#3b82f6', '#22c55e', '#f59e0b', '#ec4899', '#8b5cf6',
    '#14b8a6', '#ef4444', '#06b6d4', '#f97316', '#a855f7',
    '#84cc16', '#e11d48', '#0ea5e9', '#eab308', '#6366f1',
];

// ── Layout constants ─────────────────────────────────────────
const NODE_W = 280;
const NODE_H = 120;
const GAP_X = 50;
const GAP_Y = 70;
const PADDING = 40;

// ── Layout engine ────────────────────────────────────────────

function deriveComponent(task) {
    // Extract component name from task ID: "project/component-name" → "component-name"
    const short = task.id.includes('/') ? task.id.split('/').pop() : task.id;
    // Group by prefix before first dash-separated version indicator (v1, v2, etc.)
    const match = short.match(/^(.+?)(?:-v\d|$)/);
    return match ? match[1] : short;
}

function computeLayout(tasks, stateColors) {
    const STATE_COLORS = stateColors;
    if (!tasks || tasks.length === 0) return { nodes: [], edges: [], width: 0, height: 0 };

    // Filter out subtasks (parent_task_id = review/test subtasks)
    const mainTasks = tasks.filter(t => !t.parent_task_id);
    const taskMap = new Map(mainTasks.map(t => [t.id, t]));

    // Determine effective status: if depends_on exists and parent isn't completed/merged, it's blocked
    const effectiveStatus = (t) => {
        if (t.depends_on && taskMap.has(t.depends_on)) {
            const parent = taskMap.get(t.depends_on);
            if (!['completed', 'merged'].includes(parent.status) || (parent.gate_status && parent.gate_status !== 'passed')) {
                if (t.status === 'ready') return 'blocked';
            }
        }
        // Map gate_status to display status for active gates
        if (t.status === 'working' && t.gate_status === 'testing') return 'testing';
        if (t.status === 'working' && t.gate_status === 'reviewing') return 'reviewing';
        return t.status || 'ready';
    };

    // Build adjacency: parent → children
    const children = new Map();
    const parents = new Map();
    for (const t of mainTasks) {
        if (!children.has(t.id)) children.set(t.id, []);
        if (!parents.has(t.id)) parents.set(t.id, []);
        if (t.depends_on && taskMap.has(t.depends_on)) {
            parents.get(t.id).push(t.depends_on);
            if (!children.has(t.depends_on)) children.set(t.depends_on, []);
            children.get(t.depends_on).push(t.id);
        }
    }

    // Assign ranks via BFS from roots
    const ranks = new Map();
    const roots = mainTasks.filter(t => !t.depends_on || !taskMap.has(t.depends_on));

    // For tasks with deps, rank = parent rank + 1. For roots, rank = 0
    const queue = [...roots.map(t => t.id)];
    for (const id of queue) {
        if (!ranks.has(id)) ranks.set(id, 0);
    }

    // BFS: assign ranks
    let i = 0;
    while (i < queue.length) {
        const id = queue[i++];
        const rank = ranks.get(id);
        for (const childId of (children.get(id) || [])) {
            const newRank = rank + 1;
            if (!ranks.has(childId) || ranks.get(childId) < newRank) {
                ranks.set(childId, newRank);
            }
            if (!queue.includes(childId)) queue.push(childId);
        }
    }

    // Handle orphans (tasks not reachable from roots, shouldn't happen but be safe)
    for (const t of mainTasks) {
        if (!ranks.has(t.id)) ranks.set(t.id, 0);
    }

    // Group by rank
    const rankGroups = new Map();
    for (const t of mainTasks) {
        const r = ranks.get(t.id);
        if (!rankGroups.has(r)) rankGroups.set(r, []);
        rankGroups.get(r).push(t);
    }

    // Sort ranks, then sort within each rank by component then creation order
    const sortedRanks = [...rankGroups.keys()].sort((a, b) => a - b);

    // Assign component colors
    const components = [...new Set(mainTasks.map(deriveComponent))];
    const componentColors = new Map(components.map((c, i) => [c, COMPONENT_PALETTE[i % COMPONENT_PALETTE.length]]));

    // Compute positions
    const nodes = [];
    let maxX = 0;

    for (const rank of sortedRanks) {
        const group = rankGroups.get(rank);
        // Sort by component name, then by creation date
        group.sort((a, b) => {
            const ca = deriveComponent(a), cb = deriveComponent(b);
            if (ca !== cb) return ca.localeCompare(cb);
            return (a.created_at || '').localeCompare(b.created_at || '');
        });

        const startX = PADDING;

        for (let j = 0; j < group.length; j++) {
            const t = group[j];
            const x = startX + j * (NODE_W + GAP_X);
            const y = PADDING + rank * (NODE_H + GAP_Y);
            const comp = deriveComponent(t);
            const status = effectiveStatus(t);

            nodes.push({
                id: t.id,
                task: t,
                x, y,
                status,
                component: comp,
                componentColor: componentColors.get(comp),
                stateColor: STATE_COLORS[status] || STATE_COLORS.ready,
            });

            if (x + NODE_W > maxX) maxX = x + NODE_W;
        }
    }

    // Build edges
    const nodeMap = new Map(nodes.map(n => [n.id, n]));
    const edges = [];
    for (const t of mainTasks) {
        if (t.depends_on && nodeMap.has(t.depends_on) && nodeMap.has(t.id)) {
            const from = nodeMap.get(t.depends_on);
            const to = nodeMap.get(t.id);
            const crossComponent = deriveComponent(from.task) !== deriveComponent(to.task);
            edges.push({
                fromId: from.id,
                toId: to.id,
                x1: from.x + NODE_W / 2,
                y1: from.y + NODE_H,
                x2: to.x + NODE_W / 2,
                y2: to.y,
                crossComponent,
            });
        }
    }

    const height = (sortedRanks.length > 0 ? (Math.max(...sortedRanks) + 1) * (NODE_H + GAP_Y) : NODE_H) + PADDING * 2;
    const width = Math.max(maxX + PADDING, 600);

    return { nodes, edges, width, height, componentColors };
}

// ── Heartbeat helper ─────────────────────────────────────────
function heartbeatClass(task) {
    if (task.status !== 'working') return null;
    const la = task.last_activity;
    if (!la) return 'heartbeat-dead';
    const age = (Date.now() - new Date(la + (la.endsWith('Z') ? '' : 'Z')).getTime()) / 1000;
    if (age > 300) return 'heartbeat-dead';   // >5min
    if (age > 120) return 'heartbeat-stale';  // >2min
    return 'heartbeat-active';
}

function heartbeatLabel(cls) {
    if (cls === 'heartbeat-active') return 'Active';
    if (cls === 'heartbeat-stale') return 'Stale (>2m)';
    if (cls === 'heartbeat-dead') return 'Dead (>5m)';
    return '';
}

// ── SVG Edge component ──────────────────────────────────────
function EdgePath({ edge, highlighted, dimmed }) {
    const { x1, y1, x2, y2, crossComponent } = edge;
    const midY = (y1 + y2) / 2;
    const d = `M${x1},${y1} C${x1},${midY} ${x2},${midY} ${x2},${y2}`;
    const opacity = dimmed ? 0.1 : highlighted ? 1 : 0.4;
    const stroke = crossComponent ? '#a78bfa' : '#475569';
    const dashArray = crossComponent ? '6,4' : 'none';
    const strokeWidth = highlighted ? 2.5 : 1.5;

    return html`
        <g>
            <path d=${d} fill="none" stroke=${stroke} stroke-width=${strokeWidth}
                stroke-dasharray=${dashArray} opacity=${opacity}
                style="transition: opacity 0.2s, stroke-width 0.2s" />
            <!-- Arrowhead -->
            <polygon points="${x2 - 5},${y2 - 8} ${x2 + 5},${y2 - 8} ${x2},${y2 - 2}"
                fill=${stroke} opacity=${opacity}
                style="transition: opacity 0.2s" />
        </g>
    `;
}

// ── Task Node component ─────────────────────────────────────
function TaskNode({ node, selected, hovered, dimmed, onSelect, onHover, onUnhover, allNodes }) {
    const { task, x, y, status, componentColor, stateColor } = node;
    const shortId = task.id.includes('/') ? task.id.split('/').pop() : task.id;
    const hb = heartbeatClass(task);
    const opacity = dimmed ? 0.1 : 1;

    // Blockers: find what this task depends on that isn't done
    const blockers = [];
    if (status === 'blocked' && task.depends_on) {
        const parent = allNodes.find(n => n.id === task.depends_on);
        if (parent) blockers.push(parent);
    }

    return html`
        <div class="dag-node ${selected ? 'dag-node-selected' : ''} ${hovered ? 'dag-node-hovered' : ''}"
            style="left:${x}px; top:${y}px; width:${NODE_W}px; height:${NODE_H}px;
                   border-left: 4px solid ${componentColor};
                   background: ${stateColor.bg};
                   border-color: ${selected ? stateColor.border : 'transparent'};
                   border-left-color: ${componentColor};
                   opacity: ${opacity};
                   transition: opacity 0.2s, box-shadow 0.2s;"
            onClick=${(e) => { e.stopPropagation(); onSelect(node.id); }}
            onMouseEnter=${() => onHover(node.id)}
            onMouseLeave=${onUnhover}>

            <div class="flex items-center gap-2 mb-1">
                <span class="inline-block w-2.5 h-2.5 rounded-full ${stateColor.pulse ? 'status-dot-working' : ''}"
                    style="background: ${stateColor.dot}"></span>
                <span class="text-xs font-medium truncate" style="color: ${stateColor.text}">${(stateColor.label || status).toUpperCase()}</span>
                ${hb ? html`<span class="w-2 h-2 rounded-full ${hb}" title=${heartbeatLabel(hb)}></span>` : null}
            </div>

            <div class="text-sm font-mono text-slate-200 truncate mb-0.5" title=${task.id}>${shortId}</div>
            <div class="text-xs text-slate-400 truncate mb-1" title=${task.goal}>${(task.goal || '').slice(0, 60)}</div>

            <div class="flex items-center gap-2 text-xs text-slate-500 mt-auto">
                ${task.model ? html`<span>${task.model}</span>` : null}
                ${task.total_cost_usd ? html`<span>$${task.total_cost_usd.toFixed(2)}</span>` : null}
                ${task.last_activity ? html`<span>${relativeTime(task.last_activity)}</span>` : null}
            </div>

            ${blockers.length > 0 ? html`
                <div class="absolute -bottom-1 left-1/2 -translate-x-1/2 px-2 py-0.5 rounded text-xs bg-slate-800 text-slate-400 border border-slate-700 whitespace-nowrap"
                    style="transform: translateX(-50%) translateY(50%); z-index: 5;">
                    Blocked by: ${blockers.map(b => b.task.id.split('/').pop()).join(', ')}
                </div>
            ` : null}
        </div>
    `;
}

// ── Tag Filter Bar ──────────────────────────────────────────
function TagFilterBar({ tags, activeTags, onToggleTag, onClear, componentColors }) {
    return html`
        <div class="flex flex-wrap items-center gap-2 mb-4 px-2">
            <span class="text-xs text-slate-500 mr-1">Filter:</span>
            ${tags.map(tag => {
                const active = activeTags.has(tag);
                return html`
                    <button key=${tag} onClick=${() => onToggleTag(tag)}
                        class="px-2 py-0.5 rounded text-xs font-medium transition-colors
                            ${active ? 'bg-blue-600 text-white' : 'bg-slate-800 text-slate-400 hover:bg-slate-700'}">
                        ${tag}
                    </button>`;
            })}
            ${activeTags.size > 0 ? html`
                <button onClick=${onClear}
                    class="px-2 py-0.5 rounded text-xs bg-slate-700 text-slate-300 hover:bg-slate-600">Clear</button>
            ` : null}

            ${componentColors && componentColors.size > 1 ? html`
                <span class="ml-4 text-xs text-slate-500">Components:</span>
                ${[...componentColors.entries()].map(([name, color]) => html`
                    <span key=${name} class="flex items-center gap-1 text-xs text-slate-400">
                        <span class="inline-block w-3 h-1.5 rounded" style="background: ${color}"></span>
                        ${name}
                    </span>
                `)}
            ` : null}
        </div>
    `;
}

// ── State Legend ─────────────────────────────────────────────
function StateLegend({ tasks, stateColors }) {
    const STATE_COLORS = stateColors;
    const counts = {};
    for (const t of tasks) {
        const s = t.status || 'ready';
        counts[s] = (counts[s] || 0) + 1;
    }

    return html`
        <div class="flex flex-wrap items-center gap-3 px-2 py-2 mt-2 border-t border-slate-800">
            ${Object.entries(STATE_COLORS).filter(([k]) => counts[k]).map(([status, sc]) => html`
                <span key=${status} class="flex items-center gap-1.5 text-xs">
                    <span class="inline-block w-2.5 h-2.5 rounded-full" style="background: ${sc.dot}"></span>
                    <span style="color: ${sc.text}">${sc.label}</span>
                    <span class="text-slate-600">(${counts[status]})</span>
                </span>
            `)}
            <span class="text-xs text-slate-600 ml-2">Total: ${tasks.length}</span>
        </div>
    `;
}

// ── Main DagGraph component ─────────────────────────────────
export function DagGraph({ projectId, onSelectTask, onTasksUpdate, selectedTaskId }) {
    const [tasks, setTasks] = useState(null);
    const [error, setError] = useState(null);
    const [hoveredId, setHoveredId] = useState(null);
    const [activeTags, setActiveTags] = useState(new Set());
    const [stateColors, setStateColors] = useState(DEFAULT_STATE_COLORS);
    const mountedRef = useRef(true);
    const containerRef = useRef(null);

    // Attempt to load custom state definitions from project config
    useEffect(() => {
        api.getProject(projectId).then(proj => {
            if (proj && proj.state_definitions && typeof proj.state_definitions === 'object') {
                // Merge custom definitions over defaults
                setStateColors(prev => ({ ...prev, ...proj.state_definitions }));
            }
        }).catch(() => {}); // Fallback to defaults silently
    }, [projectId]);

    // Fetch tasks for project
    const loadTasks = useCallback(async () => {
        try {
            const data = await api.getTasks({ project_id: projectId });
            if (mountedRef.current) {
                setTasks(data);
                if (onTasksUpdate) onTasksUpdate(data);
                if (!error) setError(null);
            }
        } catch (e) {
            if (mountedRef.current && !tasks) setError(e.message);
        }
    }, [projectId]);

    useEffect(() => {
        mountedRef.current = true;
        loadTasks();
        const timer = setInterval(loadTasks, 5000);
        return () => { mountedRef.current = false; clearInterval(timer); };
    }, [loadTasks]);

    // Compute layout
    const layout = useMemo(() => {
        if (!tasks) return null;
        return computeLayout(tasks, stateColors);
    }, [tasks, stateColors]);

    // Collect all unique tags
    const allTags = useMemo(() => {
        if (!tasks) return [];
        const s = new Set();
        tasks.forEach(t => (t.tags || []).forEach(tag => s.add(tag)));
        return [...s].sort();
    }, [tasks]);

    // Tag filtering
    const toggleTag = useCallback((tag) => {
        setActiveTags(prev => {
            const next = new Set(prev);
            if (next.has(tag)) next.delete(tag);
            else next.add(tag);
            return next;
        });
    }, []);

    const clearTags = useCallback(() => setActiveTags(new Set()), []);

    // Determine which nodes are visible (tag filter)
    const visibleIds = useMemo(() => {
        if (!layout || activeTags.size === 0) return null; // null = all visible
        const ids = new Set();
        for (const node of layout.nodes) {
            const taskTags = node.task.tags || [];
            if (taskTags.some(t => activeTags.has(t))) ids.add(node.id);
        }
        return ids;
    }, [layout, activeTags]);

    // Hover: find connected nodes
    const connectedIds = useMemo(() => {
        if (!hoveredId || !layout) return null;
        const ids = new Set([hoveredId]);
        for (const edge of layout.edges) {
            if (edge.fromId === hoveredId) ids.add(edge.toId);
            if (edge.toId === hoveredId) ids.add(edge.fromId);
        }
        return ids;
    }, [hoveredId, layout]);

    if (error) {
        return html`<div class="p-6"><div class="bg-slate-900 border border-slate-800 rounded-lg p-4">
            <p class="text-red-400">Error loading tasks: ${error}</p>
        </div></div>`;
    }

    if (!tasks || !layout) {
        return html`<div class="p-6"><p class="text-slate-500">Loading graph...</p></div>`;
    }

    if (layout.nodes.length === 0) {
        return html`<div class="p-6"><p class="text-slate-500">No tasks in this project</p></div>`;
    }

    return html`
        <div class="dag-container">
            <${TagFilterBar} tags=${allTags} activeTags=${activeTags}
                onToggleTag=${toggleTag} onClear=${clearTags}
                componentColors=${layout.componentColors} />

            <div class="dag-scroll" ref=${containerRef}
                onClick=${() => onSelectTask(null)}>
                <div class="dag-canvas" style="width:${layout.width}px; height:${layout.height}px; position:relative;">
                    <!-- SVG edges layer -->
                    <svg class="dag-edges" width=${layout.width} height=${layout.height}
                        style="position:absolute; top:0; left:0; pointer-events:none; z-index:1;">
                        <defs>
                            <marker id="arrow" viewBox="0 0 10 10" refX="5" refY="5"
                                markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                                <path d="M 0 0 L 10 5 L 0 10 z" fill="#475569" />
                            </marker>
                        </defs>
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
                        const isSelected = selectedTaskId === node.id;
                        const isHovered = hoveredId === node.id;
                        const isDimmed = (connectedIds && !connectedIds.has(node.id)) ||
                                         (visibleIds && !visibleIds.has(node.id));
                        return html`<${TaskNode} key=${node.id} node=${node}
                            selected=${isSelected} hovered=${isHovered} dimmed=${isDimmed}
                            onSelect=${onSelectTask}
                            onHover=${setHoveredId}
                            onUnhover=${() => setHoveredId(null)}
                            allNodes=${layout.nodes} />`;
                    })}
                </div>
            </div>

            <${StateLegend} tasks=${tasks.filter(t => !t.parent_task_id)} stateColors=${stateColors} />
        </div>
    `;
}
