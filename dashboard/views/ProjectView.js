// Foreman Project View
// Layout: Recent Activity → Components (knowledge drawers) → Conversations → Tasks
// Spec: foreman-design conversation, messages [6-9]

import { h } from 'https://esm.sh/preact@10.25.4';
import { useState, useEffect, useCallback, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, layout, statusColors, animation } from '../tokens.js';
import { routes, navigate } from '../router.js';
import { api } from '../api.js';
import { StatusDot } from '../components/StatusDot.js';
import { ChainBadge } from '../components/ChainBadge.js';
import { relativeTime } from '../components/utils.js';
import { TaskView } from './TaskView.js';

const html = htm.bind(h);

const POLL_INTERVAL_MS = 15_000;
const ACTIVITY_LIMIT = 15;

// ---------------------------------------------------------------------------
// Chain position map — computed from depends_on graph
// ---------------------------------------------------------------------------

function buildChainMap(tasks) {
    const taskIds = new Set(tasks.map(t => t.id));
    const dependents = new Map(); // parentId → [childId]

    for (const t of tasks) {
        if (t.depends_on && taskIds.has(t.depends_on)) {
            if (!dependents.has(t.depends_on)) dependents.set(t.depends_on, []);
            dependents.get(t.depends_on).push(t.id);
        }
    }

    // Roots: tasks whose depends_on is absent or points outside this project
    const roots = tasks.filter(t => !t.depends_on || !taskIds.has(t.depends_on));

    const chainMap = new Map(); // taskId → { position, total, chainIds }

    for (const root of roots) {
        // BFS to collect chain
        const chain = [];
        const queue = [root.id];
        while (queue.length > 0) {
            const id = queue.shift();
            chain.push(id);
            (dependents.get(id) || []).forEach(c => queue.push(c));
        }
        if (chain.length > 1) {
            chain.forEach((id, i) => {
                chainMap.set(id, { position: i + 1, total: chain.length, chainIds: chain });
            });
        }
    }

    return chainMap;
}

// ---------------------------------------------------------------------------
// Recent Activity timeline
// ---------------------------------------------------------------------------

function activityIcon(ev) {
    const t = ev.event_type;
    if (t === 'result') {
        if (ev.task_status === 'completed') return { icon: '✓', color: colors.blue };
        if (ev.task_status === 'failed') return { icon: '✕', color: colors.red };
        return { icon: '✓', color: colors.textSecondary };
    }
    if (t === 'test-result') return { icon: '⚙', color: colors.accent };
    if (t === 'review') return { icon: '👁', color: colors.textSecondary };
    if (t === 'handoff') return { icon: '→', color: colors.blue };
    return { icon: '●', color: colors.textTertiary };
}

function RecentActivity({ projectId }) {
    const [events, setEvents] = useState([]);
    const [loading, setLoading] = useState(true);
    const [collapsed, setCollapsed] = useState(true);
    const mountedRef = useRef(true);

    useEffect(() => {
        mountedRef.current = true;
        return () => { mountedRef.current = false; };
    }, []);

    const load = useCallback(() => {
        api.getActivity({ project_id: projectId, limit: ACTIVITY_LIMIT })
            .then(data => { if (mountedRef.current) { setEvents(data); setLoading(false); } })
            .catch(() => { if (mountedRef.current) setLoading(false); });
    }, [projectId]);

    useEffect(() => {
        load();
        const timer = setInterval(load, ACTIVITY_LIMIT * 1000);
        return () => clearInterval(timer);
    }, [load]);

    if (!loading && events.length === 0) return null;

    const sectionStyle = {
        display: 'flex',
        flexDirection: 'column',
        gap: '0',
    };

    const headerStyle = {
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        marginBottom: collapsed ? 0 : '12px',
    };

    const titleStyle = {
        fontSize: typography.size.sm,
        fontWeight: typography.weight.semibold,
        color: colors.textSecondary,
        letterSpacing: '0.06em',
        textTransform: 'uppercase',
        display: 'flex',
        alignItems: 'center',
        gap: '6px',
    };

    const toggleStyle = {
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        background: 'none',
        border: 'none',
        cursor: 'pointer',
        padding: '2px 6px',
        borderRadius: layout.borderRadius.sm,
    };

    return html`
        <div style=${sectionStyle}>
            <div style=${{ ...headerStyle, cursor: 'pointer' }}
                 onClick=${() => setCollapsed(c => !c)}>
                <span style=${titleStyle}>◷ Recent Activity</span>
                <span style=${toggleStyle}>
                    ${collapsed ? 'Show ▾' : 'Hide ▴'}
                </span>
            </div>

            ${!collapsed ? html`
                <div style=${{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                    ${loading && events.length === 0 ? html`
                        <div style=${{ color: colors.textTertiary, fontSize: typography.size.sm, padding: '8px 0' }}>
                            Loading…
                        </div>
                    ` : events.map(ev => {
                        const { icon, color } = activityIcon(ev);
                        const desc = ev.title || (ev.content
                            ? ev.content.split('\n').find(l => l.trim())?.replace(/^#+\s*/, '').replace(/\*\*/g, '').trim()
                            : '');
                        const brief = desc && desc.length > 70 ? desc.slice(0, 69) + '…' : desc;
                        const goal = ev.task_goal
                            ? (ev.task_goal.length > 45 ? ev.task_goal.slice(0, 44) + '…' : ev.task_goal)
                            : ev.task_id;

                        return html`
                            <div key=${ev.id} style=${{
                                display: 'flex',
                                alignItems: 'baseline',
                                gap: '10px',
                                padding: '5px 0',
                                borderBottom: `1px solid ${colors.border}22`,
                            }}>
                                <span style=${{
                                    width: '16px',
                                    textAlign: 'center',
                                    fontSize: typography.size.xs,
                                    color,
                                    flexShrink: 0,
                                }}>${icon}</span>
                                <a href=${routes.task(ev.task_id)}
                                   style=${{
                                       color: colors.text,
                                       textDecoration: 'none',
                                       fontSize: typography.size.sm,
                                       flex: 1,
                                       minWidth: 0,
                                   }}
                                   class="foreman-activity-link"
                                >${goal}</a>
                                ${brief ? html`
                                    <span style=${{
                                        fontSize: typography.size.xs,
                                        color: colors.textTertiary,
                                        flex: 2,
                                        minWidth: 0,
                                        overflow: 'hidden',
                                        textOverflow: 'ellipsis',
                                        whiteSpace: 'nowrap',
                                    }}>${brief}</span>
                                ` : null}
                                <span style=${{
                                    fontFamily: typography.fontMono,
                                    fontSize: typography.size.xs,
                                    color: colors.textTertiary,
                                    flexShrink: 0,
                                    whiteSpace: 'nowrap',
                                }}>${relativeTime(ev.created_at)}</span>
                            </div>
                        `;
                    })}
                </div>
            ` : null}
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Component drawer — knowledge card for a component
// ---------------------------------------------------------------------------

function PunchlistSection({ componentId }) {
    const [items, setItems] = useState(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        api.getPunchlist(componentId)
            .then(data => { setItems(data); setLoading(false); })
            .catch(() => { setItems([]); setLoading(false); });
    }, [componentId]);

    if (loading) {
        return html`<div style=${{ color: colors.textTertiary, fontSize: typography.size.xs, padding: '4px 0' }}>Loading punchlist…</div>`;
    }
    if (!items || items.length === 0) {
        return html`<div style=${{ color: colors.textTertiary, fontSize: typography.size.xs, fontStyle: 'italic' }}>No punchlist items</div>`;
    }

    const openCount = items.filter(i => i.status !== 'resolved').length;
    const displayItems = items;

    const statusColor = (s) => {
        if (s === 'resolved') return colors.blue;
        if (s === 'claimed') return colors.yellow;
        return colors.textTertiary;
    };

    const statusIcon = (s) => {
        if (s === 'resolved') return '✓';
        if (s === 'claimed') return '●';
        return '○';
    };

    return html`
        <div>
            <div style=${{
                fontSize: typography.size.sm,
                fontWeight: typography.weight.semibold,
                color: colors.textSecondary,
                marginBottom: '8px',
            }}>Punchlist · ${openCount} open</div>
        <div style=${{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            ${displayItems.map(item => html`
                <div key=${item.id} style=${{
                    display: 'flex',
                    alignItems: 'baseline',
                    gap: '8px',
                    fontSize: typography.size.sm,
                }}>
                    <span style=${{
                        color: statusColor(item.status),
                        fontSize: typography.size.xs,
                        flexShrink: 0,
                        width: '12px',
                        textAlign: 'center',
                    }}>${statusIcon(item.status)}</span>
                    <span style=${{
                        color: item.status === 'resolved' ? colors.textTertiary : colors.textSecondary,
                        textDecoration: item.status === 'resolved' ? 'line-through' : 'none',
                        flex: 1,
                        lineHeight: 1.4,
                    }}>${item.text || item.item || item.description}</span>
                    ${item.task_id ? html`
                        <a href=${routes.task(item.task_id)} style=${{
                            fontFamily: typography.fontMono,
                            fontSize: typography.size.xs,
                            color: colors.accent,
                            textDecoration: 'none',
                            flexShrink: 0,
                        }}>↗</a>
                    ` : null}
                </div>
            `)}
        </div>
        </div>
    `;
}

function ComponentCard({ component, allTasks, onClick }) {
    const componentTasks = allTasks.filter(t => t.component_id === component.id);
    const runningCount = componentTasks.filter(t => t.status === 'working').length;

    const phaseColor = {
        planning: colors.yellow,
        building: colors.green,
        polish: colors.blue,
        deployed: colors.textSecondary,
    }[component.phase] || colors.textTertiary;

    const cardStyle = {
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.lg,
        overflow: 'hidden',
        transition: `border-color ${animation.durationNormal}`,
        cursor: 'pointer',
    };

    const headerStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '10px',
        padding: '12px 16px',
        userSelect: 'none',
    };

    const nameStyle = {
        fontFamily: typography.fontBody,
        fontSize: typography.size.md,
        fontWeight: typography.weight.medium,
        color: colors.text,
        flex: 1,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
    };

    const metaStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        flexShrink: 0,
    };

    return html`
        <div style=${cardStyle} class="foreman-component-card" onClick=${() => onClick(component)}>
            <div style=${headerStyle} class="foreman-component-header">
                <span style=${nameStyle}>${component.name || component.id}</span>
                <div style=${metaStyle}>
                    ${component.phase ? html`
                        <span style=${{
                            fontSize: typography.size.xs,
                            color: phaseColor,
                            fontWeight: typography.weight.medium,
                        }}>${component.phase}</span>
                    ` : null}
                    ${runningCount > 0 ? html`
                        <span style=${{
                            fontSize: typography.size.xs,
                            color: colors.green,
                            fontFamily: typography.fontMono,
                        }}>${runningCount} running</span>
                    ` : null}
                    <span style=${{
                        fontFamily: typography.fontMono,
                        fontSize: typography.size.xs,
                        color: colors.textTertiary,
                    }}>${componentTasks.length} task${componentTasks.length !== 1 ? 's' : ''}</span>
                </div>
            </div>
        </div>
    `;
}

function ComponentPanel({ component, conversations, allTasks, onClose, onFilterByComponent }) {
    const [isMobile, setIsMobile] = useState(() => window.innerWidth < 640);

    useEffect(() => {
        const check = () => setIsMobile(window.innerWidth < 640);
        window.addEventListener('resize', check);
        return () => window.removeEventListener('resize', check);
    }, []);

    useEffect(() => {
        const onKey = (e) => { if (e.key === 'Escape') onClose(); };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [onClose]);

    if (!component) return null;

    const linkedConvs = conversations.filter(c => c.component_id === component.id).slice(0, 5);
    const componentTasks = allTasks.filter(t => t.component_id === component.id);
    const runningCount = componentTasks.filter(t => t.status === 'working').length;
    const blockedCount = componentTasks.filter(t => t.status === 'failed' || t.status === 'needs-review').length;
    const doneCount = componentTasks.filter(t => t.status === 'completed' || t.status === 'merged').length;

    const summaryParts = [];
    if (runningCount) summaryParts.push(`${runningCount} running`);
    if (blockedCount) summaryParts.push(`${blockedCount} blocked`);
    if (doneCount) summaryParts.push(`${doneCount} done`);
    const summaryText = summaryParts.join(' · ') || 'No tasks';

    const phaseColor = {
        planning: colors.yellow,
        building: colors.green,
        polish: colors.blue,
        deployed: colors.textSecondary,
    }[component.phase] || colors.textTertiary;

    const panelStyle = isMobile ? {
        position: 'fixed', left: 0, right: 0, bottom: 0,
        height: '65vh', background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: `${layout.borderRadius.lg} ${layout.borderRadius.lg} 0 0`,
        boxShadow: '0 -8px 40px rgba(0,0,0,0.5)', zIndex: 500,
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
        animation: `foreman-slide-up ${animation.durationNormal} ${animation.easing}`,
    } : {
        position: 'fixed', top: 0, right: 0, bottom: 0,
        width: 'clamp(380px, 30vw, 520px)', background: colors.surface,
        border: `1px solid ${colors.border}`,
        boxShadow: '-8px 0 40px rgba(0,0,0,0.4)', zIndex: 500,
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
        animation: `foreman-slide-right ${animation.durationNormal} ${animation.easing}`,
    };

    const closeBtnStyle = {
        background: 'none', border: 'none', color: colors.textTertiary,
        cursor: 'pointer', fontSize: '20px', lineHeight: 1,
        padding: '2px 6px', borderRadius: layout.borderRadius.sm,
    };

    const subheadStyle = {
        fontSize: typography.size.xs, fontWeight: typography.weight.semibold,
        color: colors.textTertiary, letterSpacing: '0.06em',
        textTransform: 'uppercase', marginBottom: '6px',
    };

    const filterBtnStyle = {
        display: 'inline-flex', alignItems: 'center', gap: '6px',
        fontSize: typography.size.sm, fontWeight: typography.weight.medium,
        color: colors.accent, background: colors.accentBg,
        border: `1px solid rgba(124, 90, 246, 0.25)`,
        borderRadius: layout.borderRadius.md,
        padding: '8px 16px', cursor: 'pointer', width: '100%',
        justifyContent: 'center',
    };

    const handleFilter = () => {
        onFilterByComponent(component.id);
        onClose();
    };

    // Config overrides — check for non-default values
    const config = component.config || {};
    const hasOverrides = config.model || config.auto_test === false || config.auto_review === false
        || config.max_turns || config.max_wall_clock || config.test_command || config.setup_command;

    return html`
        <div>
            <style>${`
                @keyframes foreman-slide-right {
                    from { transform: translateX(100%); opacity: 0; }
                    to   { transform: translateX(0);    opacity: 1; }
                }
                @keyframes foreman-slide-up {
                    from { transform: translateY(100%); opacity: 0; }
                    to   { transform: translateY(0);    opacity: 1; }
                }
            `}</style>
            <div style=${{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)', zIndex: 499 }}
                 onClick=${onClose} />
            <div style=${panelStyle}>
                <!-- Header -->
                <div style=${{
                    display: 'flex', alignItems: 'center', gap: '10px',
                    padding: '12px 16px', borderBottom: `1px solid ${colors.border}`, flexShrink: 0,
                }}>
                    <span style=${{
                        fontFamily: typography.fontBody, fontSize: typography.size.lg,
                        fontWeight: typography.weight.semibold, color: colors.text,
                        flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    }}>${component.name || component.id}</span>
                    ${component.phase ? html`
                        <span style=${{
                            fontSize: typography.size.xs, color: phaseColor,
                            fontWeight: typography.weight.medium,
                            padding: '2px 8px', borderRadius: layout.borderRadius.pill,
                            background: phaseColor + '18',
                        }}>${component.phase}</span>
                    ` : null}
                    <button style=${closeBtnStyle} onClick=${onClose} title="Close (Esc)">×</button>
                </div>

                <!-- Body -->
                <div style=${{ flex: 1, overflowY: 'auto', padding: '16px', display: 'flex', flexDirection: 'column', gap: '20px' }}>
                    <!-- Summary -->
                    <div style=${{
                        fontSize: typography.size.sm, color: colors.textSecondary,
                    }}>${summaryText}</div>

                    <!-- Filter tasks button -->
                    <button style=${filterBtnStyle} onClick=${handleFilter}>
                        ⚡ Filter tasks to ${component.name || component.id}
                    </button>

                    <!-- Linked conversations -->
                    ${linkedConvs.length > 0 ? html`
                        <div>
                            <div style=${subheadStyle}>Conversations</div>
                            <div style=${{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                                ${linkedConvs.map(conv => html`
                                    <a key=${conv.id}
                                       href=${routes.conversation(conv.id)}
                                       style=${{
                                           display: 'flex', alignItems: 'baseline',
                                           justifyContent: 'space-between', gap: '8px',
                                           padding: '6px 8px', borderRadius: layout.borderRadius.sm,
                                           background: colors.surfaceActive, textDecoration: 'none',
                                           color: colors.text, fontSize: typography.size.sm,
                                       }}
                                       class="foreman-conv-row"
                                    >
                                        <span style=${{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                            ${conv.goal || conv.id}
                                        </span>
                                        <span style=${{
                                            fontFamily: typography.fontMono, fontSize: typography.size.xs,
                                            color: colors.textTertiary, flexShrink: 0,
                                        }}>${relativeTime(conv.last_activity || conv.updated_at)}</span>
                                    </a>
                                `)}
                            </div>
                        </div>
                    ` : null}

                    <!-- Punchlist -->
                    <${PunchlistSection} componentId=${component.id} />

                    <!-- Config overrides -->
                    ${hasOverrides ? html`
                        <div>
                            <div style=${subheadStyle}>Config Overrides</div>
                            <div style=${{
                                fontSize: typography.size.xs, color: colors.textSecondary,
                                fontFamily: typography.fontMono, lineHeight: 1.6,
                            }}>
                                ${config.model ? html`model: ${config.model}<br />` : null}
                                ${config.auto_test === false ? html`auto_test: off<br />` : null}
                                ${config.auto_review === false ? html`auto_review: off<br />` : null}
                                ${config.max_turns ? html`max_turns: ${config.max_turns}<br />` : null}
                                ${config.test_command ? html`test: ${config.test_command}<br />` : null}
                            </div>
                        </div>
                    ` : null}
                </div>
            </div>
        </div>
    `;
}

function ComponentsSection({ components, conversations, tasks, componentFilter, onComponentFilter }) {
    const [selectedComponent, setSelectedComponent] = useState(null);

    if (components.length === 0) return null;

    const sectionStyle = {
        display: 'flex',
        flexDirection: 'column',
        gap: '8px',
    };

    const headerStyle = {
        fontSize: typography.size.sm,
        fontWeight: typography.weight.semibold,
        color: colors.textSecondary,
        letterSpacing: '0.06em',
        textTransform: 'uppercase',
        marginBottom: '4px',
    };

    const gridStyle = {
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
        gap: '8px',
    };

    return html`
        <div style=${sectionStyle}>
            <div style=${headerStyle}>Components</div>
            <div style=${gridStyle}>
                ${components.map(comp => html`
                    <${ComponentCard}
                        key=${comp.id}
                        component=${comp}
                        allTasks=${tasks}
                        onClick=${setSelectedComponent}
                    />
                `)}
            </div>
        </div>

        <${ComponentPanel}
            component=${selectedComponent}
            conversations=${conversations}
            allTasks=${tasks}
            onClose=${() => setSelectedComponent(null)}
            onFilterByComponent=${onComponentFilter}
        />
    `;
}

// ---------------------------------------------------------------------------
// Conversations section — project-level only (component_id === null)
// ---------------------------------------------------------------------------

function ConversationsSection({ conversations }) {
    // Only show project-level conversations (not linked to a component)
    const projectConvs = conversations.filter(c => !c.component_id);
    if (projectConvs.length === 0) return null;

    const [expanded, setExpanded] = useState(false);

    const sectionStyle = {
        display: 'flex',
        flexDirection: 'column',
        gap: '8px',
    };

    const headerStyle = {
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        cursor: 'pointer',
    };

    const titleStyle = {
        fontSize: typography.size.sm,
        fontWeight: typography.weight.semibold,
        color: colors.textSecondary,
        letterSpacing: '0.06em',
        textTransform: 'uppercase',
    };

    const listStyle = {
        display: 'flex',
        flexDirection: 'column',
        gap: '4px',
    };

    const rowStyle = {
        display: 'flex',
        alignItems: 'baseline',
        justifyContent: 'space-between',
        gap: '12px',
        padding: '8px 12px',
        borderRadius: layout.borderRadius.md,
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        textDecoration: 'none',
        color: colors.text,
        transition: `background ${animation.durationFast}`,
    };

    // Show 3 most recent as peek when collapsed
    const peekConvs = projectConvs.slice(0, 3);
    const displayConvs = expanded ? projectConvs : peekConvs;
    const hasMore = projectConvs.length > 3;

    return html`
        <div style=${sectionStyle}>
            <div style=${headerStyle} onClick=${() => setExpanded(e => !e)}>
                <span style=${titleStyle}>Conversations · ${projectConvs.length}</span>
                <span style=${{
                    fontSize: typography.size.xs,
                    color: colors.textTertiary,
                }}>${expanded ? 'Collapse ▴' : 'Expand ▾'}</span>
            </div>
            ${!expanded ? html`
                <div style=${listStyle}>
                    ${peekConvs.map(conv => html`
                        <a key=${conv.id}
                           href=${routes.conversation(conv.id)}
                           style=${{
                               display: 'flex',
                               alignItems: 'baseline',
                               gap: '8px',
                               padding: '4px 0',
                               textDecoration: 'none',
                               color: colors.textSecondary,
                               fontSize: typography.size.sm,
                           }}
                           class="foreman-conv-row"
                        >
                            <span style=${{ color: colors.textTertiary, flexShrink: 0 }}>💬</span>
                            <span style=${{
                                flex: 1, overflow: 'hidden',
                                textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                            }}>${conv.goal || conv.id}</span>
                        </a>
                    `)}
                    ${hasMore ? html`
                        <span style=${{
                            fontSize: typography.size.xs,
                            color: colors.textTertiary,
                            paddingLeft: '22px',
                        }}>+${projectConvs.length - 3} more</span>
                    ` : null}
                </div>
            ` : html`
                <div style=${listStyle}>
                    ${projectConvs.map(conv => html`
                        <a key=${conv.id}
                           href=${routes.conversation(conv.id)}
                           style=${rowStyle}
                           class="foreman-conv-row"
                        >
                            <span style=${{
                                flex: 1,
                                overflow: 'hidden',
                                textOverflow: 'ellipsis',
                                whiteSpace: 'nowrap',
                                fontSize: typography.size.sm,
                            }}>
                                ${conv.goal || conv.id}
                            </span>
                            <span style=${{
                                fontFamily: typography.fontMono,
                                fontSize: typography.size.xs,
                                color: colors.textTertiary,
                                flexShrink: 0,
                            }}>${relativeTime(conv.last_activity || conv.updated_at)}</span>
                        </a>
                    `)}
                </div>
            `}
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Chain pop-out overlay — vertical mini-DAG
// ---------------------------------------------------------------------------

function ChainOverlay({ chainIds, anchorTaskId, allTasks, onClose }) {
    useEffect(() => {
        const onKey = (e) => { if (e.key === 'Escape') onClose(); };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [onClose]);

    // Build ordered chain from already-loaded tasks — no API call needed
    const chain = chainIds.map(id => allTasks.find(t => t.id === id)).filter(Boolean);

    // Detect multi-component chain (show component tag per node when spans >1 component)
    const componentIds = new Set(chain.filter(t => t.component_id).map(t => t.component_id));
    const multiComponent = componentIds.size > 1;

    // Node color by status — per spec: green=completed/merged, blue=working, yellow=needs-review/failed, grey=ready/cancelled
    const nodeColor = (s) => {
        if (s === 'completed' || s === 'merged') return colors.green;
        if (s === 'working' || s === 'rate-limited' || s === 'turns-exhausted') return colors.blue;
        if (s === 'needs-review' || s === 'failed') return colors.yellow;
        return colors.textTertiary; // ready, cancelled, queued
    };

    const overlayStyle = {
        position: 'fixed',
        inset: 0,
        background: 'rgba(16, 17, 20, 0.75)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1000,
    };

    const panelStyle = {
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.lg,
        padding: '20px',
        minWidth: '340px',
        maxWidth: '500px',
        width: '90%',
        maxHeight: '80vh',
        overflowY: 'auto',
        display: 'flex',
        flexDirection: 'column',
    };

    const headerStyle = {
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        marginBottom: '20px',
    };

    const titleStyle = {
        fontSize: typography.size.sm,
        fontWeight: typography.weight.semibold,
        color: colors.textSecondary,
        letterSpacing: '0.06em',
        textTransform: 'uppercase',
        display: 'flex',
        alignItems: 'center',
        gap: '6px',
    };

    const closeBtnStyle = {
        background: 'none',
        border: 'none',
        color: colors.textTertiary,
        cursor: 'pointer',
        fontSize: '20px',
        lineHeight: 1,
        padding: '0 4px',
        borderRadius: layout.borderRadius.sm,
    };

    return html`
        <div style=${overlayStyle} onClick=${(e) => { e.stopPropagation(); onClose(); }}>
            <style>${`
                @keyframes foreman-chain-dot-pulse {
                    0%, 100% { opacity: 1; transform: scale(1); }
                    50%       { opacity: 0.5; transform: scale(0.75); }
                }
            `}</style>
            <div style=${panelStyle} onClick=${e => e.stopPropagation()}>
                <div style=${headerStyle}>
                    <span style=${titleStyle}>⛓ Chain${chain.length ? ` · ${chain.length}` : ''}</span>
                    <button style=${closeBtnStyle} onClick=${onClose} title="Close (Esc)">×</button>
                </div>

                <div style=${{ display: 'flex', flexDirection: 'column' }}>
                        ${chain.map((task, i) => {
                            const color = nodeColor(task.status);
                            const isActive = task.status === 'working';
                            const isCurrent = task.id === anchorTaskId;
                            const goal = task.goal || task.id;
                            const displayGoal = goal.length > 52 ? goal.slice(0, 51) + '…' : goal;
                            const compLabel = multiComponent && task.component_id
                                ? task.component_id.split('/').pop()
                                : null;

                            return html`
                                <div key=${task.id} style=${{ display: 'flex', alignItems: 'stretch' }}>

                                    <!-- Left: dot + vertical connector -->
                                    <div style=${{
                                        display: 'flex',
                                        flexDirection: 'column',
                                        alignItems: 'center',
                                        width: '20px',
                                        flexShrink: 0,
                                        marginRight: '12px',
                                    }}>
                                        <div style=${{
                                            width: '10px',
                                            height: '10px',
                                            borderRadius: '50%',
                                            background: color,
                                            flexShrink: 0,
                                            marginTop: '11px',
                                            ...(isActive ? {
                                                animation: 'foreman-chain-dot-pulse 1.4s ease-in-out infinite',
                                            } : {}),
                                        }} />
                                        ${i < chain.length - 1 ? html`
                                            <div style=${{
                                                width: '2px',
                                                flex: 1,
                                                minHeight: '12px',
                                                background: colors.border,
                                                margin: '4px 0',
                                            }} />
                                        ` : null}
                                    </div>

                                    <!-- Right: node card -->
                                    <a href=${routes.task(task.id)}
                                       style=${{
                                           flex: 1,
                                           display: 'flex',
                                           flexDirection: 'column',
                                           gap: '3px',
                                           padding: '8px 10px',
                                           marginBottom: i < chain.length - 1 ? '2px' : '0',
                                           borderRadius: layout.borderRadius.md,
                                           background: isCurrent ? colors.surfaceActive : colors.bg,
                                           border: `1px solid ${isCurrent ? color + '55' : colors.border}`,
                                           textDecoration: 'none',
                                           transition: `background ${animation.durationFast}`,
                                           width: '100%',
                                           boxSizing: 'border-box',
                                       }}
                                       class="foreman-chain-node"
                                       onClick=${onClose}
                                    >
                                        <div style=${{
                                            display: 'flex',
                                            alignItems: 'center',
                                            gap: '6px',
                                            justifyContent: 'space-between',
                                        }}>
                                            <span style=${{
                                                fontSize: typography.size.sm,
                                                color: colors.text,
                                                overflow: 'hidden',
                                                textOverflow: 'ellipsis',
                                                whiteSpace: 'nowrap',
                                                flex: 1,
                                            }}>${displayGoal}</span>

                                            ${compLabel ? html`
                                                <span style=${{
                                                    fontSize: typography.size.xs,
                                                    color: colors.accent,
                                                    background: colors.accentBg,
                                                    border: `1px solid rgba(124, 90, 246, 0.25)`,
                                                    borderRadius: '4px',
                                                    padding: '1px 6px',
                                                    whiteSpace: 'nowrap',
                                                    flexShrink: 0,
                                                    lineHeight: '16px',
                                                }}>${compLabel}</span>
                                            ` : null}
                                        </div>

                                        <span style=${{
                                            fontSize: typography.size.xs,
                                            color,
                                            fontFamily: typography.fontMono,
                                        }}>${task.status || 'queued'}</span>
                                    </a>
                                </div>
                            `;
                        })}
                    </div>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Task Panel — slide-out triage panel
// ---------------------------------------------------------------------------

function TaskPanel({ taskId, onClose }) {
    const [isMobile, setIsMobile] = useState(() => window.innerWidth < 640);

    // Track viewport width for mobile/desktop layout
    useEffect(() => {
        const check = () => setIsMobile(window.innerWidth < 640);
        window.addEventListener('resize', check);
        return () => window.removeEventListener('resize', check);
    }, []);

    // Escape key to dismiss
    useEffect(() => {
        if (!taskId) return;
        const onKey = (e) => { if (e.key === 'Escape') onClose(); };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [taskId, onClose]);

    if (!taskId) return null;

    // Panel slides in from right on desktop, up from bottom on mobile
    const panelStyle = isMobile ? {
        position: 'fixed',
        left: 0,
        right: 0,
        bottom: 0,
        height: '65vh',
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: `${layout.borderRadius.lg} ${layout.borderRadius.lg} 0 0`,
        boxShadow: '0 -8px 40px rgba(0,0,0,0.5)',
        zIndex: 500,
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        animation: `foreman-slide-up ${animation.durationNormal} ${animation.easing}`,
    } : {
        position: 'fixed',
        top: 0,
        right: 0,
        bottom: 0,
        width: 'clamp(420px, 33vw, 560px)',
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderLeft: `1px solid ${colors.border}`,
        boxShadow: '-8px 0 40px rgba(0,0,0,0.4)',
        zIndex: 500,
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        animation: `foreman-slide-right ${animation.durationNormal} ${animation.easing}`,
    };

    const headerStyle = {
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '12px 16px',
        borderBottom: `1px solid ${colors.border}`,
        flexShrink: 0,
    };

    const closeBtnStyle = {
        background: 'none',
        border: 'none',
        color: colors.textTertiary,
        cursor: 'pointer',
        fontSize: '20px',
        lineHeight: 1,
        padding: '2px 6px',
        borderRadius: layout.borderRadius.sm,
    };

    const backdropStyle = {
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.4)',
        zIndex: 499,
    };

    return html`
        <div>
            <!-- Inject keyframe animations once -->
            <style>${`
                @keyframes foreman-slide-right {
                    from { transform: translateX(100%); opacity: 0; }
                    to   { transform: translateX(0);    opacity: 1; }
                }
                @keyframes foreman-slide-up {
                    from { transform: translateY(100%); opacity: 0; }
                    to   { transform: translateY(0);    opacity: 1; }
                }
            `}</style>

            <!-- Backdrop -->
            <div style=${backdropStyle} onClick=${onClose} />

            <!-- Panel -->
            <div style=${panelStyle}>
                <div style=${headerStyle}>
                    <span style=${{ flex: 1 }} />
                    <button style=${closeBtnStyle} onClick=${onClose} title="Close (Esc)">×</button>
                </div>

                <div style=${{
                    flex: 1,
                    overflowY: 'auto',
                    padding: '16px',
                }}>
                    <${TaskView} id=${taskId} mode="compact" onClose=${onClose} />
                </div>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Task row
// ---------------------------------------------------------------------------

function PRTag({ task }) {
    const prUrl = task.pr_url
        || (task.artifacts && task.artifacts.find && task.artifacts.find(a => a.type === 'pr_url')?.ref);
    if (!prUrl) return null;
    if (typeof prUrl !== 'string' || (!prUrl.startsWith('https://') && !prUrl.startsWith('http://'))) return null;

    return html`
        <a href=${prUrl} target="_blank" rel="noopener" style=${{
            display: 'inline-flex',
            alignItems: 'center',
            fontFamily: typography.fontMono,
            fontSize: typography.size.xs,
            color: colors.accent,
            background: colors.accentBg,
            border: `1px solid rgba(124, 90, 246, 0.25)`,
            borderRadius: '4px',
            padding: '1px 7px',
            lineHeight: '18px',
            textDecoration: 'none',
            whiteSpace: 'nowrap',
        }}>PR ↗</a>
    `;
}

function TaskRowWithChain({ task, chainMap, allTasks, conversations, components, onSelect }) {
    const [showChain, setShowChain] = useState(false);
    const handleCloseChain = useCallback(() => setShowChain(false), []);
    const chain = chainMap.get(task.id);

    // Component name for badge
    const compName = task.component_id
        ? (components.find(c => c.id === task.component_id)?.name || task.component_id.split('/').pop())
        : null;

    const rowStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        padding: '7px 0',
        borderBottom: `1px solid ${colors.border}22`,
        minWidth: 0,
        cursor: 'pointer',
    };

    const goalStyle = {
        flex: 1,
        fontSize: typography.size.sm,
        color: colors.text,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        minWidth: 0,
    };

    const tagsRowStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '5px',
        flexShrink: 0,
    };

    const taskShortId = task.id.includes('/') ? task.id.split('/').slice(1).join('/') : task.id;
    const displayId = taskShortId.length > 22 ? taskShortId.slice(0, 21) + '…' : taskShortId;

    const handleRowClick = (e) => {
        // Don't open panel if clicking a link or button inside the row
        if (e.target.closest('a') || e.target.closest('button')) return;
        if (onSelect) onSelect(task.id);
    };

    return html`
        <div style=${rowStyle} class="foreman-task-row" onClick=${handleRowClick}>
            <${StatusDot} status=${task.status} />

            <span style=${goalStyle}>
                ${task.goal || task.id}
            </span>

            <div style=${tagsRowStyle}>
                <span style=${{
                    fontFamily: typography.fontMono,
                    fontSize: typography.size.xs,
                    color: colors.textTertiary,
                    whiteSpace: 'nowrap',
                }} title=${task.id}>${displayId}</span>

                ${compName ? html`
                    <span style=${{
                        fontSize: typography.size.xs,
                        color: colors.textTertiary,
                        background: colors.surfaceActive,
                        border: '1px solid ' + colors.borderSubtle,
                        borderRadius: layout.borderRadius.pill,
                        padding: '1px 7px',
                        whiteSpace: 'nowrap',
                        lineHeight: '16px',
                    }}>${compName}</span>
                ` : null}

                ${task.conversation_id ? html`
                    <a href=${routes.conversation(task.conversation_id)}
                       onClick=${e => e.stopPropagation()}
                       style=${{
                        fontFamily: typography.fontMono,
                        fontSize: typography.size.xs,
                        color: colors.blue,
                        textDecoration: 'none',
                    }} title=${'View conversation: ' + task.conversation_id}>💬</a>
                ` : null}

                ${chain ? html`
                    <${ChainBadge}
                        position=${chain.position}
                        total=${chain.total}
                        onClick=${(e) => { e.stopPropagation(); setShowChain(true); }}
                    />
                    ${showChain ? html`
                        <${ChainOverlay}
                            chainIds=${chain.chainIds}
                            anchorTaskId=${task.id}
                            allTasks=${allTasks}
                            onClose=${handleCloseChain}
                        />
                    ` : null}
                ` : null}

                <${PRTag} task=${task} />

                <span style=${{
                    fontFamily: typography.fontMono,
                    fontSize: typography.size.xs,
                    color: colors.textTertiary,
                    whiteSpace: 'nowrap',
                }}>${relativeTime(task.last_activity || task.updated_at)}</span>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// New Task Panel — slide-out form to create and dispatch a task
// ---------------------------------------------------------------------------

const SLUG_RE = /^[a-z0-9][a-z0-9-]*[a-z0-9]$/;

function NewTaskPanel({ projectId, components, onClose, onCreated }) {
    const [isMobile, setIsMobile] = useState(() => window.innerWidth < 640);
    const [goal, setGoal] = useState('');
    const [slug, setSlug] = useState('');
    const [spec, setSpec] = useState('');
    const [model, setModel] = useState('sonnet');
    const [componentId, setComponentId] = useState('');
    const [autoTest, setAutoTest] = useState(true);
    const [autoReview, setAutoReview] = useState(true);

    // Slug validation state: 'empty' | 'invalid' | 'checking' | 'available' | 'taken'
    const [slugState, setSlugState] = useState('empty');
    const [slugReason, setSlugReason] = useState('');
    const [submitting, setSubmitting] = useState(false);
    const [submitError, setSubmitError] = useState('');

    const goalInputRef = useRef(null);
    const checkAbortRef = useRef(null);

    useEffect(() => {
        const check = () => setIsMobile(window.innerWidth < 640);
        window.addEventListener('resize', check);
        return () => window.removeEventListener('resize', check);
    }, []);

    useEffect(() => {
        const onKey = (e) => { if (e.key === 'Escape') onClose(); };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [onClose]);

    // Autofocus goal on open
    useEffect(() => {
        if (goalInputRef.current) goalInputRef.current.focus();
    }, []);

    const handleSlugChange = (val) => {
        setSlug(val);
        setSlugReason('');
        if (!val) {
            setSlugState('empty');
        } else if (!SLUG_RE.test(val)) {
            setSlugState('invalid');
        } else {
            setSlugState('valid'); // valid format but not checked yet
        }
    };

    const handleSlugBlur = async () => {
        if (slugState !== 'valid' && slugState !== 'available' && slugState !== 'taken') return;
        if (!SLUG_RE.test(slug)) return;

        // Cancel previous check
        if (checkAbortRef.current) checkAbortRef.current = false;
        const token = {};
        checkAbortRef.current = token;

        setSlugState('checking');
        try {
            const res = await api.checkTaskSlug(projectId, slug);
            if (token !== checkAbortRef.current) return; // stale
            if (res.available) {
                setSlugState('available');
            } else {
                setSlugState('taken');
                setSlugReason(res.reason || 'Already in use');
            }
        } catch {
            if (token === checkAbortRef.current) setSlugState('valid');
        }
    };

    const canDispatch = goal.trim() && slugState === 'available' && !submitting;

    const handleSubmit = async (e) => {
        e.preventDefault();
        if (!canDispatch) return;
        setSubmitting(true);
        setSubmitError('');
        try {
            const task = await api.createProjectTask(projectId, {
                slug,
                goal: goal.trim(),
                spec: spec.trim() || undefined,
                model,
                auto_test: autoTest,
                auto_review: autoReview,
                component_id: componentId || undefined,
            });
            onCreated(task);
        } catch (err) {
            setSubmitError(err.message || 'Failed to dispatch task');
            setSubmitting(false);
        }
    };

    const panelStyle = isMobile ? {
        position: 'fixed', left: 0, right: 0, bottom: 0,
        height: '85vh', background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: `${layout.borderRadius.lg} ${layout.borderRadius.lg} 0 0`,
        boxShadow: '0 -8px 40px rgba(0,0,0,0.5)', zIndex: 500,
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
        animation: `foreman-slide-up ${animation.durationNormal} ${animation.easing}`,
    } : {
        position: 'fixed', top: 0, right: 0, bottom: 0,
        width: 'clamp(380px, 35vw, 540px)', background: colors.surface,
        border: `1px solid ${colors.border}`,
        boxShadow: '-8px 0 40px rgba(0,0,0,0.4)', zIndex: 500,
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
        animation: `foreman-slide-right ${animation.durationNormal} ${animation.easing}`,
    };

    const headerStyle = {
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '14px 20px', borderBottom: `1px solid ${colors.border}`, flexShrink: 0,
    };

    const bodyStyle = {
        flex: 1, overflowY: 'auto', padding: '20px',
        display: 'flex', flexDirection: 'column', gap: '18px',
    };

    const labelStyle = {
        fontSize: typography.size.xs, fontWeight: typography.weight.semibold,
        color: colors.textTertiary, letterSpacing: '0.06em', textTransform: 'uppercase',
        marginBottom: '6px', display: 'block',
    };

    const inputStyle = (hasError) => ({
        width: '100%', boxSizing: 'border-box',
        background: colors.bg, border: `1px solid ${hasError ? colors.red : colors.border}`,
        borderRadius: layout.borderRadius.md, padding: '8px 10px',
        fontSize: typography.size.sm, color: colors.text,
        fontFamily: typography.fontBody, outline: 'none',
    });

    const hintStyle = (color) => ({
        fontSize: typography.size.xs, color: color || colors.textTertiary,
        marginTop: '5px',
    });

    const toggleBtnStyle = (active) => ({
        flex: 1, padding: '6px 0', textAlign: 'center',
        fontSize: typography.size.sm, fontWeight: typography.weight.medium,
        cursor: 'pointer', border: 'none', borderRadius: layout.borderRadius.sm,
        background: active ? colors.accentBg : 'transparent',
        color: active ? colors.accent : colors.textTertiary,
        transition: `background ${animation.durationFast}, color ${animation.durationFast}`,
    });

    const checkboxRowStyle = {
        display: 'flex', alignItems: 'center', gap: '8px',
        fontSize: typography.size.sm, color: colors.textSecondary, cursor: 'pointer',
    };

    const footerStyle = {
        padding: '14px 20px', borderTop: `1px solid ${colors.border}`,
        display: 'flex', gap: '10px', flexShrink: 0,
    };

    const dispatchBtnStyle = {
        flex: 1, padding: '9px 0', borderRadius: layout.borderRadius.md,
        border: 'none', cursor: canDispatch ? 'pointer' : 'not-allowed',
        background: canDispatch ? colors.accent : colors.surfaceActive,
        color: canDispatch ? '#fff' : colors.textTertiary,
        fontSize: typography.size.sm, fontWeight: typography.weight.semibold,
        transition: `background ${animation.durationFast}`,
    };

    const cancelBtnStyle = {
        padding: '9px 16px', borderRadius: layout.borderRadius.md,
        border: `1px solid ${colors.border}`, cursor: 'pointer',
        background: 'transparent', color: colors.textSecondary,
        fontSize: typography.size.sm,
    };

    const closeBtnStyle = {
        background: 'none', border: 'none', color: colors.textTertiary,
        cursor: 'pointer', fontSize: '20px', lineHeight: 1, padding: '2px 6px',
        borderRadius: layout.borderRadius.sm,
    };

    const backdropStyle = {
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)', zIndex: 499,
    };

    // Slug status indicator
    const slugIndicator = () => {
        if (slugState === 'checking') return html`<span style=${{ ...hintStyle(colors.textTertiary) }}>Checking…</span>`;
        if (slugState === 'available') return html`<span style=${{ ...hintStyle(colors.green) }}>✓ Available</span>`;
        if (slugState === 'taken') return html`<span style=${{ ...hintStyle(colors.red) }}>✗ ${slugReason}</span>`;
        if (slugState === 'invalid') return html`<span style=${{ ...hintStyle(colors.red) }}>Lowercase letters, numbers, and hyphens only</span>`;
        return html`<span style=${{ ...hintStyle() }}>Becomes your git branch name</span>`;
    };

    return html`
        <div>
            <div style=${backdropStyle} onClick=${onClose} />
            <div style=${panelStyle}>
                <div style=${headerStyle}>
                    <span style=${{
                        fontSize: typography.size.md, fontWeight: typography.weight.semibold,
                        color: colors.text,
                    }}>New Task</span>
                    <button style=${closeBtnStyle} onClick=${onClose} title="Close (Esc)">×</button>
                </div>

                <form style=${bodyStyle} onSubmit=${handleSubmit}>
                    <!-- Goal -->
                    <div>
                        <label style=${labelStyle}>Goal *</label>
                        <input
                            ref=${goalInputRef}
                            type="text"
                            placeholder="What should CC do?"
                            value=${goal}
                            onInput=${e => setGoal(e.target.value)}
                            style=${inputStyle(false)}
                        />
                    </div>

                    <!-- Slug -->
                    <div>
                        <label style=${labelStyle}>Branch / Slug *</label>
                        <input
                            type="text"
                            placeholder="my-task-slug"
                            value=${slug}
                            onInput=${e => handleSlugChange(e.target.value)}
                            onBlur=${handleSlugBlur}
                            style=${inputStyle(slugState === 'invalid' || slugState === 'taken')}
                        />
                        ${slugIndicator()}
                    </div>

                    <!-- Spec -->
                    <div>
                        <label style=${labelStyle}>Spec</label>
                        <textarea
                            placeholder="Detailed instructions for CC (optional)"
                            rows="6"
                            value=${spec}
                            onInput=${e => setSpec(e.target.value)}
                            style=${{
                                ...inputStyle(false),
                                resize: 'vertical', minHeight: '100px',
                                fontFamily: typography.fontBody, lineHeight: 1.5,
                            }}
                        />
                    </div>

                    <!-- Model toggle -->
                    <div>
                        <label style=${labelStyle}>Model</label>
                        <div style=${{
                            display: 'flex', gap: '4px', padding: '4px',
                            background: colors.bg, border: `1px solid ${colors.border}`,
                            borderRadius: layout.borderRadius.md,
                        }}>
                            <button type="button" style=${toggleBtnStyle(model === 'sonnet')}
                                onClick=${() => setModel('sonnet')}>Sonnet</button>
                            <button type="button" style=${toggleBtnStyle(model === 'opus')}
                                onClick=${() => setModel('opus')}>Opus</button>
                        </div>
                    </div>

                    <!-- Component -->
                    ${components.length > 0 ? html`
                        <div>
                            <label style=${labelStyle}>Component</label>
                            <div style=${{ position: 'relative' }}>
                                <select
                                    value=${componentId}
                                    onChange=${e => setComponentId(e.target.value)}
                                    style=${{
                                        ...inputStyle(false),
                                        appearance: 'none', WebkitAppearance: 'none',
                                        paddingRight: '28px', cursor: 'pointer',
                                    }}
                                >
                                    <option value="">No component</option>
                                    ${components.map(c => html`
                                        <option key=${c.id} value=${c.id}>${c.name || c.id}</option>
                                    `)}
                                </select>
                                <span style=${{
                                    position: 'absolute', right: '10px', top: '50%',
                                    transform: 'translateY(-50%)', fontSize: '10px',
                                    color: colors.textTertiary, pointerEvents: 'none',
                                }}>▾</span>
                            </div>
                        </div>
                    ` : null}

                    <!-- Auto Test / Auto Review -->
                    <div style=${{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                        <label style=${checkboxRowStyle}>
                            <input type="checkbox" checked=${autoTest}
                                onChange=${e => setAutoTest(e.target.checked)} />
                            Auto Test
                        </label>
                        <label style=${checkboxRowStyle}>
                            <input type="checkbox" checked=${autoReview}
                                onChange=${e => setAutoReview(e.target.checked)} />
                            Auto Review
                        </label>
                    </div>

                    <!-- Submit error -->
                    ${submitError ? html`
                        <div style=${{
                            padding: '10px 12px', borderRadius: layout.borderRadius.md,
                            background: colors.redBg, border: `1px solid ${colors.red}44`,
                            color: colors.red, fontSize: typography.size.sm,
                        }}>${submitError}</div>
                    ` : null}
                </form>

                <div style=${footerStyle}>
                    <button style=${dispatchBtnStyle} disabled=${!canDispatch}
                        onClick=${handleSubmit}>
                        ${submitting ? 'Dispatching…' : 'Dispatch Task'}
                    </button>
                    <button style=${cancelBtnStyle} onClick=${onClose}>Cancel</button>
                </div>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Tasks section — filter bar + grouped list
// ---------------------------------------------------------------------------

const ALL_STATUSES = ['working', 'completed', 'failed', 'needs-review', 'ready', 'cancelled',
    'rate-limited', 'turns-exhausted'];

function FilterBar({ statusFilter, componentFilter, components, onStatusFilter, onComponentFilter }) {
    const selectStyle = {
        fontFamily: typography.fontBody,
        fontSize: typography.size.sm,
        color: colors.textSecondary,
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.sm,
        padding: '4px 8px',
        cursor: 'pointer',
        outline: 'none',
        appearance: 'none',
        WebkitAppearance: 'none',
        paddingRight: '24px',
    };

    const wrapStyle = {
        position: 'relative',
        display: 'inline-block',
    };

    const arrowStyle = {
        position: 'absolute',
        right: '7px',
        top: '50%',
        transform: 'translateY(-50%)',
        fontSize: '9px',
        color: colors.textTertiary,
        pointerEvents: 'none',
    };

    return html`
        <div style=${{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
            <div style=${wrapStyle}>
                <select
                    style=${selectStyle}
                    value=${statusFilter}
                    onChange=${e => onStatusFilter(e.target.value)}
                >
                    <option value="">All statuses</option>
                    ${ALL_STATUSES.map(s => html`<option key=${s} value=${s}>${s}</option>`)}
                </select>
                <span style=${arrowStyle}>▾</span>
            </div>

            ${components.length > 0 ? html`
                <div style=${wrapStyle}>
                    <select
                        style=${selectStyle}
                        value=${componentFilter}
                        onChange=${e => onComponentFilter(e.target.value)}
                    >
                        <option value="">All components</option>
                        ${components.map(c => html`
                            <option key=${c.id} value=${c.id}>${c.name || c.id}</option>
                        `)}
                        <option value="__none__">No component</option>
                    </select>
                    <span style=${arrowStyle}>▾</span>
                </div>
            ` : null}
        </div>
    `;
}

function TasksSection({ tasks, components, conversations, chainMap, statusFilter, componentFilter,
    onStatusFilter, onComponentFilter, onTaskSelect, onNewTask }) {

    // Filter
    let filtered = tasks;
    if (statusFilter) filtered = filtered.filter(t => t.status === statusFilter);
    if (componentFilter === '__none__') {
        filtered = filtered.filter(t => !t.component_id);
    } else if (componentFilter) {
        filtered = filtered.filter(t => t.component_id === componentFilter);
    }

    // Sort by last_activity descending — flat list, no grouping
    const ts = (t) => {
        const raw = t.last_activity || t.updated_at || t.created_at || '1970-01-01T00:00:00Z';
        return new Date(raw.endsWith('Z') ? raw : raw + 'Z').getTime();
    };
    filtered = [...filtered].sort((a, b) => ts(b) - ts(a));

    const sectionStyle = {
        display: 'flex',
        flexDirection: 'column',
    };

    const sectionHeaderStyle = {
        fontSize: typography.size.sm,
        fontWeight: typography.weight.semibold,
        color: colors.textSecondary,
        letterSpacing: '0.06em',
        textTransform: 'uppercase',
    };

    const emptyStyle = {
        padding: '32px 0',
        textAlign: 'center',
        color: colors.textTertiary,
        fontSize: typography.size.sm,
    };

    return html`
        <div style=${sectionStyle}>
            <div style=${{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
                <div style=${sectionHeaderStyle}>Tasks · ${filtered.length}</div>
                <button
                    onClick=${onNewTask}
                    style=${{
                        fontSize: typography.size.xs, fontWeight: typography.weight.medium,
                        color: colors.textTertiary, background: 'none',
                        border: `1px solid ${colors.border}`, borderRadius: layout.borderRadius.sm,
                        padding: '3px 10px', cursor: 'pointer',
                        transition: `color ${animation.durationFast}, border-color ${animation.durationFast}`,
                    }}
                    class="foreman-new-task-btn"
                    title="Create a new task"
                >+ New Task</button>
            </div>

            <${FilterBar}
                statusFilter=${statusFilter}
                componentFilter=${componentFilter}
                components=${components}
                onStatusFilter=${onStatusFilter}
                onComponentFilter=${onComponentFilter}
            />

            ${filtered.length === 0 ? html`
                <div style=${emptyStyle}>No tasks match the current filters</div>
            ` : filtered.map(task => html`
                <${TaskRowWithChain}
                    key=${task.id}
                    task=${task}
                    chainMap=${chainMap}
                    allTasks=${tasks}
                    conversations=${conversations}
                    components=${components}
                    onSelect=${onTaskSelect}
                />
            `)}
        </div>
    `;
}

// ---------------------------------------------------------------------------
// ProjectView — root component
// ---------------------------------------------------------------------------

export function ProjectView({ id }) {
    const [project, setProject] = useState(null);
    const [tasks, setTasks] = useState([]);
    const [components, setComponents] = useState([]);
    const [conversations, setConversations] = useState([]);
    const [error, setError] = useState(null);
    const [loading, setLoading] = useState(true);
    const [selectedTaskId, setSelectedTaskId] = useState(null);

    const [statusFilter, setStatusFilter] = useState('');
    const [componentFilter, setComponentFilter] = useState('');
    const [showNewTask, setShowNewTask] = useState(false);

    const chainMap = buildChainMap(tasks);

    const handleTaskCreated = useCallback((task) => {
        setShowNewTask(false);
        load();
        navigate(`/task/${encodeURIComponent(task.id || task.task_id)}`);
    }, [load]);

    const load = useCallback(async () => {
        try {
            const [proj, taskList, compList, convList] = await Promise.all([
                api.getProject(id),
                api.getTasks({ project_id: id }),
                api.getComponents(id),
                api.getConversations({ project_id: id }).catch(() => []),
            ]);
            setProject(proj);
            setTasks(taskList);
            setComponents(compList);
            setConversations(convList);
            setError(null);
            setLoading(false);
        } catch (e) {
            setError(e.message || 'Failed to load project');
            setLoading(false);
        }
    }, [id]);

    useEffect(() => {
        setLoading(true);
        load();
    }, [load]);

    useEffect(() => {
        const timer = setInterval(load, POLL_INTERVAL_MS);
        return () => clearInterval(timer);
    }, [load]);

    // ---- Styles ----
    const pageStyle = {
        display: 'flex',
        flexDirection: 'column',
        gap: '32px',
    };

    const backLinkStyle = {
        display: 'inline-flex',
        alignItems: 'center',
        gap: '5px',
        fontSize: typography.size.sm,
        color: colors.textTertiary,
        textDecoration: 'none',
        marginBottom: '-8px',
        transition: `color ${animation.durationFast}`,
    };

    const headerStyle = {
        display: 'flex',
        alignItems: 'baseline',
        justifyContent: 'space-between',
        paddingBottom: '16px',
        borderBottom: `1px solid ${colors.border}`,
        gap: '12px',
    };

    const titleStyle = {
        fontFamily: typography.fontBody,
        fontSize: typography.size['2xl'],
        fontWeight: typography.weight.semibold,
        color: colors.text,
        margin: 0,
        letterSpacing: '-0.02em',
        flex: 1,
        wordBreak: 'break-word',
    };

    const repoTagStyle = {
        fontFamily: typography.fontMono,
        fontSize: typography.size.sm,
        color: colors.textTertiary,
        flexShrink: 0,
    };

    const errorStyle = {
        padding: '24px',
        borderRadius: layout.borderRadius.md,
        background: colors.redBg,
        border: `1px solid ${colors.red}44`,
        color: colors.red,
        fontSize: typography.size.sm,
    };

    const loadingStyle = {
        padding: '60px 0',
        textAlign: 'center',
        color: colors.textTertiary,
        fontSize: typography.size.sm,
    };

    if (loading) {
        return html`
            <div style=${pageStyle}>
                <a href=${routes.landing()} style=${backLinkStyle} class="foreman-back-link">← Projects</a>
                <div style=${loadingStyle}>Loading…</div>
            </div>
        `;
    }

    if (error) {
        return html`
            <div style=${pageStyle}>
                <a href=${routes.landing()} style=${backLinkStyle} class="foreman-back-link">← Projects</a>
                <div style=${{
                    ...errorStyle,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    gap: '16px',
                }}>
                    <span>Error: ${error}</span>
                    <button onClick=${load} style=${{
                        padding: '4px 12px',
                        borderRadius: layout.borderRadius.sm,
                        background: `${colors.red}22`,
                        border: `1px solid ${colors.red}44`,
                        color: colors.red,
                        fontSize: typography.size.sm,
                        cursor: 'pointer',
                        flexShrink: 0,
                    }}>Retry</button>
                </div>
            </div>
        `;
    }

    const repoShort = project?.repo ? project.repo.split('/').pop() : '';

    return html`
        <div style=${pageStyle}>
            <!-- Back navigation -->
            <a href=${routes.landing()} style=${backLinkStyle} class="foreman-back-link">← Projects</a>

            <!-- Project header -->
            <div style=${headerStyle}>
                <h1 style=${titleStyle}>${project?.id || id}</h1>
                ${repoShort ? html`<span style=${repoTagStyle}>${repoShort}</span>` : null}
            </div>

            <!-- Recent Activity -->
            <${RecentActivity} projectId=${id} />

            <!-- Components (knowledge drawers) -->
            <${ComponentsSection}
                components=${components}
                conversations=${conversations}
                tasks=${tasks}
                componentFilter=${componentFilter}
                onComponentFilter=${setComponentFilter}
            />

            <!-- Project-level conversations (unlinked only) -->
            <${ConversationsSection} conversations=${conversations} />

            <!-- Tasks -->
            <${TasksSection}
                tasks=${tasks}
                components=${components}
                conversations=${conversations}
                chainMap=${chainMap}
                statusFilter=${statusFilter}
                componentFilter=${componentFilter}
                onStatusFilter=${setStatusFilter}
                onComponentFilter=${setComponentFilter}
                onTaskSelect=${setSelectedTaskId}
                onNewTask=${() => setShowNewTask(true)}
            />
        </div>

        <!-- Task Panel slide-out -->
        <${TaskPanel}
            taskId=${selectedTaskId}
            onClose=${() => setSelectedTaskId(null)}
        />

        <!-- New Task Panel slide-out -->
        ${showNewTask ? html`
            <${NewTaskPanel}
                projectId=${id}
                components=${components}
                onClose=${() => setShowNewTask(false)}
                onCreated=${handleTaskCreated}
            />
        ` : null}
    `;
}
