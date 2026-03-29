// Foreman Project View
// Layout: Recent Activity → Components (knowledge drawers) → Conversations → Tasks
// Spec: foreman-design conversation, messages [6-9]

import { h } from 'https://esm.sh/preact@10.25.4';
import { useState, useEffect, useCallback, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, layout, statusColors, animation } from '../tokens.js';
import { routes } from '../router.js';
import { api } from '../api.js';
import { StatusDot } from '../components/StatusDot.js';
import { ChainBadge } from '../components/ChainBadge.js';
import { relativeTime } from '../components/utils.js';
import { TaskView } from './TaskView.js';

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
// Component drawer — knowledge card for a component
// ---------------------------------------------------------------------------

function PunchlistSection({ componentId, componentName }) {
    const [items, setItems] = useState(null);
    const [loading, setLoading] = useState(true);
    const [newText, setNewText] = useState('');
    const [adding, setAdding] = useState(false);
    const [selectedIds, setSelectedIds] = useState(new Set());
    const [confirmDeleteId, setConfirmDeleteId] = useState(null);
    const [busyIds, setBusyIds] = useState(new Set());
    const [error, setError] = useState(null);

    const loadItems = useCallback(() => {
        api.getPunchlist(componentId)
            .then(data => { setItems(data); setLoading(false); })
            .catch(() => { setItems([]); setLoading(false); });
    }, [componentId]);

    useEffect(() => { loadItems(); }, [loadItems]);

    const nextStatus = (s) => {
        if (s === 'open') return 'claimed';
        if (s === 'claimed') return 'done';
        return 'open';
    };

    const statusColor = (s) => {
        if (s === 'done') return colors.green;
        if (s === 'claimed') return colors.yellow;
        return colors.textTertiary;
    };

    const statusIcon = (s) => {
        if (s === 'done') return '✓';
        if (s === 'claimed') return '◉';
        return '○';
    };

    const handleToggleStatus = useCallback(async (item) => {
        if (busyIds.has(item.id)) return;
        const next = nextStatus(item.status);
        setBusyIds(prev => new Set([...prev, item.id]));
        try {
            await api.updatePunchlistStatus(componentId, item.id, next);
            setItems(prev => prev.map(i => i.id === item.id ? { ...i, status: next } : i));
            if (next !== 'open') {
                setSelectedIds(prev => { const s = new Set(prev); s.delete(item.id); return s; });
            }
        } catch (e) {
            setError(e.message);
        } finally {
            setBusyIds(prev => { const s = new Set(prev); s.delete(item.id); return s; });
        }
    }, [componentId, busyIds]);

    const handleAdd = useCallback(async () => {
        const text = newText.trim();
        if (!text || adding) return;
        setAdding(true);
        setError(null);
        try {
            const item = await api.addPunchlistItem(componentId, text);
            setItems(prev => [...(prev || []), item]);
            setNewText('');
        } catch (e) {
            setError(e.message);
        } finally {
            setAdding(false);
        }
    }, [componentId, newText, adding]);

    const handleDelete = useCallback(async (itemId) => {
        setBusyIds(prev => new Set([...prev, itemId]));
        setError(null);
        try {
            await api.deletePunchlistItem(componentId, itemId);
            setItems(prev => prev.filter(i => i.id !== itemId));
            setSelectedIds(prev => { const s = new Set(prev); s.delete(itemId); return s; });
            setConfirmDeleteId(null);
        } catch (e) {
            setError(e.message);
        } finally {
            setBusyIds(prev => { const s = new Set(prev); s.delete(itemId); return s; });
        }
    }, [componentId]);

    const handleToggleSelect = useCallback((itemId) => {
        setSelectedIds(prev => {
            const s = new Set(prev);
            if (s.has(itemId)) s.delete(itemId); else s.add(itemId);
            return s;
        });
    }, []);

    const handleCreateTask = useCallback(() => {
        const selectedItems = (items || []).filter(i => selectedIds.has(i.id));
        const scaffold = {
            componentId,
            componentName: componentName || componentId,
            items: selectedItems.map(i => ({ id: i.id, text: i.item || i.text || '' })),
        };
        sessionStorage.setItem('foreman-punchlist-scaffold', JSON.stringify(scaffold));
        window.location.hash = '#/task/new';
    }, [items, selectedIds, componentId, componentName]);

    const handleKeyDown = useCallback((e) => {
        if (e.key === 'Enter') handleAdd();
    }, [handleAdd]);

    if (loading) {
        return html`<div style=${{ color: colors.textTertiary, fontSize: typography.size.xs, padding: '4px 0' }}>Loading punchlist…</div>`;
    }

    const displayItems = items || [];
    const openCount = displayItems.filter(i => i.status === 'open').length;
    const selectedCount = selectedIds.size;

    const btnBase = {
        background: 'transparent',
        border: `1px solid ${colors.border}`,
        borderRadius: '4px',
        color: colors.textTertiary,
        cursor: 'pointer',
        fontFamily: 'inherit',
        fontSize: typography.size.xs,
        padding: '2px 8px',
        lineHeight: '18px',
    };

    const btnPrimary = {
        ...btnBase,
        background: colors.accent,
        border: `1px solid ${colors.accent}`,
        color: '#fff',
    };

    const btnDanger = {
        ...btnBase,
        color: colors.red,
        borderColor: colors.red,
    };

    return html`
        <div>
            <!-- Header row with count + multi-select button -->
            <div style=${{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                marginBottom: '8px',
            }}>
                <div style=${{
                    fontSize: typography.size.sm,
                    fontWeight: typography.weight.semibold,
                    color: colors.textSecondary,
                }}>Punchlist · ${openCount} open</div>
                ${selectedCount > 0 ? html`
                    <button
                        style=${btnPrimary}
                        onClick=${handleCreateTask}
                        title="Opens the Task Create form with selected items pre-filled as spec + checklist. Component auto-assigned."
                    >Create task from ${selectedCount} selected</button>
                ` : null}
            </div>

            ${error ? html`<div style=${{
                fontSize: typography.size.xs,
                color: colors.red,
                marginBottom: '6px',
            }}>${error}</div>` : null}

            <!-- Item list -->
            ${displayItems.length === 0 ? html`
                <div style=${{ color: colors.textTertiary, fontSize: typography.size.xs, fontStyle: 'italic', marginBottom: '8px' }}>No punchlist items</div>
            ` : html`
                <div style=${{ display: 'flex', flexDirection: 'column', gap: '4px', marginBottom: '8px' }}>
                    ${displayItems.map(item => html`
                        <div key=${item.id} style=${{
                            display: 'flex',
                            alignItems: 'center',
                            gap: '6px',
                            fontSize: typography.size.sm,
                        }}>
                            <!-- Checkbox (open items only) -->
                            ${item.status === 'open' ? html`
                                <input
                                    type="checkbox"
                                    checked=${selectedIds.has(item.id)}
                                    onChange=${() => handleToggleSelect(item.id)}
                                    style=${{ accentColor: colors.accent, flexShrink: 0, cursor: 'pointer', margin: 0 }}
                                />
                            ` : html`<span style=${{ width: '13px', flexShrink: 0 }}></span>`}

                            <!-- Status icon (clickable) -->
                            <span
                                onClick=${() => handleToggleStatus(item)}
                                title="Click to cycle status"
                                style=${{
                                    color: busyIds.has(item.id) ? colors.textTertiary : statusColor(item.status),
                                    fontSize: typography.size.xs,
                                    flexShrink: 0,
                                    width: '14px',
                                    textAlign: 'center',
                                    cursor: 'pointer',
                                    userSelect: 'none',
                                    opacity: busyIds.has(item.id) ? 0.5 : 1,
                                }}
                            >${statusIcon(item.status)}</span>

                            <!-- Item text -->
                            <span style=${{
                                color: item.status === 'done' ? colors.textTertiary : colors.textSecondary,
                                textDecoration: item.status === 'done' ? 'line-through' : 'none',
                                flex: 1,
                                lineHeight: 1.4,
                                minWidth: 0,
                            }}>${item.item || item.text || item.description}</span>

                            <!-- Task link -->
                            ${item.task_id ? html`
                                <a href=${routes.task(item.task_id)} style=${{
                                    fontFamily: typography.fontMono,
                                    fontSize: typography.size.xs,
                                    color: colors.accent,
                                    textDecoration: 'none',
                                    flexShrink: 0,
                                }}>↗</a>
                            ` : null}

                            <!-- Delete button / confirm -->
                            ${confirmDeleteId === item.id ? html`
                                <span style=${{ display: 'inline-flex', alignItems: 'center', gap: '4px', flexShrink: 0 }}>
                                    <span style=${{ fontSize: typography.size.xs, color: colors.textTertiary }}>Delete?</span>
                                    <button
                                        style=${btnDanger}
                                        onClick=${() => handleDelete(item.id)}
                                        disabled=${busyIds.has(item.id)}
                                    >Yes</button>
                                    <button
                                        style=${btnBase}
                                        onClick=${() => setConfirmDeleteId(null)}
                                    >Cancel</button>
                                </span>
                            ` : html`
                                <button
                                    style=${{
                                        ...btnBase,
                                        padding: '1px 5px',
                                        fontSize: '10px',
                                        opacity: 0.4,
                                        flexShrink: 0,
                                    }}
                                    onClick=${() => setConfirmDeleteId(item.id)}
                                    title="Delete item"
                                >×</button>
                            `}
                        </div>
                    `)}
                </div>
            `}

            <!-- Add item input -->
            <div style=${{ display: 'flex', gap: '6px', alignItems: 'center' }}>
                <input
                    type="text"
                    value=${newText}
                    onInput=${(e) => setNewText(e.target.value)}
                    onKeyDown=${handleKeyDown}
                    placeholder="Add punchlist item…"
                    style=${{
                        flex: 1,
                        background: 'transparent',
                        border: `1px solid ${colors.border}`,
                        borderRadius: '4px',
                        color: colors.text,
                        fontFamily: 'inherit',
                        fontSize: typography.size.xs,
                        padding: '3px 8px',
                        outline: 'none',
                        minWidth: 0,
                    }}
                />
                <button
                    style=${{
                        ...btnBase,
                        opacity: newText.trim() ? 1 : 0.4,
                        cursor: newText.trim() ? 'pointer' : 'default',
                    }}
                    onClick=${handleAdd}
                    disabled=${!newText.trim() || adding}
                    title="Add punchlist item"
                >${adding ? '…' : 'Add'}</button>
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

function ComponentPanel({ component, conversations, allTasks, onClose, onFilterByComponent, onComponentUpdated }) {
    const [isMobile, setIsMobile] = useState(() => window.innerWidth < 640);
    const [localComponent, setLocalComponent] = useState(null);
    const [editingField, setEditingField] = useState(null); // 'name' | 'description'
    const [editingValue, setEditingValue] = useState('');
    const [editSaving, setEditSaving] = useState(false);
    const [editError, setEditError] = useState(null);
    const nameInputRef = useRef(null);
    const descTextareaRef = useRef(null);
    const justCancelled = useRef(false);

    useEffect(() => { setLocalComponent(component); }, [component]);

    useEffect(() => {
        if (editingField === 'name' && nameInputRef.current) {
            nameInputRef.current.focus();
            nameInputRef.current.select();
        }
        if (editingField === 'description' && descTextareaRef.current) {
            descTextareaRef.current.focus();
        }
    }, [editingField]);

    const refreshComponent = useCallback(async () => {
        if (!component) return;
        try {
            const updated = await api.getComponent(component.id);
            setLocalComponent(updated);
            if (onComponentUpdated) onComponentUpdated(updated);
        } catch (_) { /* ignore */ }
    }, [component, onComponentUpdated]);

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

    const startEdit = (field) => {
        setEditingField(field);
        const cur = localComponent || component;
        setEditingValue(field === 'name' ? (cur.name || '') : (cur.description || ''));
        setEditError(null);
    };

    const cancelEdit = () => {
        justCancelled.current = true;
        setEditingField(null);
        setEditingValue('');
        setEditError(null);
    };

    const saveEdit = async (field, value) => {
        if (justCancelled.current) { justCancelled.current = false; return; }
        if (!field) return;
        if (field === 'name' && !value.trim()) { cancelEdit(); return; }
        setEditSaving(true);
        try {
            const compId = (localComponent || component).id;
            await api.updateComponent(compId, { [field]: value });
            const updated = await api.getComponent(compId);
            setLocalComponent(updated);
            if (onComponentUpdated) onComponentUpdated(updated);
            setEditingField(null);
            setEditingValue('');
        } catch (err) {
            setEditError(err.message || 'Save failed');
        } finally {
            setEditSaving(false);
        }
    };

    const handleEditKeyDown = (e, field, value) => {
        if (e.key === 'Escape') { e.preventDefault(); cancelEdit(); return; }
        if (e.key === 'Enter' && field === 'name') { e.preventDefault(); saveEdit(field, value); }
    };

    if (!component) return null;

    // eff: use locally-updated state after a save, fall back to prop before first save
    const eff = localComponent || component;

    const linkedConvs = conversations.filter(c => c.component_id === eff.id).slice(0, 5);
    const componentTasks = allTasks
        .filter(t => t.component_id === eff.id)
        .sort((a, b) => {
            const ta = a.last_activity || a.updated_at || '';
            const tb = b.last_activity || b.updated_at || '';
            return tb < ta ? -1 : tb > ta ? 1 : 0;
        });
    const chainMap = buildChainMap(allTasks);
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
    }[eff.phase] || colors.textTertiary;

    const panelStyle = isMobile ? {
        position: 'fixed', left: 0, right: 0, bottom: 0,
        height: '65vh', background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: `${layout.borderRadius.lg} ${layout.borderRadius.lg} 0 0`,
        boxShadow: '0 -8px 40px rgba(0,0,0,0.5)', zIndex: 500,
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
        maxWidth: '100vw', boxSizing: 'border-box',
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
    const config = eff.config || {};
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
                .comp-edit-wrap { display: flex; align-items: center; gap: 6px; }
                .comp-edit-pencil { background: none; border: none; color: #888; cursor: pointer; padding: 0 2px; font-size: 13px; opacity: 0; transition: opacity 0.15s; line-height: 1; flex-shrink: 0; }
                .comp-edit-wrap:hover .comp-edit-pencil { opacity: 0.7; }
                .comp-edit-pencil:hover { opacity: 1 !important; }
                .comp-edit-name-wrap { flex: 1; overflow: hidden; }
                .comp-edit-desc-wrap { min-height: 20px; }
            `}</style>
            <div style=${{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)', zIndex: 499 }}
                 onClick=${onClose} />
            <div style=${panelStyle}>
                <!-- Header -->
                <div style=${{
                    display: 'flex', alignItems: 'center', gap: '10px',
                    padding: '12px 16px', borderBottom: `1px solid ${colors.border}`, flexShrink: 0,
                }}>
                    <div class="comp-edit-wrap comp-edit-name-wrap" style=${{ flex: 1, overflow: 'hidden' }}>
                        ${editingField === 'name' ? html`
                            <input
                                ref=${nameInputRef}
                                value=${editingValue}
                                onInput=${e => setEditingValue(e.target.value)}
                                onKeyDown=${e => handleEditKeyDown(e, 'name', editingValue)}
                                onBlur=${() => saveEdit('name', editingValue)}
                                disabled=${editSaving}
                                style=${{
                                    fontFamily: typography.fontBody, fontSize: typography.size.lg,
                                    fontWeight: typography.weight.semibold, color: colors.text,
                                    background: colors.bg, border: `1px solid ${colors.border}`,
                                    borderRadius: layout.borderRadius.sm, padding: '2px 6px',
                                    width: '100%', outline: 'none', boxSizing: 'border-box',
                                }}
                            />
                        ` : html`
                            <span style=${{
                                fontFamily: typography.fontBody, fontSize: typography.size.lg,
                                fontWeight: typography.weight.semibold, color: colors.text,
                                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                                display: 'block',
                            }}>${eff.name || eff.id}</span>
                            <button class="comp-edit-pencil" onClick=${() => startEdit('name')} title="Edit name">✎</button>
                        `}
                    </div>
                    ${eff.phase ? html`
                        <span style=${{
                            fontSize: typography.size.xs, color: phaseColor,
                            fontWeight: typography.weight.medium,
                            padding: '2px 8px', borderRadius: layout.borderRadius.pill,
                            background: phaseColor + '18',
                        }}>${eff.phase}</span>
                    ` : null}
                    <${ControlButtons}
                        paused=${component.paused}
                        onPause=${async () => { await api.pauseComponent(component.id); await refreshComponent(); }}
                        onResume=${async () => { await api.resumeComponent(component.id); await refreshComponent(); }}
                        onStop=${async () => { await api.stopComponent(component.id); await refreshComponent(); }}
                        entityType="component"
                    />
                    <button style=${closeBtnStyle} onClick=${onClose} title="Close (Esc)">×</button>
                </div>

                <!-- Body -->
                <div style=${{ flex: 1, overflowY: 'auto', padding: '16px', display: 'flex', flexDirection: 'column', gap: '20px' }}>
                    <!-- Description inline edit -->
                    <div class="comp-edit-wrap comp-edit-desc-wrap">
                        ${editingField === 'description' ? html`
                            <textarea
                                ref=${descTextareaRef}
                                value=${editingValue}
                                onInput=${e => setEditingValue(e.target.value)}
                                onKeyDown=${e => handleEditKeyDown(e, 'description', editingValue)}
                                onBlur=${() => saveEdit('description', editingValue)}
                                disabled=${editSaving}
                                rows="3"
                                style=${{
                                    width: '100%', fontFamily: typography.fontBody,
                                    fontSize: typography.size.sm, color: colors.text,
                                    background: colors.bg, border: `1px solid ${colors.border}`,
                                    borderRadius: layout.borderRadius.sm, padding: '5px 8px',
                                    outline: 'none', resize: 'vertical', boxSizing: 'border-box',
                                    lineHeight: typography.lineHeight.relaxed,
                                }}
                            />
                        ` : html`
                            <span style=${{
                                fontSize: typography.size.sm, flex: 1,
                                color: eff.description ? colors.textSecondary : colors.textTertiary,
                                fontStyle: eff.description ? 'normal' : 'italic',
                                cursor: eff.description ? 'default' : 'pointer',
                            }} onClick=${eff.description ? undefined : () => startEdit('description')}>${eff.description || 'Add description…'}</span>
                            <button class="comp-edit-pencil" onClick=${() => startEdit('description')} title="Edit description">✎</button>
                        `}
                    </div>
                    ${editError ? html`<div style=${{ fontSize: typography.size.xs, color: '#f87171' }}>${editError}</div>` : null}

                    <!-- Summary -->
                    <div style=${{
                        fontSize: typography.size.sm, color: colors.textSecondary,
                    }}>${summaryText}</div>

                    <!-- Filter tasks button -->
                    <button style=${filterBtnStyle} onClick=${handleFilter}>
                        ⚡ Filter tasks to ${eff.name || eff.id}
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
                    <${PunchlistSection} componentId=${eff.id} componentName=${eff.name} />

                    <!-- Tasks -->
                    <div>
                        <div style=${subheadStyle}>Tasks · ${componentTasks.length}</div>
                        ${componentTasks.length === 0 ? html`
                            <div style=${{
                                fontSize: typography.size.sm,
                                color: colors.textTertiary,
                                fontStyle: 'italic',
                            }}>No tasks yet</div>
                        ` : html`
                            <div style=${{ display: 'flex', flexDirection: 'column' }}>
                                ${componentTasks.map(task => {
                                    const chain = chainMap.get(task.id);
                                    const goal = task.goal || task.id;
                                    const displayGoal = goal.length > 52 ? goal.slice(0, 51) + '…' : goal;
                                    const taskShortId = task.id.includes('/') ? task.id.split('/').slice(1).join('/') : task.id;
                                    const displayId = taskShortId.length > 22 ? taskShortId.slice(0, 21) + '…' : taskShortId;
                                    return html`
                                        <a key=${task.id}
                                           href=${routes.task(task.id)}
                                           style=${{
                                               display: 'flex',
                                               alignItems: 'center',
                                               gap: '8px',
                                               padding: '7px 0',
                                               borderBottom: `1px solid ${colors.border}22`,
                                               minWidth: 0,
                                               textDecoration: 'none',
                                               color: 'inherit',
                                           }}
                                           class="foreman-task-row"
                                        >
                                            <${StatusDot} status=${task.status} />
                                            <span style=${{
                                                flex: 1,
                                                fontSize: typography.size.sm,
                                                color: colors.text,
                                                overflow: 'hidden',
                                                textOverflow: 'ellipsis',
                                                whiteSpace: 'nowrap',
                                                minWidth: 0,
                                            }}>${displayGoal}</span>
                                            <div style=${{ display: 'flex', alignItems: 'center', gap: '5px', flexShrink: 0 }}>
                                                <span style=${{
                                                    fontFamily: typography.fontMono,
                                                    fontSize: typography.size.xs,
                                                    color: colors.textTertiary,
                                                    whiteSpace: 'nowrap',
                                                }} title=${task.id}>${displayId}</span>
                                                ${chain ? html`
                                                    <${ChainBadge}
                                                        position=${chain.position}
                                                        total=${chain.total}
                                                    />
                                                ` : null}
                                                <span style=${{
                                                    fontFamily: typography.fontMono,
                                                    fontSize: typography.size.xs,
                                                    color: colors.textTertiary,
                                                    whiteSpace: 'nowrap',
                                                }}>${relativeTime(task.last_activity || task.updated_at)}</span>
                                            </div>
                                        </a>
                                    `;
                                })}
                            </div>
                        `}
                    </div>

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

function slugifyComponent(text) {
    return text
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '')
        .slice(0, 60);
}

function ComponentsSection({ components, conversations, tasks, componentFilter, onComponentFilter, projectId, onComponentCreated, onComponentUpdated }) {
    const [selectedComponent, setSelectedComponent] = useState(null);

    const handleComponentUpdated = (comp) => {
        setSelectedComponent(comp);
        if (onComponentUpdated) onComponentUpdated(comp);
    };
    const [showForm, setShowForm] = useState(false);
    const [formName, setFormName] = useState('');
    const [formDesc, setFormDesc] = useState('');
    const [submitting, setSubmitting] = useState(false);
    const [formError, setFormError] = useState(null);
    const [showNameTip, setShowNameTip] = useState(false);

    const derivedId = slugifyComponent(formName);

    const handleSubmit = async (e) => {
        e.preventDefault();
        if (!formName.trim()) return;
        setSubmitting(true);
        setFormError(null);
        try {
            const result = await api.createComponent({
                id: derivedId,
                project_id: projectId,
                name: formName.trim(),
                description: formDesc.trim() || undefined,
            });
            onComponentCreated(result);
            setFormName('');
            setFormDesc('');
            setShowForm(false);
        } catch (err) {
            setFormError(err.message || 'Failed to create component');
        } finally {
            setSubmitting(false);
        }
    };

    const handleCancel = () => {
        setShowForm(false);
        setFormName('');
        setFormDesc('');
        setFormError(null);
    };

    const sectionStyle = {
        display: 'flex',
        flexDirection: 'column',
        gap: '8px',
    };

    const headerRowStyle = {
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        marginBottom: '4px',
    };

    const headerStyle = {
        fontSize: typography.size.sm,
        fontWeight: typography.weight.semibold,
        color: colors.textSecondary,
        letterSpacing: '0.06em',
        textTransform: 'uppercase',
    };

    const newBtnStyle = {
        fontSize: typography.size.xs,
        fontFamily: typography.fontBody,
        fontWeight: typography.weight.medium,
        color: colors.accent,
        background: 'transparent',
        border: `1px solid ${colors.accent}`,
        borderRadius: layout.borderRadius.sm,
        padding: '3px 10px',
        cursor: 'pointer',
        lineHeight: '1.4',
    };

    const gridStyle = {
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
        gap: '8px',
    };

    const formBoxStyle = {
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.md,
        padding: '16px',
        display: 'flex',
        flexDirection: 'column',
        gap: '12px',
        marginTop: '4px',
    };

    const inputStyle = {
        width: '100%',
        background: colors.bg,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.sm,
        color: colors.text,
        fontFamily: typography.fontBody,
        fontSize: typography.size.sm,
        padding: '7px 10px',
        boxSizing: 'border-box',
        outline: 'none',
    };

    const textareaStyle = {
        ...inputStyle,
        resize: 'vertical',
        minHeight: '72px',
    };

    const labelStyle = {
        fontSize: typography.size.xs,
        fontWeight: typography.weight.medium,
        color: colors.textSecondary,
        marginBottom: '5px',
        display: 'flex',
        alignItems: 'center',
        gap: '5px',
    };

    const hintStyle = {
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        marginTop: '3px',
    };

    const formActionsStyle = {
        display: 'flex',
        gap: '8px',
        alignItems: 'center',
    };

    const submitBtnStyle = {
        fontFamily: typography.fontBody,
        fontSize: typography.size.sm,
        fontWeight: typography.weight.medium,
        background: colors.accent,
        color: '#fff',
        border: 'none',
        borderRadius: layout.borderRadius.sm,
        padding: '7px 16px',
        cursor: submitting ? 'not-allowed' : 'pointer',
        opacity: submitting ? 0.7 : 1,
    };

    const cancelBtnStyle = {
        fontFamily: typography.fontBody,
        fontSize: typography.size.sm,
        fontWeight: typography.weight.medium,
        background: 'transparent',
        color: colors.textSecondary,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.sm,
        padding: '7px 14px',
        cursor: 'pointer',
    };

    const tipStyle = {
        position: 'absolute',
        bottom: '120%',
        left: '50%',
        transform: 'translateX(-50%)',
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.md,
        padding: '8px 10px',
        fontSize: typography.size.xs,
        color: colors.textSecondary,
        whiteSpace: 'normal',
        width: '220px',
        zIndex: 100,
        pointerEvents: 'none',
        lineHeight: typography.lineHeight.relaxed,
    };

    const questionIconStyle = {
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: '15px',
        height: '15px',
        borderRadius: '50%',
        border: `1px solid ${colors.border}`,
        fontSize: '9px',
        color: colors.textTertiary,
        cursor: 'pointer',
        position: 'relative',
        flexShrink: 0,
    };

    if (components.length === 0 && !showForm) {
        return html`
            <div style=${sectionStyle}>
                <div style=${headerRowStyle}>
                    <div style=${headerStyle}>Components</div>
                    <button style=${newBtnStyle} onClick=${() => setShowForm(true)}>+ New Component</button>
                </div>
            </div>
        `;
    }

    return html`
        <div style=${sectionStyle}>
            <div style=${headerRowStyle}>
                <div style=${headerStyle}>Components</div>
                ${!showForm ? html`
                    <button style=${newBtnStyle} onClick=${() => setShowForm(true)}>+ New Component</button>
                ` : null}
            </div>
            ${components.length > 0 ? html`
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
            ` : null}

            ${showForm ? html`
                <form style=${formBoxStyle} onSubmit=${handleSubmit}>
                    <div>
                        <div style=${labelStyle}>
                            <span>Name</span>
                            <span
                                style=${questionIconStyle}
                                onMouseEnter=${() => setShowNameTip(true)}
                                onMouseLeave=${() => setShowNameTip(false)}
                            >?
                                ${showNameTip ? html`<div style=${tipStyle}>The display name for this component. Used to group tasks under a shared feature or epic. Keep it short and descriptive (e.g. "Auth Revamp", "Billing Flow").</div>` : null}
                            </span>
                        </div>
                        <input
                            style=${inputStyle}
                            type="text"
                            placeholder="e.g. Auth Revamp"
                            value=${formName}
                            onInput=${e => setFormName(e.target.value)}
                            required
                            autoFocus
                        />
                        ${derivedId ? html`<div style=${hintStyle}>ID: ${derivedId}</div>` : null}
                    </div>
                    <div>
                        <div style=${labelStyle}>Description <span style=${{ fontWeight: 400, color: colors.textTertiary }}>(optional)</span></div>
                        <textarea
                            style=${textareaStyle}
                            placeholder="What is this component about?"
                            value=${formDesc}
                            onInput=${e => setFormDesc(e.target.value)}
                        />
                    </div>
                    ${formError ? html`<div style=${{ fontSize: typography.size.xs, color: '#f87171' }}>${formError}</div>` : null}
                    <div style=${formActionsStyle}>
                        <button type="submit" style=${submitBtnStyle} disabled=${submitting || !formName.trim()}>
                            ${submitting ? 'Creating…' : 'Create'}
                        </button>
                        <button type="button" style=${cancelBtnStyle} onClick=${handleCancel}>Cancel</button>
                    </div>
                </form>
            ` : null}
        </div>

        <${ComponentPanel}
            component=${selectedComponent}
            conversations=${conversations}
            allTasks=${tasks}
            onClose=${() => setSelectedComponent(null)}
            onFilterByComponent=${onComponentFilter}
            onComponentUpdated=${handleComponentUpdated}
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

            <div style=${tagsRowStyle} class="foreman-task-row-tags">
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
    onStatusFilter, onComponentFilter, onTaskSelect }) {

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
        marginBottom: '8px',
    };

    const emptyStyle = {
        padding: '32px 0',
        textAlign: 'center',
        color: colors.textTertiary,
        fontSize: typography.size.sm,
    };

    return html`
        <div style=${sectionStyle}>
            <style>${`
                @media (max-width: 640px) {
                    .foreman-task-row { flex-wrap: wrap; }
                    .foreman-task-row-tags { width: 100%; flex-wrap: wrap; margin-top: 2px; }
                }
            `}</style>
            <div style=${sectionHeaderStyle}>Tasks · ${filtered.length}</div>

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

    const chainMap = buildChainMap(tasks);

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

            <!-- Components (knowledge drawers) -->
            <${ComponentsSection}
                components=${components}
                conversations=${conversations}
                tasks=${tasks}
                componentFilter=${componentFilter}
                onComponentFilter=${setComponentFilter}
                projectId=${id}
                onComponentCreated=${(comp) => setComponents(prev => [...prev, comp])}
                onComponentUpdated=${(comp) => setComponents(prev => prev.map(c => c.id === comp.id ? comp : c))}
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
            />
        </div>

        <!-- Task Panel slide-out -->
        <${TaskPanel}
            taskId=${selectedTaskId}
            onClose=${() => setSelectedTaskId(null)}
        />
    `;
}
