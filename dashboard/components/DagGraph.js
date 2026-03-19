// DAG Graph Visualization — dependency graph as primary component view
import { useState, useEffect, useRef, useCallback, useMemo } from 'https://esm.sh/preact@10.25.4/hooks';
import dagre from 'https://esm.sh/dagre@0.8.5?bundle-deps';
import { api } from '../api.js';
import { html, relativeTime, StatusBadge, GateBadge, Tip, LoadingState, ErrorState, EmptyState, navigate } from './utils.js';

// ── Default state colors (can be overridden by project config) ──
export const DEFAULT_STATE_COLORS = {
    ready:          { bg: '#3b82f620', border: '#3b82f6', text: '#93c5fd', dot: '#3b82f6', label: 'Ready' },
    held:           { bg: '#a855f720', border: '#a855f7', text: '#c084fc', dot: '#a855f7', label: '\uD83D\uDD12 Held' },
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
    'rate-limited':    { bg: '#f9731620', border: '#f97316', text: '#fb923c', dot: '#f97316', label: 'Rate Limited' },
};

// ── Component colors (for left border grouping) ──────────────
const COMPONENT_PALETTE = [
    '#3b82f6', '#22c55e', '#f59e0b', '#ec4899', '#8b5cf6',
    '#14b8a6', '#ef4444', '#06b6d4', '#f97316', '#a855f7',
    '#84cc16', '#e11d48', '#0ea5e9', '#eab308', '#6366f1',
];

// ── Layout constants ─────────────────────────────────────────
export const NODE_W = 280;
export const NODE_H = 170;
const GAP_X = 60;               // horizontal gap between nodes in same rank
const GAP_Y = 140;              // vertical gap between ranks — more room for edges to route
const PADDING = 40;
const DAGRE_NODE_PADDING = 36;  // extra dagre bbox margin so edges route further from node edges

// ── Layout engine ────────────────────────────────────────────

function deriveComponent(task) {
    // Extract component name from task ID: "project/component-name" → "component-name"
    const short = task.id.includes('/') ? task.id.split('/').pop() : task.id;
    // Group by prefix before first dash-separated version indicator (v1, v2, etc.)
    const match = short.match(/^(.+?)(?:-v\d|$)/);
    return match ? match[1] : short;
}

// ── Connected component detection ────────────────────────────
function findConnectedComponents(taskIds, adjacency) {
    const visited = new Set();
    const components = [];
    for (const id of taskIds) {
        if (visited.has(id)) continue;
        const component = [];
        const queue = [id];
        visited.add(id);
        while (queue.length > 0) {
            const cur = queue.shift();
            component.push(cur);
            for (const neighbor of (adjacency.get(cur) || [])) {
                if (!visited.has(neighbor)) {
                    visited.add(neighbor);
                    queue.push(neighbor);
                }
            }
        }
        components.push(component);
    }
    return components;
}

// ── Push an edge control point outside a node bounding box ───
function pushPointOutsideNode(pt, box, margin = 14) {
    const left = box.x - margin;
    const right = box.x + box.w + margin;
    const top = box.y - margin;
    const bottom = box.y + box.h + margin;
    if (pt.x < left || pt.x > right || pt.y < top || pt.y > bottom) return pt;
    // Point is inside the padded box — push it to the nearest edge
    const distLeft = pt.x - left;
    const distRight = right - pt.x;
    const distTop = pt.y - top;
    const distBottom = bottom - pt.y;
    const minDist = Math.min(distLeft, distRight, distTop, distBottom);
    if (minDist === distLeft)   return { x: left,   y: pt.y   };
    if (minDist === distRight)  return { x: right,  y: pt.y   };
    if (minDist === distTop)    return { x: pt.x,   y: top    };
    return                             { x: pt.x,   y: bottom };
}

function pushPointOutsideNodes(pt, nodeBoxes) {
    let result = pt;
    for (const box of nodeBoxes) result = pushPointOutsideNode(result, box);
    return result;
}

export function computeLayout(tasks, stateColors) {
    const STATE_COLORS = stateColors;
    if (!tasks || tasks.length === 0) return { nodes: [], edges: [], width: 0, height: 0 };

    // Filter out subtasks (parent_task_id = review/test subtasks)
    const mainTasks = tasks.filter(t => !t.parent_task_id);
    const taskMap = new Map(mainTasks.map(t => [t.id, t]));

    // Determine effective status
    const effectiveStatus = (t) => {
        if (t.held) return 'held';
        if (t.depends_on && taskMap.has(t.depends_on)) {
            const parent = taskMap.get(t.depends_on);
            if (!['completed', 'merged'].includes(parent.status) || (parent.gate_status && parent.gate_status !== 'passed')) {
                if (t.status === 'ready') return 'blocked';
            }
        }
        if (t.status === 'working' && t.gate_status === 'testing') return 'testing';
        if (t.status === 'working' && t.gate_status === 'reviewing') return 'reviewing';
        return t.status || 'ready';
    };

    // Assign component colors
    const components = [...new Set(mainTasks.map(deriveComponent))];
    const componentColors = new Map(components.map((c, i) => [c, COMPONENT_PALETTE[i % COMPONENT_PALETTE.length]]));

    // Build undirected adjacency for connected component detection
    const undirectedAdj = new Map();
    for (const t of mainTasks) {
        if (!undirectedAdj.has(t.id)) undirectedAdj.set(t.id, []);
        if (t.depends_on && taskMap.has(t.depends_on)) {
            undirectedAdj.get(t.id).push(t.depends_on);
            if (!undirectedAdj.has(t.depends_on)) undirectedAdj.set(t.depends_on, []);
            undirectedAdj.get(t.depends_on).push(t.id);
        }
    }

    // Detect connected components
    const connectedComponents = findConnectedComponents(
        mainTasks.map(t => t.id), undirectedAdj
    );

    // Separate chains (2+ tasks) from singletons (independent tasks)
    const chains = connectedComponents.filter(c => c.length > 1);
    const singletons = connectedComponents.filter(c => c.length === 1);

    // Sort chains by earliest task created_at (oldest first = left-to-right timeline)
    const earliestCreated = (ids) => Math.min(...ids.map(id => new Date(taskMap.get(id).created_at || 0).getTime()));
    chains.sort((a, b) => earliestCreated(a) - earliestCreated(b));

    // Lay out each chain with dagre, then grid-pack singletons
    const allNodes = [];
    const allEdges = [];
    let xOffset = PADDING;
    const COMPONENT_GAP = 80;
    let maxChainHeight = 0;  // track tallest chain for singleton grid rows

    for (const compIds of chains) {
        const compTaskSet = new Set(compIds);
        const compTasks = compIds.map(id => taskMap.get(id));

        // Build dagre graph for this component
        const g = new dagre.graphlib.Graph();
        g.setGraph({
            rankdir: 'TB',
            ranksep: GAP_Y,
            nodesep: GAP_X,
            edgesep: 20,
            marginx: 0,
            marginy: 0,
        });
        g.setDefaultEdgeLabel(() => ({}));

        // Add nodes — inflate dimensions so dagre routes edges further from visual edges
        for (const t of compTasks) {
            g.setNode(t.id, { width: NODE_W + DAGRE_NODE_PADDING, height: NODE_H + DAGRE_NODE_PADDING });
        }

        // Add edges (depends_on → task, i.e. parent → child)
        for (const t of compTasks) {
            if (t.depends_on && compTaskSet.has(t.depends_on)) {
                g.setEdge(t.depends_on, t.id);
            }
        }

        // Run dagre layout
        dagre.layout(g);

        // Find bounding box of this component (use inflated dims to match dagre's coordinate space)
        const halfW = (NODE_W + DAGRE_NODE_PADDING) / 2;
        const halfH = (NODE_H + DAGRE_NODE_PADDING) / 2;
        let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
        for (const id of compIds) {
            const n = g.node(id);
            minX = Math.min(minX, n.x - halfW);
            maxX = Math.max(maxX, n.x + halfW);
            minY = Math.min(minY, n.y - halfH);
            maxY = Math.max(maxY, n.y + halfH);
        }

        // Map dagre positions to our coordinate space (offset so component starts at xOffset)
        const compNodeBoxes = [];  // screen-space bboxes for post-processing
        for (const t of compTasks) {
            const dagreNode = g.node(t.id);
            const x = xOffset + (dagreNode.x - minX) - NODE_W / 2 + PADDING;
            const y = (dagreNode.y - minY) + PADDING - NODE_H / 2;
            const comp = deriveComponent(t);
            const status = effectiveStatus(t);

            allNodes.push({
                id: t.id,
                task: t,
                x,
                y,
                status,
                component: comp,
                componentColor: componentColors.get(comp),
                stateColor: STATE_COLORS[status] || STATE_COLORS.ready,
            });
            compNodeBoxes.push({ x, y, w: NODE_W, h: NODE_H });
        }

        // Map dagre edges — use dagre's routed points for clean paths
        const nodeMap = new Map(allNodes.map(n => [n.id, n]));
        for (const t of compTasks) {
            if (t.depends_on && compTaskSet.has(t.depends_on) && nodeMap.has(t.depends_on) && nodeMap.has(t.id)) {
                const from = nodeMap.get(t.depends_on);
                const to = nodeMap.get(t.id);
                const crossComponent = deriveComponent(from.task) !== deriveComponent(to.task);

                // Get dagre's edge points and map to screen space
                const edgeData = g.edge(t.depends_on, t.id);
                let points = edgeData && edgeData.points ? edgeData.points.map(p => ({
                    x: xOffset + (p.x - minX) + PADDING,
                    y: (p.y - minY) + PADDING,
                })) : null;

                // Post-process: push any control point that lands inside a node bbox outward
                if (points) {
                    points = points.map(pt => pushPointOutsideNodes(pt, compNodeBoxes));
                }

                allEdges.push({
                    fromId: from.id,
                    toId: to.id,
                    x1: from.x + NODE_W / 2,
                    y1: from.y + NODE_H,
                    x2: to.x + NODE_W / 2,
                    y2: to.y,
                    points,
                    crossComponent,
                });
            }
        }

        // Advance xOffset for next component
        const compWidth = maxX - minX + PADDING * 2;
        const compHeight = maxY - minY + PADDING * 2;
        maxChainHeight = Math.max(maxChainHeight, compHeight);
        xOffset += compWidth + COMPONENT_GAP;
    }

    // Grid-pack singletons: stack N-high based on chain depth
    if (singletons.length > 0) {
        const rows = chains.length > 0
            ? Math.max(1, Math.round(maxChainHeight / (NODE_H + GAP_Y)))
            : Math.min(4, singletons.length);  // all singletons — stack 4 deep
        // Sort singletons by created_at (oldest first)
        singletons.sort((a, b) => {
            const ta = taskMap.get(a[0]), tb = taskMap.get(b[0]);
            return new Date(ta.created_at || 0).getTime() - new Date(tb.created_at || 0).getTime();
        });

        for (let i = 0; i < singletons.length; i++) {
            const t = taskMap.get(singletons[i][0]);
            const col = Math.floor(i / rows);
            const row = i % rows;
            const x = xOffset + col * (NODE_W + GAP_X);
            const y = PADDING + row * (NODE_H + GAP_Y);
            const comp = deriveComponent(t);
            const status = effectiveStatus(t);

            allNodes.push({
                id: t.id,
                task: t,
                x,
                y,
                status,
                component: comp,
                componentColor: componentColors.get(comp),
                stateColor: STATE_COLORS[status] || STATE_COLORS.ready,
            });
        }
    }

    // Calculate total dimensions
    let totalWidth = 0, totalHeight = 0;
    for (const n of allNodes) {
        totalWidth = Math.max(totalWidth, n.x + NODE_W + PADDING);
        totalHeight = Math.max(totalHeight, n.y + NODE_H + PADDING + 20);
    }
    totalWidth = Math.max(totalWidth, 600);

    return { nodes: allNodes, edges: allEdges, width: totalWidth, height: totalHeight, componentColors };
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

// ── Catmull-Rom → cubic bezier for smooth curves through waypoints ──
function catmullRomPath(pts) {
    if (pts.length < 2) return '';
    const d = [`M${pts[0].x.toFixed(1)},${pts[0].y.toFixed(1)}`];
    for (let i = 0; i < pts.length - 1; i++) {
        const p0 = pts[Math.max(0, i - 1)];
        const p1 = pts[i];
        const p2 = pts[i + 1];
        const p3 = pts[Math.min(pts.length - 1, i + 2)];
        const cp1x = p1.x + (p2.x - p0.x) / 6;
        const cp1y = p1.y + (p2.y - p0.y) / 6;
        const cp2x = p2.x - (p3.x - p1.x) / 6;
        const cp2y = p2.y - (p3.y - p1.y) / 6;
        d.push(`C${cp1x.toFixed(1)},${cp1y.toFixed(1)} ${cp2x.toFixed(1)},${cp2y.toFixed(1)} ${p2.x.toFixed(1)},${p2.y.toFixed(1)}`);
    }
    return d.join(' ');
}

// ── SVG Edge component ──────────────────────────────────────
export function EdgePath({ edge, highlighted, dimmed }) {
    const { x1, y1, x2, y2, points, crossComponent } = edge;
    const opacity = dimmed ? 0.1 : highlighted ? 1 : 0.4;
    const stroke = crossComponent ? '#a78bfa' : '#475569';
    const dashArray = crossComponent ? '6,4' : 'none';
    const strokeWidth = highlighted ? 2.5 : 1.5;

    // Build path: simple vertical-first step routing that never clips through nodes
    let d;
    const midY = (y1 + y2) / 2;
    if (x1 === x2) {
        // Straight vertical line
        d = `M${x1},${y1} L${x2},${y2}`;
    } else {
        // Step path: go down to midpoint, move horizontally, then down to target
        d = `M${x1},${y1} L${x1},${midY} L${x2},${midY} L${x2},${y2}`;
    }

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
export function TaskNode({ node, selected, hovered, dimmed, onSelect, onHover, onUnhover, allNodes }) {
    const { task, x, y, status, componentColor, stateColor } = node;
    const shortId = task.id.includes('/') ? task.id.split('/').pop() : task.id;
    const isGhost = !!task._ghost;
    const hb = isGhost ? null : heartbeatClass(task);
    const opacity = dimmed ? 0.1 : isGhost ? 0.7 : 1;

    // Blockers: find what this task depends on that isn't done
    const blockers = [];
    if (status === 'blocked' && task.depends_on) {
        const parent = allNodes.find(n => n.id === task.depends_on);
        if (parent) blockers.push(parent);
    }

    return html`
        <div class="dag-node ${selected ? 'dag-node-selected' : ''} ${hovered ? 'dag-node-hovered' : ''}"
            style="left:${x}px; top:${y}px; width:${NODE_W}px; height:${isGhost ? NODE_H - 30 : NODE_H}px;
                   border-left: 4px solid ${isGhost ? '#475569' : componentColor};
                   background: ${isGhost ? '#1e293b40' : stateColor.bg};
                   border-color: ${selected ? stateColor.border : isGhost ? '#475569' : 'transparent'};
                   border-left-color: ${isGhost ? '#475569' : componentColor};
                   ${isGhost ? 'border-style: dashed; border-width: 1px; border-left-width: 4px;' : ''}
                   opacity: ${opacity};
                   transition: opacity 0.2s, box-shadow 0.2s;
                   display: flex; flex-direction: column; overflow: hidden;"
            onClick=${(e) => { e.stopPropagation(); onSelect(node.id); }}
            onMouseEnter=${() => onHover(node.id)}
            onMouseLeave=${onUnhover}>

            ${isGhost && task.component_name ? html`
                <div class="text-[10px] text-slate-500 mb-0.5 shrink-0 truncate">\u2197 ${task.component_name}</div>
            ` : null}

            <div class="flex items-center gap-2 mb-1 shrink-0">
                <span class="inline-block w-2.5 h-2.5 rounded-full shrink-0 ${stateColor.pulse ? 'status-dot-working' : ''}"
                    style="background: ${stateColor.dot}"></span>
                <span class="text-xs font-medium truncate" style="color: ${stateColor.text}">${(stateColor.label || status).toUpperCase()}</span>
                ${hb ? html`<${Tip} text=${heartbeatLabel(hb)}><span class="w-2 h-2 rounded-full ${hb}"></span><//>` : null}
            </div>

            <div class="text-sm font-mono text-slate-200 truncate mb-0.5 shrink-0" title=${task.id}>${shortId}</div>
            <div class="text-xs text-slate-400 truncate mb-1 shrink-0" style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title=${task.goal}>${task.goal || ''}</div>

            ${task.checklist_total > 0 ? html`
                <div class="shrink-0 mb-1">
                    <div class="flex items-center gap-2">
                        <div class="flex-1 h-1.5 rounded-full bg-slate-700 overflow-hidden">
                            <div class="h-full rounded-full bg-emerald-500 transition-all"
                                style="width: ${Math.round((task.checklist_done / task.checklist_total) * 100)}%"></div>
                        </div>
                        <span class="text-xs text-slate-500 shrink-0">${task.checklist_done}/${task.checklist_total}</span>
                    </div>
                </div>
            ` : null}

            <div class="flex items-center gap-2 text-xs text-slate-500 mt-auto shrink-0">
                ${task.model ? html`<span>${task.model}</span>` : null}
                ${task.total_cost_usd ? html`<${Tip} text="Total API cost across all dispatches"><span>$${task.total_cost_usd.toFixed(2)}</span><//>` : null}
                ${task.last_activity ? html`<span>${relativeTime(task.last_activity)}</span>` : null}
            </div>

            ${blockers.length > 0 ? html`
                <div class="text-xs text-slate-500 mt-1 shrink-0 truncate" title=${"Blocked by: " + blockers.map(b => b.task.id.split('/').pop()).join(', ')}>
                    \u26D3 ${blockers.map(b => b.task.id.split('/').pop()).join(', ')}
                </div>
            ` : null}
        </div>
    `;
}

// ── Tag Filter Bar ──────────────────────────────────────────
export function TagFilterBar({ tags, activeTags, onToggleTag, onClear, componentColors }) {
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
export function StateLegend({ tasks, stateColors }) {
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
                    <span class="text-slate-500">(${counts[status]})</span>
                </span>
            `)}
            <span class="text-xs text-slate-500 ml-2">Total: ${tasks.length}</span>
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
    const [zoom, setZoom] = useState(1);
    const [pan, setPan] = useState({ x: 0, y: 0 });
    const [dragging, setDragging] = useState(false);
    const dragStart = useRef(null);
    const mountedRef = useRef(true);
    const containerRef = useRef(null);

    const lastTouchDist = useRef(null);

    // Attach wheel + touch listeners with passive:false
    useEffect(() => {
        const el = containerRef.current;
        if (!el) return;
        const onWheel = (e) => {
            if (!e.ctrlKey && !e.metaKey) return;
            e.preventDefault();
            const delta = e.deltaY > 0 ? -0.05 : 0.05;
            setZoom(z => Math.min(2, Math.max(0.3, z + delta)));
        };
        const onTouchStart = (e) => {
            if (e.touches.length === 1) {
                dragStart.current = { x: e.touches[0].clientX - pan.x, y: e.touches[0].clientY - pan.y };
                setDragging(true);
            } else if (e.touches.length === 2) {
                e.preventDefault();
                const dx = e.touches[0].clientX - e.touches[1].clientX;
                const dy = e.touches[0].clientY - e.touches[1].clientY;
                lastTouchDist.current = Math.sqrt(dx * dx + dy * dy);
            }
        };
        const onTouchMove = (e) => {
            if (e.touches.length === 1 && dragStart.current) {
                setPan({ x: e.touches[0].clientX - dragStart.current.x, y: e.touches[0].clientY - dragStart.current.y });
            } else if (e.touches.length === 2 && lastTouchDist.current) {
                e.preventDefault();
                const dx = e.touches[0].clientX - e.touches[1].clientX;
                const dy = e.touches[0].clientY - e.touches[1].clientY;
                const dist = Math.sqrt(dx * dx + dy * dy);
                const delta = (dist - lastTouchDist.current) * 0.005;
                lastTouchDist.current = dist;
                setZoom(z => Math.min(2, Math.max(0.3, z + delta)));
            }
        };
        const onTouchEnd = () => {
            setDragging(false);
            dragStart.current = null;
            lastTouchDist.current = null;
        };
        el.addEventListener('wheel', onWheel, { passive: false });
        el.addEventListener('touchstart', onTouchStart, { passive: false });
        el.addEventListener('touchmove', onTouchMove, { passive: false });
        el.addEventListener('touchend', onTouchEnd);
        return () => {
            el.removeEventListener('wheel', onWheel);
            el.removeEventListener('touchstart', onTouchStart);
            el.removeEventListener('touchmove', onTouchMove);
            el.removeEventListener('touchend', onTouchEnd);
        };
    }, [pan]);

    const handleMouseDown = useCallback((e) => {
        if (e.button !== 0) return;
        dragStart.current = { x: e.clientX - pan.x, y: e.clientY - pan.y };
        setDragging(true);
    }, [pan]);

    const handleMouseMove = useCallback((e) => {
        if (!dragging || !dragStart.current) return;
        setPan({ x: e.clientX - dragStart.current.x, y: e.clientY - dragStart.current.y });
    }, [dragging]);

    const handleMouseUp = useCallback(() => {
        setDragging(false);
        dragStart.current = null;
    }, []);

    const resetView = useCallback(() => {
        setZoom(1);
        setPan({ x: 0, y: 0 });
    }, []);

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
        return html`<div class="p-6"><${ErrorState} message="Error loading tasks: ${error}" onRetry=${loadTasks} /></div>`;
    }

    if (!tasks || !layout) {
        return html`<div class="p-6"><${LoadingState} message="Loading graph..." /></div>`;
    }

    if (layout.nodes.length === 0) {
        return html`<div class="p-6"><${EmptyState} message="No tasks in this project" /></div>`;
    }

    return html`
        <div class="dag-container">
            <${TagFilterBar} tags=${allTags} activeTags=${activeTags}
                onToggleTag=${toggleTag} onClear=${clearTags}
                componentColors=${layout.componentColors} />

            <div class="dag-scroll" ref=${containerRef}
                onMouseDown=${handleMouseDown}
                onMouseMove=${handleMouseMove}
                onMouseUp=${handleMouseUp}
                onMouseLeave=${handleMouseUp}
                onClick=${(e) => { if (!dragging) onSelectTask(null); }}
                style="cursor: ${dragging ? 'grabbing' : 'grab'};">
                <div class="flex items-center gap-2 px-2 py-1 text-xs text-slate-500">
                    <button onClick=${resetView} class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-400">Reset</button>
                    <span>${Math.round(zoom * 100)}%</span>
                </div>
                <div class="dag-canvas" style="width:${layout.width * zoom}px; height:${layout.height * zoom}px; position:relative; transform: scale(${zoom}); transform-origin: 0 0; translate: ${pan.x}px ${pan.y}px;">
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
