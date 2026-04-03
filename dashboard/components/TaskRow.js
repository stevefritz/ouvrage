import { h } from 'https://esm.sh/preact@10.25.4';
import { useState, useEffect, useCallback } from 'https://esm.sh/preact@10.25.4/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, layout, animation } from '../tokens.js';
import { routes } from '../router.js';
import { StatusDot } from './StatusDot.js';
import { ChainBadge } from './ChainBadge.js';
import { relativeTime } from './utils.js';

const html = htm.bind(h);

// ---------------------------------------------------------------------------
// Chain position map — computed from depends_on graph
// ---------------------------------------------------------------------------

export function buildChainMap(tasks) {
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
// PRTag — PR badge in task row
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

// ---------------------------------------------------------------------------
// TaskRow — single task row (status dot, goal, slug, badges, time)
// ---------------------------------------------------------------------------

export function TaskRow({ task, chainMap, allTasks, conversations, onSelect }) {
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
