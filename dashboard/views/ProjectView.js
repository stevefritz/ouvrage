// Foreman Project View
// Layout: Recent Activity → Conversations → Tasks
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
import { styles as fkStyles, FormField, FormRow, Toggle } from '../components/FormKit.js';

const html = htm.bind(h);

const POLL_INTERVAL_MS = 15_000;
const ACTIVITY_LIMIT = 15;

// ---------------------------------------------------------------------------
// ControlButtons — pause / resume / stop for project or component
// ---------------------------------------------------------------------------

function ControlButtons({ paused, onPause, onResume, onStop, entityType = 'project' }) {
    const [confirmStop, setConfirmStop] = useState(false);
    const [busy, setBusy] = useState(false);

    const handlePause = async () => {
        setBusy(true);
        try { await onPause(); } finally { setBusy(false); }
    };

    const handleResume = async () => {
        setBusy(true);
        try { await onResume(); } finally { setBusy(false); }
    };

    const handleStop = async () => {
        setBusy(true);
        setConfirmStop(false);
        try { await onStop(); } finally { setBusy(false); }
    };

    const btnBase = {
        display: 'inline-flex', alignItems: 'center',
        padding: '4px 10px', borderRadius: layout.borderRadius.sm,
        fontSize: typography.size.xs, fontWeight: typography.weight.medium,
        fontFamily: typography.fontBody, cursor: busy ? 'not-allowed' : 'pointer',
        border: '1px solid', lineHeight: 1.4, whiteSpace: 'nowrap',
        opacity: busy ? 0.6 : 1, transition: 'opacity 0.15s',
    };

    const pauseBtn = {
        ...btnBase,
        color: colors.yellow,
        background: colors.yellowBg,
        borderColor: `${colors.yellow}44`,
    };

    const resumeBtn = {
        ...btnBase,
        color: colors.green,
        background: colors.greenBg,
        borderColor: `${colors.green}44`,
    };

    const stopBtn = {
        ...btnBase,
        color: colors.red,
        background: colors.redBg,
        borderColor: `${colors.red}44`,
    };

    const label = entityType.charAt(0).toUpperCase() + entityType.slice(1);

    return html`
        <div style=${{ display: 'flex', alignItems: 'center', gap: '6px', flexShrink: 0 }}>
            ${!paused ? html`
                <button
                    style=${pauseBtn}
                    disabled=${busy}
                    onClick=${handlePause}
                    title=${`Pause ${entityType} — blocks new task dispatches. Running tasks finish naturally.`}
                >⏸ Pause</button>
            ` : html`
                <button
                    style=${resumeBtn}
                    disabled=${busy}
                    onClick=${handleResume}
                    title=${`Resume ${entityType} — allows task dispatches again.`}
                >▶ Resume</button>
            `}
            <button
                style=${stopBtn}
                disabled=${busy}
                onClick=${() => setConfirmStop(true)}
                title=${`Stop ${entityType} — pauses AND cancels all running tasks immediately.`}
            >⏹ Stop</button>

            ${confirmStop ? html`
                <div onClick=${() => setConfirmStop(false)} style=${{
                    position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    zIndex: 1000,
                }}>
                    <div onClick=${e => e.stopPropagation()} style=${{
                        background: colors.surface, border: `1px solid ${colors.border}`,
                        borderRadius: layout.borderRadius.lg,
                        padding: '24px', maxWidth: '400px', width: '90%',
                    }}>
                        <h3 style=${{
                            fontFamily: typography.fontBody, fontSize: typography.size.lg,
                            fontWeight: typography.weight.semibold, color: colors.text,
                            margin: '0 0 8px',
                        }}>Stop ${label}?</h3>
                        <p style=${{
                            fontFamily: typography.fontBody, fontSize: typography.size.sm,
                            color: colors.textSecondary, margin: '0 0 20px',
                            lineHeight: typography.lineHeight.normal,
                        }}>This will cancel all running tasks. Continue?</p>
                        <div style=${{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
                            <button onClick=${() => setConfirmStop(false)} style=${{
                                padding: '6px 16px', borderRadius: layout.borderRadius.sm,
                                background: colors.surface, border: `1px solid ${colors.border}`,
                                color: colors.textSecondary, cursor: 'pointer',
                                fontFamily: typography.fontBody, fontSize: typography.size.sm,
                            }}>Cancel</button>
                            <button onClick=${handleStop} style=${{
                                padding: '6px 16px', borderRadius: layout.borderRadius.sm,
                                background: colors.red, border: 'none',
                                color: '#fff', cursor: 'pointer',
                                fontFamily: typography.fontBody, fontSize: typography.size.sm,
                                fontWeight: typography.weight.medium,
                            }}>Stop ${label}</button>
                        </div>
                    </div>
                </div>
            ` : null}
        </div>
    `;
}

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
// Conversations section
// ---------------------------------------------------------------------------

function ConversationsSection({ conversations }) {
    const projectConvs = conversations;
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

    const prNumber = (prUrl.match(/\/pull\/(\d+)/) || [])[1];
    const isMerged = task.pr_status === 'merged';
    const isClosed = task.pr_status === 'closed';

    return html`
        <a href=${prUrl} target="_blank" rel="noopener" style=${{
            display: 'inline-flex',
            alignItems: 'center',
            fontFamily: typography.fontMono,
            fontSize: typography.size.xs,
            color: isMerged ? colors.green : isClosed ? colors.textTertiary : colors.accent,
            background: isMerged ? colors.greenBg : isClosed ? 'rgba(92, 94, 102, 0.12)' : colors.accentBg,
            border: `1px solid ${isMerged ? `${colors.green}44` : isClosed ? 'rgba(92, 94, 102, 0.25)' : 'rgba(124, 90, 246, 0.25)'}`,
            borderRadius: '4px',
            padding: '1px 7px',
            lineHeight: '18px',
            textDecoration: 'none',
            whiteSpace: 'nowrap',
        }}>
            ${isMerged
                ? `PR${prNumber ? ` #${prNumber}` : ''} merged ↗`
                : `PR${prNumber ? ` #${prNumber}` : ''} ↗`}
        </a>
    `;
}

function TaskRowWithChain({ task, chainMap, allTasks, conversations, onSelect }) {
    const [showChain, setShowChain] = useState(false);
    const handleCloseChain = useCallback(() => setShowChain(false), []);
    const chain = chainMap.get(task.id);

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

            <div style=${tagsRowStyle} class="foreman-task-row-tags">
                <span style=${{
                    fontFamily: typography.fontMono,
                    fontSize: typography.size.xs,
                    color: colors.textTertiary,
                    whiteSpace: 'nowrap',
                }} title=${task.id}>${displayId}</span>

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
// Tasks section — filter bar + grouped list
// ---------------------------------------------------------------------------

const ALL_STATUSES = ['working', 'completed', 'failed', 'needs-review', 'ready', 'cancelled',
    'rate-limited', 'turns-exhausted'];

function FilterBar({ statusFilter, onStatusFilter, searchQuery, onSearch }) {
    const [rawSearch, setRawSearch] = useState(searchQuery || '');
    const debounceRef = useRef(null);

    // Keep rawSearch in sync if parent clears searchQuery externally
    useEffect(() => {
        if (!searchQuery) setRawSearch('');
    }, [searchQuery]);

    const handleSearchChange = (e) => {
        const val = e.target.value;
        setRawSearch(val);
        if (debounceRef.current) clearTimeout(debounceRef.current);
        debounceRef.current = setTimeout(() => {
            onSearch(val.trim());
        }, 300);
    };

    const handleClear = () => {
        setRawSearch('');
        if (debounceRef.current) clearTimeout(debounceRef.current);
        onSearch('');
    };

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

    const searchInputStyle = {
        fontFamily: typography.fontBody,
        fontSize: typography.size.sm,
        color: colors.text,
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.sm,
        padding: '4px 28px 4px 28px',
        outline: 'none',
        width: '200px',
        transition: `border-color ${animation.durationFast}`,
    };

    const searchWrapStyle = {
        position: 'relative',
        display: 'inline-flex',
        alignItems: 'center',
        flex: '1 1 auto',
        minWidth: '160px',
        maxWidth: '320px',
    };

    const searchIconStyle = {
        position: 'absolute',
        left: '8px',
        fontSize: '11px',
        color: colors.textTertiary,
        pointerEvents: 'none',
        lineHeight: 1,
    };

    const clearBtnStyle = {
        position: 'absolute',
        right: '6px',
        background: 'none',
        border: 'none',
        cursor: 'pointer',
        color: colors.textTertiary,
        fontSize: '13px',
        lineHeight: 1,
        padding: '2px',
        display: rawSearch ? 'block' : 'none',
    };

    return html`
        <div style=${{ marginBottom: '12px' }}>
            <style>${`
                @media (max-width: 640px) {
                    .foreman-filterbar { flex-direction: column; align-items: stretch !important; }
                    .foreman-filterbar-search { max-width: none !important; width: 100% !important; }
                    .foreman-filterbar-search input { width: 100% !important; box-sizing: border-box; }
                    .foreman-filterbar-dropdowns { flex-wrap: wrap; }
                }
                .foreman-filterbar-search input:focus { border-color: ${colors.accent}; }
            `}</style>
            <div class="foreman-filterbar" style=${{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                <!-- Search input -->
                <div class="foreman-filterbar-search" style=${searchWrapStyle}>
                    <span style=${searchIconStyle}>⌕</span>
                    <input
                        type="text"
                        placeholder="Search tasks..."
                        value=${rawSearch}
                        onInput=${handleSearchChange}
                        style=${{ ...searchInputStyle, width: '100%' }}
                    />
                    <button style=${clearBtnStyle} onClick=${handleClear} title="Clear search">✕</button>
                </div>

                <!-- Dropdowns -->
                <div class="foreman-filterbar-dropdowns" style=${{ display: 'flex', alignItems: 'center', gap: '8px' }}>
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
                </div>
            </div>
        </div>
    `;
}

function SearchResultRow({ result, onTaskSelect }) {
    const handleClick = () => {
        if (result.task_id) {
            navigate(`/task/${result.task_id}`);
        } else if (result.conversation_id) {
            navigate(`/conversations/${result.conversation_id}`);
        }
    };

    const rowStyle = {
        display: 'flex',
        flexDirection: 'column',
        padding: '10px 12px',
        borderRadius: layout.borderRadius.md,
        border: `1px solid ${colors.border}`,
        marginBottom: '6px',
        background: colors.surface,
        cursor: 'pointer',
        transition: `background ${animation.durationFast}`,
    };

    const typeColors = {
        task: colors.accent,
        task_message: colors.blue,
        conversation_message: colors.green,
        chunk: colors.yellow,
    };
    const typeColor = typeColors[result.type] || colors.textTertiary;

    const typeBadgeStyle = {
        display: 'inline-block',
        fontSize: '10px',
        fontWeight: typography.weight.semibold,
        color: typeColor,
        background: `${typeColor}20`,
        borderRadius: layout.borderRadius.sm,
        padding: '1px 6px',
        textTransform: 'uppercase',
        letterSpacing: '0.05em',
        marginRight: '8px',
    };

    const titleStyle = {
        fontSize: typography.size.sm,
        fontWeight: typography.weight.medium,
        color: colors.text,
        marginBottom: '3px',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
    };

    const snippetStyle = {
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        display: '-webkit-box',
        WebkitLineClamp: 2,
        WebkitBoxOrient: 'vertical',
    };

    const metaStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '6px',
        marginBottom: '4px',
    };

    const contextStyle = {
        fontSize: '11px',
        color: colors.textTertiary,
    };

    const typeLabel = result.type === 'task' ? 'task'
        : result.type === 'task_message' ? 'message'
        : result.type === 'conversation_message' ? 'conv message'
        : 'excerpt';

    return html`
        <div
            style=${rowStyle}
            onClick=${handleClick}
            onMouseEnter=${e => e.currentTarget.style.background = colors.surfaceHover}
            onMouseLeave=${e => e.currentTarget.style.background = colors.surface}
        >
            <div style=${metaStyle}>
                <span style=${typeBadgeStyle}>${typeLabel}</span>
                ${result.task_id ? html`<span style=${contextStyle}>${result.task_id}</span>` : null}
            </div>
            ${result.title ? html`<div style=${titleStyle}>${result.title}</div>` : null}
            ${result.snippet ? html`<div style=${snippetStyle}>${result.snippet}</div>` : null}
        </div>
    `;
}

function TasksSection({ tasks, conversations, chainMap, statusFilter, onStatusFilter, onTaskSelect,
    searchQuery, searchResults, searchLoading, onSearch, projectId }) {

    // Filter normal task list (used when no search active)
    let filtered = tasks;
    if (statusFilter) filtered = filtered.filter(t => t.status === statusFilter);

    // Sort by last_activity descending — flat list, no grouping
    const ts = (t) => {
        const raw = t.last_activity || t.updated_at || t.created_at || '1970-01-01T00:00:00Z';
        return new Date(raw.endsWith('Z') ? raw : raw + 'Z').getTime();
    };
    filtered = [...filtered].sort((a, b) => ts(b) - ts(a));

    const isSearchActive = !!searchQuery;

    // When search is active, filter search results by status dropdown too
    let displayTasks = isSearchActive ? (searchResults || []) : filtered;
    if (isSearchActive && statusFilter && searchResults) {
        displayTasks = searchResults.filter(t => t.status === statusFilter);
    }

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
        marginBottom: '8px',
    };

    const emptyStyle = {
        padding: '32px 0',
        textAlign: 'center',
        color: colors.textTertiary,
        fontSize: typography.size.sm,
    };

    const loadingStyle = {
        padding: '20px 0',
        textAlign: 'center',
        color: colors.textTertiary,
        fontSize: typography.size.sm,
    };

    const headerLabel = searchLoading
        ? 'Tasks · …'
        : isSearchActive && searchResults
            ? `Tasks · ${displayTasks.length}`
            : `Tasks · ${filtered.length}`;

    return html`
        <div style=${sectionStyle}>
            <style>${`
                @media (max-width: 640px) {
                    .foreman-task-row { flex-wrap: wrap; }
                    .foreman-task-row-tags { width: 100%; flex-wrap: wrap; margin-top: 2px; }
                }
            `}</style>
            <div style=${sectionHeaderStyle}>${headerLabel}</div>

            <${FilterBar}
                statusFilter=${statusFilter}
                onStatusFilter=${onStatusFilter}
                searchQuery=${searchQuery}
                onSearch=${onSearch}
            />

            ${isSearchActive ? html`
                ${searchLoading ? html`
                    <div style=${loadingStyle}>Searching…</div>
                ` : searchResults && displayTasks.length === 0 ? html`
                    <div style=${emptyStyle}>No results found for "${searchQuery}"</div>
                ` : searchResults ? displayTasks.map(task => html`
                    <${TaskRowWithChain}
                        key=${task.id}
                        task=${task}
                        chainMap=${chainMap}
                        allTasks=${tasks}
                        conversations=${conversations}
                        onSelect=${onTaskSelect}
                    />
                `) : null}
            ` : html`
                ${filtered.length === 0 ? html`
                    <div style=${emptyStyle}>No tasks match the current filters</div>
                ` : filtered.map(task => html`
                    <${TaskRowWithChain}
                        key=${task.id}
                        task=${task}
                        chainMap=${chainMap}
                        allTasks=${tasks}
                        conversations=${conversations}
                        onSelect=${onTaskSelect}
                    />
                `)}
            `}
        </div>
    `;
}

// RepoUrlField — read-only repo URL display with copy button
// ---------------------------------------------------------------------------

function RepoUrlField({ repo }) {
    const [copied, setCopied] = useState(false);

    if (!repo) return null;

    const handleCopy = async () => {
        try {
            await navigator.clipboard.writeText(repo);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
        } catch (_) {}
    };

    return html`
        <${FormField} label="Repository">
            <div style=${{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
            }}>
                <span style=${{
                    flex: 1,
                    fontFamily: typography.fontMono,
                    fontSize: typography.size.sm,
                    color: colors.textSecondary,
                    background: colors.bg,
                    border: `1px solid ${colors.border}`,
                    borderRadius: layout.borderRadius.sm,
                    padding: '6px 10px',
                    wordBreak: 'break-all',
                    lineHeight: '1.4',
                    display: 'block',
                }}>${repo}</span>
                <button
                    type="button"
                    onClick=${handleCopy}
                    title="Copy repository URL"
                    style=${{
                        flexShrink: 0,
                        padding: '6px 10px',
                        borderRadius: layout.borderRadius.sm,
                        background: copied ? colors.greenBg : colors.surfaceHover,
                        border: `1px solid ${copied ? colors.green + '44' : colors.border}`,
                        color: copied ? colors.green : colors.textTertiary,
                        cursor: 'pointer',
                        fontSize: typography.size.xs,
                        fontFamily: typography.fontBody,
                        transition: 'color 0.15s, background 0.15s, border-color 0.15s',
                        whiteSpace: 'nowrap',
                    }}
                >${copied ? 'Copied!' : 'Copy'}</button>
            </div>
        </${FormField}>
    `;
}

// EditProjectPanel — slide-out panel for editing project configuration
// ---------------------------------------------------------------------------

function EditProjectPanel({ project, onClose, onSaved }) {
    const [isMobile, setIsMobile] = useState(() => window.innerWidth < 640);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState(null);

    // Form state — initialized from current project values
    const [defaultBranch, setDefaultBranch] = useState(project.default_branch || 'main');
    const [model, setModel] = useState(project.model || '');
    const [reviewModel, setReviewModel] = useState(project.review_model || '');
    const [setupCommand, setSetupCommand] = useState(project.setup_command || '');
    const [testCommand, setTestCommand] = useState(project.test_command || '');
    const [teardownCommand, setTeardownCommand] = useState(project.teardown_command || '');
    const [maxTurns, setMaxTurns] = useState(project.max_turns != null ? String(project.max_turns) : '');
    const [maxWallClock, setMaxWallClock] = useState(project.max_wall_clock != null ? String(project.max_wall_clock) : '');
    const [autoTest, setAutoTest] = useState(project.auto_test != null ? Boolean(project.auto_test) : true);
    const [autoReview, setAutoReview] = useState(project.auto_review != null ? Boolean(project.auto_review) : true);
    const [autoPr, setAutoPr] = useState(project.auto_pr != null ? Boolean(project.auto_pr) : false);
    const [autoMerge, setAutoMerge] = useState(project.auto_merge != null ? Boolean(project.auto_merge) : false);
    const [reviewIgnorePatterns, setReviewIgnorePatterns] = useState(
        Array.isArray(project.review_ignore_patterns)
            ? project.review_ignore_patterns.join('\n')
            : (project.review_ignore_patterns || '')
    );
    const [envOverrides, setEnvOverrides] = useState(
        project.env_overrides && typeof project.env_overrides === 'object'
            ? JSON.stringify(project.env_overrides, null, 2)
            : (project.env_overrides || '')
    );
    const [envError, setEnvError] = useState(null);
    // null = unchanged (don't include in PATCH), '' = explicitly cleared, string = new value
    const [githubPatOverride, setGithubPatOverride] = useState(null);

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

    const handleSave = async () => {
        setEnvError(null);
        // Validate env_overrides JSON if provided
        let parsedEnv = undefined;
        if (envOverrides.trim()) {
            try {
                parsedEnv = JSON.parse(envOverrides);
            } catch (_) {
                setEnvError('Invalid JSON in env overrides');
                return;
            }
        }

        setSaving(true);
        setError(null);
        try {
            const fields = {
                default_branch: defaultBranch.trim() || 'main',
                model: model || null,
                review_model: reviewModel || null,
                setup_command: setupCommand.trim() || null,
                test_command: testCommand.trim() || null,
                teardown_command: teardownCommand.trim() || null,
                max_turns: maxTurns.trim() ? parseInt(maxTurns, 10) : null,
                max_wall_clock: maxWallClock.trim() ? parseInt(maxWallClock, 10) : null,
                auto_test: autoTest,
                auto_review: autoReview,
                auto_pr: autoPr,
                auto_merge: autoMerge,
                review_ignore_patterns: reviewIgnorePatterns.trim()
                    ? reviewIgnorePatterns.split('\n').map(s => s.trim()).filter(Boolean)
                    : null,
                env_overrides: parsedEnv !== undefined ? parsedEnv : (envOverrides.trim() ? undefined : null),
            };
            // Include github_pat_override only if user changed it
            if (githubPatOverride !== null) {
                fields.github_pat_override = githubPatOverride || null;
            }
            // Remove undefined values (keep nulls — they clear the field)
            Object.keys(fields).forEach(k => fields[k] === undefined && delete fields[k]);

            await api.updateProject(project.id, fields);
            onSaved();
            onClose();
        } catch (e) {
            setError(e.message || 'Save failed');
        } finally {
            setSaving(false);
        }
    };

    const panelStyle = isMobile ? {
        position: 'fixed', left: 0, right: 0, bottom: 0,
        height: '85vh', background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: `${layout.borderRadius.lg} ${layout.borderRadius.lg} 0 0`,
        boxShadow: '0 -8px 40px rgba(0,0,0,0.5)', zIndex: 600,
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
        animation: `foreman-slide-up ${animation.durationNormal} ${animation.easing}`,
    } : {
        position: 'fixed', top: 0, right: 0, bottom: 0,
        width: 'clamp(420px, 35vw, 580px)', background: colors.surface,
        border: `1px solid ${colors.border}`,
        boxShadow: '-8px 0 40px rgba(0,0,0,0.4)', zIndex: 600,
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
        animation: `foreman-slide-right ${animation.durationNormal} ${animation.easing}`,
    };

    const sectionLabelStyle = {
        fontSize: '11px',
        fontWeight: typography.weight.medium,
        color: colors.textTertiary,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        marginBottom: '10px',
        marginTop: '4px',
        paddingBottom: '6px',
        borderBottom: `1px solid ${colors.border}33`,
    };

    const inheritHintStyle = {
        fontSize: '10px',
        color: colors.textTertiary,
        fontStyle: 'italic',
        marginTop: '3px',
    };

    const toggleRowStyle = {
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '8px 0',
        borderBottom: `1px solid ${colors.border}22`,
    };

    const toggleLabelStyle = {
        fontSize: typography.size.sm,
        color: colors.text,
        flex: 1,
    };

    const toggleSubStyle = {
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        marginTop: '2px',
    };

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

            <!-- Backdrop -->
            <div style=${{
                position: 'fixed', inset: 0,
                background: 'rgba(0,0,0,0.4)', zIndex: 599,
            }} onClick=${onClose} />

            <!-- Panel -->
            <div style=${panelStyle}>
                <!-- Header -->
                <div style=${{
                    display: 'flex', alignItems: 'center', gap: '10px',
                    padding: '14px 16px', borderBottom: `1px solid ${colors.border}`,
                    flexShrink: 0,
                }}>
                    <span style=${{
                        fontFamily: typography.fontBody,
                        fontSize: typography.size.base,
                        fontWeight: typography.weight.semibold,
                        color: colors.text,
                        flex: 1,
                    }}>Edit Project Config</span>
                    <span style=${{
                        fontFamily: typography.fontMono,
                        fontSize: typography.size.xs,
                        color: colors.textTertiary,
                    }}>${project.id}</span>
                    <button
                        onClick=${onClose}
                        style=${{
                            background: 'none', border: 'none',
                            color: colors.textTertiary, cursor: 'pointer',
                            fontSize: '20px', lineHeight: 1,
                            padding: '2px 6px',
                            borderRadius: layout.borderRadius.sm,
                        }}
                        title="Close (Esc)"
                    >×</button>
                </div>

                <!-- Error banner -->
                ${error ? html`
                    <div style=${{
                        padding: '10px 16px',
                        background: colors.redBg,
                        borderBottom: `1px solid ${colors.red}44`,
                        color: colors.red,
                        fontSize: typography.size.sm,
                        flexShrink: 0,
                    }}>${error}</div>
                ` : null}

                <!-- Body -->
                <div style=${{
                    flex: 1, overflowY: 'auto',
                    padding: '16px',
                    display: 'flex', flexDirection: 'column', gap: '20px',
                }}>

                    <!-- Git section -->
                    <div>
                        <div style=${sectionLabelStyle}>Git</div>
                        <${RepoUrlField} repo=${project.repo} />
                        <${FormField} label="Default Branch">
                            <input
                                type="text"
                                value=${defaultBranch}
                                onInput=${e => setDefaultBranch(e.target.value)}
                                style=${fkStyles.input}
                                placeholder="main"
                            />
                            <div style=${inheritHintStyle}>Inherits to tasks as merge target</div>
                        </${FormField}>
                        <${FormField} label="GitHub PAT (project-specific)">
                            <input
                                type="password"
                                value=${githubPatOverride ?? ''}
                                onInput=${e => setGithubPatOverride(e.target.value)}
                                style=${fkStyles.input}
                                placeholder="ghp_… (leave blank to use instance PAT)"
                                autoComplete="new-password"
                            />
                            <div style=${{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '4px' }}>
                                ${(() => {
                                    const patIsSet = githubPatOverride !== null ? Boolean(githubPatOverride) : Boolean(project.github_pat_override);
                                    return html`
                                        <span style=${{
                                            fontSize: '11px',
                                            color: patIsSet ? colors.accent : colors.textTertiary,
                                            fontStyle: patIsSet ? 'normal' : 'italic',
                                            flex: 1,
                                        }}>
                                            ${patIsSet ? 'Using project PAT' : 'Using instance PAT (default)'}
                                        </span>
                                        ${patIsSet ? html`
                                            <button
                                                type="button"
                                                onClick=${() => setGithubPatOverride('')}
                                                style=${{
                                                    background: 'none', border: 'none',
                                                    color: colors.textTertiary, cursor: 'pointer',
                                                    fontSize: '11px', padding: '0',
                                                    textDecoration: 'underline',
                                                }}
                                            >Clear</button>
                                        ` : null}
                                    `;
                                })()}
                            </div>
                        </${FormField}>
                    </div>

                    <!-- Models section -->
                    <div>
                        <div style=${sectionLabelStyle}>Models</div>
                        <${FormRow}>
                            <${FormField} label="Worker Model">
                                <select
                                    value=${model}
                                    onChange=${e => setModel(e.target.value)}
                                    style=${fkStyles.select}
                                >
                                    <option value="">System default</option>
                                    <option value="sonnet">sonnet</option>
                                    <option value="opus">opus</option>
                                </select>
                                <div style=${inheritHintStyle}>Inherits to tasks</div>
                            </${FormField}>
                            <${FormField} label="Review Model">
                                <select
                                    value=${reviewModel}
                                    onChange=${e => setReviewModel(e.target.value)}
                                    style=${fkStyles.select}
                                >
                                    <option value="">System default (opus)</option>
                                    <option value="sonnet">sonnet</option>
                                    <option value="opus">opus</option>
                                </select>
                                <div style=${inheritHintStyle}>Inherits to tasks</div>
                            </${FormField}>
                        </${FormRow}>
                    </div>

                    <!-- Commands section -->
                    <div>
                        <div style=${sectionLabelStyle}>Commands</div>
                        <${FormField} label="Setup Command">
                            <textarea
                                value=${setupCommand}
                                onInput=${e => setSetupCommand(e.target.value)}
                                style=${{ ...fkStyles.input, resize: 'vertical', minHeight: '60px', fontFamily: typography.fontMono, fontSize: typography.size.xs }}
                                placeholder="e.g. npm install"
                                rows="2"
                            />
                            <div style=${inheritHintStyle}>Run after worktree creation — inherits to tasks</div>
                        </${FormField}>
                        <${FormField} label="Test Command">
                            <textarea
                                value=${testCommand}
                                onInput=${e => setTestCommand(e.target.value)}
                                style=${{ ...fkStyles.input, resize: 'vertical', minHeight: '60px', fontFamily: typography.fontMono, fontSize: typography.size.xs }}
                                placeholder="e.g. pytest tests/"
                                rows="2"
                            />
                            <div style=${inheritHintStyle}>Used by test gate — inherits to tasks</div>
                        </${FormField}>
                        <${FormField} label="Teardown Command">
                            <textarea
                                value=${teardownCommand}
                                onInput=${e => setTeardownCommand(e.target.value)}
                                style=${{ ...fkStyles.input, resize: 'vertical', minHeight: '60px', fontFamily: typography.fontMono, fontSize: typography.size.xs }}
                                placeholder="e.g. docker compose down"
                                rows="2"
                            />
                            <div style=${inheritHintStyle}>Run on worktree cleanup</div>
                        </${FormField}>
                    </div>

                    <!-- Limits section -->
                    <div>
                        <div style=${sectionLabelStyle}>Limits</div>
                        <${FormRow}>
                            <${FormField} label="Max Turns">
                                <input
                                    type="number"
                                    value=${maxTurns}
                                    onInput=${e => setMaxTurns(e.target.value)}
                                    style=${fkStyles.input}
                                    placeholder="System default"
                                    min="1"
                                />
                                <div style=${inheritHintStyle}>Inherits to tasks</div>
                            </${FormField}>
                            <${FormField} label="Max Wall Clock (minutes)">
                                <input
                                    type="number"
                                    value=${maxWallClock}
                                    onInput=${e => setMaxWallClock(e.target.value)}
                                    style=${fkStyles.input}
                                    placeholder="System default"
                                    min="1"
                                />
                                <div style=${inheritHintStyle}>Inherits to tasks</div>
                            </${FormField}>
                        </${FormRow}>
                    </div>

                    <!-- Automation section -->
                    <div>
                        <div style=${sectionLabelStyle}>Automation</div>

                        <div style=${toggleRowStyle}>
                            <div style=${{ flex: 1 }}>
                                <div style=${toggleLabelStyle}>Auto Test</div>
                                <div style=${toggleSubStyle}>Run test gate after each session — inherits to tasks</div>
                            </div>
                            <${Toggle}
                                checked=${autoTest}
                                onChange=${() => setAutoTest(v => !v)}
                            />
                        </div>

                        <div style=${toggleRowStyle}>
                            <div style=${{ flex: 1 }}>
                                <div style=${toggleLabelStyle}>Auto Review</div>
                                <div style=${toggleSubStyle}>Run Opus self-review gate after test pass — inherits to tasks</div>
                            </div>
                            <${Toggle}
                                checked=${autoReview}
                                onChange=${() => setAutoReview(v => !v)}
                            />
                        </div>

                        <div style=${toggleRowStyle}>
                            <div style=${{ flex: 1 }}>
                                <div style=${toggleLabelStyle}>Auto PR</div>
                                <div style=${toggleSubStyle}>Create PR when chain tail passes all gates — inherits to tasks. Mutually exclusive with Auto Merge.</div>
                            </div>
                            <${Toggle}
                                checked=${autoPr}
                                onChange=${() => { setAutoPr(v => !v); if (!autoPr) setAutoMerge(false); }}
                            />
                        </div>

                        <div style=${toggleRowStyle}>
                            <div style=${{ flex: 1 }}>
                                <div style=${toggleLabelStyle}>Auto Merge</div>
                                <div style=${toggleSubStyle}>Merge branch on gate pass — inherits to tasks. Mutually exclusive with Auto PR.</div>
                            </div>
                            <${Toggle}
                                checked=${autoMerge}
                                onChange=${() => { setAutoMerge(v => !v); if (!autoMerge) setAutoPr(false); }}
                            />
                        </div>
                    </div>

                    <!-- Advanced section -->
                    <div>
                        <div style=${sectionLabelStyle}>Advanced</div>

                        <${FormField} label="Review Ignore Patterns">
                            <textarea
                                value=${reviewIgnorePatterns}
                                onInput=${e => setReviewIgnorePatterns(e.target.value)}
                                style=${{ ...fkStyles.input, resize: 'vertical', minHeight: '72px', fontFamily: typography.fontMono, fontSize: typography.size.xs }}
                                placeholder="*.lock${'\n'}vendor/"
                                rows="3"
                            />
                            <div style=${inheritHintStyle}>One glob pattern per line — excludes files from reviewer diffs</div>
                        </${FormField}>

                        <${FormField} label="Env Overrides">
                            <textarea
                                value=${envOverrides}
                                onInput=${e => { setEnvOverrides(e.target.value); setEnvError(null); }}
                                style=${{ ...fkStyles.input, resize: 'vertical', minHeight: '100px', fontFamily: typography.fontMono, fontSize: typography.size.xs }}
                                placeholder='{"NODE_ENV": "test"}'
                                rows="4"
                            />
                            ${envError ? html`
                                <div style=${{ fontSize: typography.size.xs, color: colors.red, marginTop: '4px' }}>${envError}</div>
                            ` : html`
                                <div style=${inheritHintStyle}>JSON key-value pairs written to .env.testing in worktree</div>
                            `}
                        </${FormField}>
                    </div>

                </div>

                <!-- Footer actions -->
                <div style=${{
                    display: 'flex', alignItems: 'center', gap: '8px',
                    padding: '12px 16px',
                    borderTop: `1px solid ${colors.border}`,
                    flexShrink: 0,
                }}>
                    <button
                        onClick=${handleSave}
                        disabled=${saving}
                        style=${{
                            ...fkStyles.buttonPrimary,
                            padding: '7px 18px',
                            fontSize: typography.size.sm,
                            opacity: saving ? 0.6 : 1,
                            cursor: saving ? 'not-allowed' : 'pointer',
                        }}
                    >${saving ? 'Saving…' : 'Save'}</button>
                    <button
                        onClick=${onClose}
                        disabled=${saving}
                        style=${{
                            ...fkStyles.button,
                            padding: '7px 14px',
                            fontSize: typography.size.sm,
                        }}
                    >Cancel</button>
                </div>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// ProjectView — root component
// ---------------------------------------------------------------------------

export function ProjectView({ id }) {
    const [project, setProject] = useState(null);
    const [tasks, setTasks] = useState([]);
    const [conversations, setConversations] = useState([]);
    const [error, setError] = useState(null);
    const [loading, setLoading] = useState(true);
    const [selectedTaskId, setSelectedTaskId] = useState(null);
    const [showEditPanel, setShowEditPanel] = useState(false);
    const [saveToast, setSaveToast] = useState(false);

    const [statusFilter, setStatusFilter] = useState('');

    const _searchStorageKey = `foreman_search_${id}`;
    const [searchQuery, setSearchQuery] = useState(() => {
        try { return localStorage.getItem(_searchStorageKey) || ''; } catch (_) { return ''; }
    });
    const [searchResults, setSearchResults] = useState(null);
    const [searchLoading, setSearchLoading] = useState(false);

    // Delete project state
    const [showDeleteOverlay, setShowDeleteOverlay] = useState(false);
    const [deleteConfirmText, setDeleteConfirmText] = useState('');
    const [deleting, setDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState(null);

    const chainMap = buildChainMap(tasks);

    const load = useCallback(async () => {
        try {
            const [proj, taskList, convList] = await Promise.all([
                api.getProject(id),
                api.getTasks({ project_id: id }),
                api.getConversations({ project: id }).catch(() => []),
            ]);
            setProject(proj);
            setTasks(taskList);
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

    const handleSearch = useCallback(async (query) => {
        setSearchQuery(query);
        try { localStorage.setItem(_searchStorageKey, query); } catch (_) {}
        if (!query) {
            try { localStorage.removeItem(_searchStorageKey); } catch (_) {}
            setSearchResults(null);
            setSearchLoading(false);
            return;
        }
        setSearchLoading(true);
        try {
            const result = await api.search({ q: query, project_id: id, limit: 20 });
            setSearchResults(result.results || []);
        } catch (e) {
            setSearchResults([]);
        } finally {
            setSearchLoading(false);
        }
    }, [id, _searchStorageKey]);

    // Re-run search on mount if a persisted query exists
    useEffect(() => {
        if (searchQuery) {
            handleSearch(searchQuery);
        }
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    const handleDeleteProject = async () => {
        if (!project || deleteConfirmText !== project.id) return;
        setDeleting(true);
        setDeleteError(null);
        try {
            await api.deleteProject(id);
            navigate('/');
        } catch (e) {
            setDeleteError(e.message || 'Failed to delete project');
            setDeleting(false);
        }
    };

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
        alignItems: 'center',
        flexWrap: 'wrap',
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
        flex: '1 0 auto',
        minWidth: '200px',
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
                <div style=${{ display: 'flex', alignItems: 'center', gap: '8px', marginLeft: 'auto', flexShrink: 0 }}>
                    <button
                        onClick=${() => setShowEditPanel(true)}
                        title="Edit project configuration"
                        style=${{
                            background: 'transparent',
                            border: `1px solid ${colors.border}`,
                            borderRadius: layout.borderRadius.sm,
                            color: colors.textTertiary,
                            cursor: 'pointer',
                            fontSize: '14px',
                            padding: '2px 7px',
                            lineHeight: 1,
                            transition: 'color 120ms, border-color 120ms',
                            flexShrink: 0,
                        }}
                    >✎</button>
                    <${ControlButtons}
                        paused=${project.paused}
                        onPause=${async () => { await api.pauseProject(id); await load(); }}
                        onResume=${async () => { await api.resumeProject(id); await load(); }}
                        onStop=${async () => { await api.stopProject(id); await load(); }}
                        entityType="project"
                    />
                    <a
                        href=${routes.taskNew(id)}
                        style=${{
                            padding: '6px 14px',
                            borderRadius: layout.borderRadius.md,
                            background: colors.blue,
                            border: 'none',
                            color: '#fff',
                            fontSize: typography.size.sm,
                            fontFamily: typography.fontBody,
                            fontWeight: typography.weight.medium,
                            cursor: 'pointer',
                            whiteSpace: 'nowrap',
                            textDecoration: 'none',
                            display: 'inline-block',
                        }}
                    >+ New Task</a>
                </div>
            </div>

            <!-- Recent Activity -->
            <${RecentActivity} projectId=${id} />


            <!-- Project-level conversations -->
            <${ConversationsSection} conversations=${conversations} />

            <!-- Tasks -->
            <${TasksSection}
                tasks=${tasks}
                conversations=${conversations}
                chainMap=${chainMap}
                statusFilter=${statusFilter}
                onStatusFilter=${setStatusFilter}
                onTaskSelect=${setSelectedTaskId}
                searchQuery=${searchQuery}
                searchResults=${searchResults}
                searchLoading=${searchLoading}
                onSearch=${handleSearch}
                projectId=${id}
            />

            <!-- Danger Zone -->
            <div style=${{
                border: `1px solid ${colors.red}44`,
                borderRadius: layout.borderRadius.lg,
                padding: '20px 24px',
                marginTop: '8px',
            }}>
                <div style=${{
                    fontSize: typography.size.xs,
                    fontWeight: typography.weight.semibold,
                    color: colors.red,
                    letterSpacing: '0.08em',
                    textTransform: 'uppercase',
                    marginBottom: '12px',
                }}>Danger Zone</div>

                ${(() => {
                    const workingTasks = tasks.filter(t => t.status === 'working');
                    const hasWorking = workingTasks.length > 0;
                    return html`
                        <div style=${{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '16px', flexWrap: 'wrap' }}>
                            <div>
                                <div style=${{ fontSize: typography.size.sm, fontWeight: typography.weight.medium, color: colors.text, marginBottom: '2px' }}>
                                    Delete this project
                                </div>
                                <div style=${{ fontSize: typography.size.xs, color: colors.textTertiary }}>
                                    ${hasWorking
                                        ? `Cannot delete — ${workingTasks.length} task(s) are still working. Cancel them first.`
                                        : 'Permanently removes the project and its working directory from disk.'}
                                </div>
                            </div>
                            <button
                                onClick=${() => { setShowDeleteOverlay(true); setDeleteConfirmText(''); setDeleteError(null); }}
                                disabled=${hasWorking}
                                style=${{
                                    padding: '6px 16px',
                                    borderRadius: layout.borderRadius.sm,
                                    background: hasWorking ? colors.surface : colors.redBg,
                                    border: `1px solid ${hasWorking ? colors.border : colors.red}44`,
                                    color: hasWorking ? colors.textTertiary : colors.red,
                                    fontSize: typography.size.sm,
                                    cursor: hasWorking ? 'not-allowed' : 'pointer',
                                    fontFamily: typography.fontBody,
                                    opacity: hasWorking ? 0.6 : 1,
                                    whiteSpace: 'nowrap',
                                }}
                                title=${hasWorking ? 'Cancel all working tasks before deleting' : 'Delete this project'}
                            >Delete Project</button>
                        </div>
                    `;
                })()}
            </div>
        </div>

        <!-- Task Panel slide-out -->
        <${TaskPanel}
            taskId=${selectedTaskId}
            onClose=${() => setSelectedTaskId(null)}
        />

        <!-- Project Edit Panel -->
        ${showEditPanel && project ? html`
            <${EditProjectPanel}
                project=${project}
                onClose=${() => setShowEditPanel(false)}
                onSaved=${async () => {
                    await load();
                    setSaveToast(true);
                    setTimeout(() => setSaveToast(false), 3000);
                }}
            />
        ` : null}

        <!-- Save success toast -->
        ${saveToast ? html`
            <div style=${{
                position: 'fixed',
                bottom: '24px',
                left: '50%',
                transform: 'translateX(-50%)',
                background: colors.green,
                color: '#fff',
                padding: '8px 20px',
                borderRadius: layout.borderRadius.md,
                fontSize: typography.size.sm,
                fontFamily: typography.fontBody,
                fontWeight: typography.weight.medium,
                zIndex: 700,
                boxShadow: '0 4px 20px rgba(0,0,0,0.4)',
                pointerEvents: 'none',
            }}>Project settings saved</div>
        ` : null}

        <!-- Delete Project confirmation overlay -->
        ${showDeleteOverlay ? html`
            <div onClick=${() => setShowDeleteOverlay(false)} style=${{
                position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                zIndex: 1000,
            }}>
                <div onClick=${e => e.stopPropagation()} style=${{
                    background: colors.surface,
                    border: `1px solid ${colors.border}`,
                    borderRadius: layout.borderRadius.lg,
                    padding: '28px',
                    maxWidth: '440px',
                    width: '90%',
                }}>
                    <h3 style=${{
                        fontFamily: typography.fontBody,
                        fontSize: typography.size.lg,
                        fontWeight: typography.weight.semibold,
                        color: colors.text,
                        margin: '0 0 8px',
                    }}>Delete Project?</h3>
                    <p style=${{
                        fontFamily: typography.fontBody,
                        fontSize: typography.size.sm,
                        color: colors.textSecondary,
                        margin: '0 0 16px',
                        lineHeight: typography.lineHeight.normal,
                    }}>
                        This will permanently delete the project and remove its working directory from disk.
                        This action cannot be undone.
                    </p>
                    <p style=${{
                        fontFamily: typography.fontBody,
                        fontSize: typography.size.sm,
                        color: colors.textSecondary,
                        margin: '0 0 6px',
                    }}>
                        Type <strong style=${{ fontFamily: typography.fontMono, color: colors.text }}>${project?.id}</strong> to confirm:
                    </p>
                    <input
                        type="text"
                        value=${deleteConfirmText}
                        onInput=${e => setDeleteConfirmText(e.target.value)}
                        placeholder=${project?.id}
                        style=${{
                            width: '100%',
                            boxSizing: 'border-box',
                            background: colors.surfaceActive,
                            border: `1px solid ${colors.border}`,
                            borderRadius: layout.borderRadius.sm,
                            color: colors.text,
                            fontSize: typography.size.sm,
                            fontFamily: typography.fontMono,
                            padding: '8px 10px',
                            outline: 'none',
                            marginBottom: '16px',
                        }}
                        autoFocus
                    />
                    ${deleteError ? html`
                        <div style=${{
                            background: colors.redBg,
                            border: `1px solid ${colors.red}44`,
                            borderRadius: layout.borderRadius.sm,
                            padding: '8px 12px',
                            color: colors.red,
                            fontSize: typography.size.xs,
                            marginBottom: '16px',
                        }}>${deleteError}</div>
                    ` : null}
                    <div style=${{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
                        <button
                            onClick=${() => setShowDeleteOverlay(false)}
                            style=${{
                                padding: '6px 16px',
                                borderRadius: layout.borderRadius.sm,
                                background: colors.surface,
                                border: `1px solid ${colors.border}`,
                                color: colors.textSecondary,
                                cursor: 'pointer',
                                fontFamily: typography.fontBody,
                                fontSize: typography.size.sm,
                            }}
                        >Cancel</button>
                        <button
                            onClick=${handleDeleteProject}
                            disabled=${deleting || deleteConfirmText !== project?.id}
                            style=${{
                                padding: '6px 16px',
                                borderRadius: layout.borderRadius.sm,
                                background: colors.red,
                                border: 'none',
                                color: '#fff',
                                cursor: (deleting || deleteConfirmText !== project?.id) ? 'not-allowed' : 'pointer',
                                opacity: (deleting || deleteConfirmText !== project?.id) ? 0.5 : 1,
                                fontFamily: typography.fontBody,
                                fontSize: typography.size.sm,
                                fontWeight: typography.weight.medium,
                            }}
                        >${deleting ? 'Deleting…' : 'Delete Project'}</button>
                    </div>
                </div>
            </div>
        ` : null}
    `;
}
