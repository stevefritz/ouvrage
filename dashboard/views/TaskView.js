// Foreman Task View — The Most Important View
// Answers: "Did it work? What did CC say?"
//
// Layout: Status line → Git flow bar → Blocked-by → Actions + checklist →
//         ATTEMPT GROUPS (hero) → Gate dots → Details drawer
//
// Full page view with conversation thread.
// Compact slide-out panel mode will be added by foreman-panel-2 (task 5).

import { h } from 'https://esm.sh/preact@10.25.4';
import htm from 'https://esm.sh/htm@3.1.1';
import { useState, useEffect, useRef, useCallback, useMemo } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { colors, typography, statusColors, statusBgs, layout, animation, spacing } from '../tokens.js';
import { StatusDot } from '../components/StatusDot.js';
import { GateDots } from '../components/GateDots.js';
import { Tag } from '../components/Tag.js';
import { relativeTime } from '../components/utils.js';
import { routes } from '../router.js';
import { ImageLightbox, isImageFile } from '../components/ImageLightbox.js';
import { MarkdownLightbox, isMarkdownFile } from '../components/MarkdownLightbox.js';

const html = htm.bind(h);

// ── Helpers ──────────────────────────────────────────────────

let _domPurifyWarned = false;
function sanitize(dirty) {
    if (typeof DOMPurify?.sanitize === 'function') return DOMPurify.sanitize(dirty);
    if (!_domPurifyWarned) {
        console.warn('[TaskView] DOMPurify not loaded — markdown will render as escaped text. Check CDN/CSP.');
        _domPurifyWarned = true;
    }
    const div = document.createElement('div');
    div.textContent = dirty;
    return div.innerHTML;
}

function renderMarkdown(content) {
    if (!content) return '';
    try {
        return sanitize(marked.parse(content));
    } catch {
        return sanitize(content);
    }
}

function normTs(ts) {
    return ts ? (ts.endsWith('Z') ? ts : ts + 'Z') : null;
}

function shortId(taskId) {
    return taskId ? taskId.split('/').pop() : '';
}

// ── Message type → icon + haiku styling ─────────────────────

const MSG_META = {
    spec:          { icon: '📌', label: 'Spec',     borderColor: colors.blue },
    plan:          { icon: '📋', label: 'Plan',     borderColor: '#14b8a6' },
    progress:      { icon: '⚡', label: 'Progress', borderColor: colors.green },
    result:        { icon: '✅', label: 'Result',   borderColor: colors.accent },
    review:        { icon: '🔍', label: 'Review',   borderColor: '#ec4899' },
    question:      { icon: '❓', label: 'Question', borderColor: colors.yellow },
    answer:        { icon: '💬', label: 'Answer',   borderColor: '#06b6d4' },
    handoff:       { icon: '🤝', label: 'Handoff',  borderColor: '#14b8a6' },
    'test-result': { icon: '🧪', label: 'Tests',    borderColor: '#8b5cf6' },
    note:          { icon: '📝', label: 'Note',     borderColor: colors.textTertiary },
    status:        { icon: '📊', label: 'Status',   borderColor: colors.textTertiary },
};

function getMsgMeta(type) {
    return MSG_META[type] || MSG_META.note;
}

// Generate a haiku summary line from message content
function haikuSummary(msg) {
    const title = msg.title;
    if (title) return title;

    const content = (msg.content || '').replace(/^#+\s+/m, '').trim();
    // First meaningful line, truncated
    const firstLine = content.split('\n').find(l => l.trim()) || '';
    if (firstLine.length <= 80) return firstLine;
    return firstLine.slice(0, 77) + '…';
}

// Determine review verdict from message title (set by gate), falling back to content
function reviewVerdict(msg) {
    if (msg.type !== 'review') return null;
    // The gate sets title to "APPROVED" or "CHANGES REQUESTED" — check that first
    const title = (msg.title || '').toUpperCase();
    if (title.includes('APPROVED') || title.includes('LGTM')) return 'approved';
    if (title.includes('CHANGES REQUESTED') || title.includes('REJECTED')) return 'rejected';
    // Fallback: first line of content only (avoid false positives from body text)
    const firstLine = (msg.content || '').split('\n')[0].toLowerCase();
    if (firstLine.includes('approved') || firstLine.includes('lgtm')) return 'approved';
    if (firstLine.includes('changes requested') || firstLine.includes('rejected')) return 'rejected';
    return null;
}

// ── Status Line ─────────────────────────────────────────────

// Gate statuses that replace the task status as the primary label
const GATE_PRIMARY = {
    'testing':   { label: 'TESTING',   color: colors.yellow },
    'reviewing': { label: 'IN REVIEW', color: colors.yellow },
};

function StatusLine({ task, taskState, onEdit }) {
    // When gate is actively testing or reviewing, show that as the primary label
    const gatePrimary = task.gate_status ? GATE_PRIMARY[task.gate_status] : null;

    // Secondary gate badge: non-primary gate states that are worth showing
    const secondaryGateLabel = !gatePrimary && task.gate_status
        && task.gate_status !== 'passed' && task.gate_status !== 'stale'
        ? task.gate_status.toUpperCase().replace(/-/g, ' ')
        : null;

    // Use API-provided state label/color when available, fall back to legacy logic
    const statusLabel = gatePrimary
        ? gatePrimary.label
        : (taskState ? taskState.label : (task.status || 'ready').toUpperCase());
    const statusColor = gatePrimary
        ? gatePrimary.color
        : (taskState ? taskState.color : (statusColors[task.status] || colors.textSecondary));
    const dotPulse = taskState ? taskState.pulse : undefined;

    return html`
        <div style=${{
            display: 'flex', alignItems: 'center', gap: '10px',
            padding: '12px 0', flexWrap: 'wrap',
        }}>
            <${StatusDot} status=${task.status} pulse=${dotPulse} size=${10} />
            <span style=${{
                fontFamily: typography.fontMono, fontSize: typography.size.sm,
                fontWeight: typography.weight.semibold,
                color: statusColor,
                textTransform: 'uppercase', letterSpacing: '0.05em',
            }}>${statusLabel}</span>

            ${secondaryGateLabel ? html`
                <span style=${{
                    fontFamily: typography.fontMono, fontSize: typography.size.xs,
                    padding: '2px 8px', borderRadius: '4px',
                    background: statusBgs[task.status] || 'rgba(92, 94, 102, 0.12)',
                    color: statusColors[task.status] || colors.textSecondary,
                }}>${secondaryGateLabel}</span>
            ` : null}

            ${task.status === 'working' || gatePrimary ? html`
                <span style=${{ fontSize: typography.size.xs, color: colors.textTertiary }}>
                    ${relativeTime(task.last_activity)}
                </span>
            ` : null}

            <span style=${{ flex: 1, minWidth: '16px' }} />

            <span style=${{
                fontFamily: typography.fontBody, fontSize: typography.size.base,
                color: colors.text, fontWeight: typography.weight.medium,
                wordBreak: 'break-word', textAlign: 'right',
            }}>${task.goal || shortId(task.id)}</span>

            ${onEdit ? html`
                <button
                    onClick=${onEdit}
                    title="Edit task metadata"
                    class="foreman-edit-task-btn"
                    style=${{
                        background: 'transparent',
                        border: `1px solid ${colors.borderSubtle}`,
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
            ` : null}
        </div>
    `;
}

// ── Git Flow Lineage ─────────────────────────────────────────

function GitFlowLineage({ task, chain }) {
    const safeUrl = (url) => (typeof url === 'string' && (url.startsWith('https://') || url.startsWith('http://'))) ? url : null;
    const prUrl = task.pr_url || (task.artifacts || []).find(a => a.type === 'pr_url')?.ref;

    // Extract PR number from URL like https://github.com/org/repo/pull/123
    const prNumber = prUrl ? (prUrl.match(/\/pull\/(\d+)/) || [])[1] : null;

    const inChain = chain && chain.length > 1;
    const idx = inChain ? chain.findIndex(n => n.id === task.id) : -1;

    // Determine left/center/right
    let leftBranch, leftLabel, leftHref;
    let rightBranch, rightLabel, rightHref;

    if (inChain && idx >= 0) {
        if (idx > 0) {
            leftBranch = chain[idx - 1].branch;
            leftLabel = 'branched from';
            leftHref = routes.task(chain[idx - 1].id);
        } else {
            leftBranch = task.base_branch || task.project_default_branch || 'main';
            leftLabel = 'branched from';
            leftHref = null;
        }
        if (idx < chain.length - 1) {
            rightBranch = chain[idx + 1].branch;
            rightLabel = 'merges into';
            rightHref = routes.task(chain[idx + 1].id);
        } else {
            rightBranch = task.branch_target || task.base_branch || task.project_default_branch || 'main';
            rightLabel = 'merges into';
            rightHref = null;
        }
    } else {
        leftBranch = task.base_branch || task.project_default_branch || 'main';
        leftLabel = 'from';
        leftHref = null;
        rightBranch = task.branch_target || task.base_branch || task.project_default_branch || 'main';
        rightLabel = 'into';
        rightHref = null;
    }

    const pillBase = {
        display: 'inline-flex', alignItems: 'center',
        fontFamily: typography.fontMono, fontSize: typography.size.xs,
        padding: '4px 10px', borderRadius: layout.borderRadius.sm,
        whiteSpace: 'nowrap', textDecoration: 'none',
    };

    const sidePill = (href) => ({
        ...pillBase,
        background: colors.surface,
        color: colors.textSecondary,
        border: `1px solid ${colors.borderSubtle}`,
        cursor: href ? 'pointer' : 'default',
    });

    const centerPill = {
        ...pillBase,
        background: colors.accentBg,
        color: colors.accent,
        border: `1px solid ${colors.accent}`,
        fontSize: typography.size.sm,
        fontWeight: typography.weight.bold,
    };

    const labelStyle = {
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        textAlign: 'center',
        marginTop: '2px',
    };

    const linkPillStyle = (bg, fg) => ({
        display: 'inline-flex', alignItems: 'center', gap: '4px',
        fontFamily: typography.fontMono, fontSize: typography.size.xs,
        padding: '2px 8px', borderRadius: '4px',
        background: bg, color: fg, textDecoration: 'none',
        whiteSpace: 'nowrap',
    });

    return html`
        <div style=${{
            padding: '8px 0', marginBottom: '16px',
            borderBottom: `1px solid ${colors.border}`,
        }}>
            <!-- Three-part lineage -->
            <div style=${{
                display: 'flex', alignItems: 'flex-start', gap: '0',
                justifyContent: 'center',
            }}>
                <!-- Left pill -->
                <div style=${{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
                    ${leftHref ? html`
                        <a href=${leftHref} style=${sidePill(leftHref)} class="foreman-lineage-pill">
                            ← ${leftBranch}
                        </a>
                    ` : html`
                        <span style=${sidePill(null)}>← ${leftBranch}</span>
                    `}
                    <span style=${labelStyle}>${leftLabel}</span>
                </div>

                <!-- Arrow -->
                <span style=${{
                    color: colors.textTertiary, fontSize: typography.size.sm,
                    padding: '4px 8px', alignSelf: 'center',
                }}>→</span>

                <!-- Center pill -->
                <div style=${{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
                    <span style=${centerPill}>${task.branch || shortId(task.id)}</span>
                </div>

                <!-- Arrow -->
                <span style=${{
                    color: colors.textTertiary, fontSize: typography.size.sm,
                    padding: '4px 8px', alignSelf: 'center',
                }}>→</span>

                <!-- Right pill -->
                <div style=${{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
                    ${rightHref ? html`
                        <a href=${rightHref} style=${sidePill(rightHref)} class="foreman-lineage-pill">
                            ${rightBranch} →
                        </a>
                    ` : html`
                        <span style=${sidePill(null)}>${rightBranch} →</span>
                    `}
                    <span style=${labelStyle}>${rightLabel}</span>
                </div>
            </div>

            <!-- Below lineage: PR + auto-merge + links -->
            <div style=${{
                display: 'flex', alignItems: 'center', gap: '8px',
                marginTop: '8px', flexWrap: 'wrap',
            }}>
                ${prUrl && safeUrl(prUrl) ? html`
                    <a href=${safeUrl(prUrl)} target="_blank" rel="noopener"
                        style=${task.pr_status === 'merged'
                            ? linkPillStyle(colors.greenBg, colors.green)
                            : task.pr_status === 'closed'
                                ? linkPillStyle('rgba(92, 94, 102, 0.12)', colors.textTertiary)
                                : linkPillStyle('rgba(124, 90, 246, 0.15)', colors.accent)}
                        class="foreman-task-pr-link">
                        ${task.pr_status === 'merged'
                            ? `PR ${prNumber ? `#${prNumber}` : ''} merged ↗`
                            : `PR ${prNumber ? `#${prNumber}` : ''} ↗`}
                    </a>
                ` : null}

                <span style=${{
                    fontSize: typography.size.xs, color: colors.textTertiary,
                    fontFamily: typography.fontMono,
                }}>
                    ${task.auto_merge ? 'auto-merge on' : 'manual merge'}
                </span>

                ${task.conversation_id ? html`
                    <a href=${routes.conversation(task.conversation_id)}
                        style=${linkPillStyle('rgba(99, 102, 241, 0.12)', '#818cf8')}
                        class="foreman-task-conv-link">
                        💬 ${task.conversation_id}
                    </a>
                ` : null}

                ${task.claude_chat_url && safeUrl(task.claude_chat_url) ? html`
                    <a href=${safeUrl(task.claude_chat_url)} target="_blank" rel="noopener"
                        style=${linkPillStyle('rgba(249, 115, 22, 0.12)', '#fb923c')}
                        class="foreman-task-claude-link">
                        Claude ↗
                    </a>
                ` : null}
            </div>
        </div>
    `;
}

// ── Chain Strip ─────────────────────────────────────────────

function ChainStrip({ task, chain }) {
    if (!chain || chain.length <= 1) return null;

    const idx = chain.findIndex(n => n.id === task.id);
    if (idx < 0) return null;

    const current = chain[idx];
    const prev = idx > 0 ? chain[idx - 1] : null;
    const next = idx < chain.length - 1 ? chain[idx + 1] : null;
    const total = chain.length;
    const step = idx + 1;

    // Overflow counts
    const beforePrev = idx > 1 ? idx - 1 : 0;
    const afterNext = idx < total - 2 ? total - idx - 2 : 0;

    const truncGoal = (goal, max) => {
        const text = goal || '';
        return text.length > max ? text.slice(0, max - 1) + '…' : text;
    };

    const sideCardStyle = {
        width: '140px', flexShrink: 0,
        padding: '8px 10px',
        borderRadius: layout.borderRadius.md,
        border: `1px solid ${colors.borderSubtle}`,
        background: 'transparent',
        textDecoration: 'none',
        display: 'flex', alignItems: 'center', gap: '6px',
        cursor: 'pointer',
    };

    const currentCardStyle = {
        flex: 1, maxWidth: '60%', minWidth: 0,
        padding: '10px 14px',
        borderRadius: layout.borderRadius.md,
        border: `2px solid ${colors.accent}`,
        background: colors.accentBg,
    };

    return html`
        <div style=${{ marginBottom: '12px' }}>
            <!-- Three-card row -->
            <div style=${{
                display: 'flex', alignItems: 'stretch', gap: '8px',
            }}>
                <!-- Prev card -->
                ${prev ? html`
                    <a href=${routes.task(prev.id)} style=${sideCardStyle} class="foreman-chain-node">
                        <span style=${{ color: colors.textTertiary, flexShrink: 0 }}>←</span>
                        <span style=${{
                            width: '6px', height: '6px', borderRadius: '50%',
                            background: statusColors[prev.status] || colors.textTertiary,
                            flexShrink: 0,
                        }} />
                        <span style=${{
                            fontFamily: typography.fontMono, fontSize: typography.size.xs,
                            color: colors.textTertiary,
                            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                        }}>${truncGoal(prev.goal || shortId(prev.id), 20)}</span>
                    </a>
                ` : html`<div style=${{ width: '140px', flexShrink: 0 }} />`}

                <!-- Current card (hero) -->
                <div style=${currentCardStyle}>
                    <div style=${{
                        display: 'flex', alignItems: 'center', gap: '6px',
                        marginBottom: '4px',
                    }}>
                        <span style=${{
                            width: '8px', height: '8px', borderRadius: '50%',
                            background: statusColors[current.status] || colors.textTertiary,
                            flexShrink: 0,
                        }} />
                        <span style=${{
                            fontFamily: typography.fontMono, fontSize: typography.size.xs,
                            color: statusColors[current.status] || colors.textSecondary,
                            textTransform: 'uppercase',
                        }}>${(current.status || 'ready').toUpperCase()}</span>
                    </div>
                    <div style=${{
                        fontFamily: typography.fontBody, fontSize: typography.size.sm,
                        color: colors.text, fontWeight: typography.weight.medium,
                        lineHeight: typography.lineHeight.normal,
                        wordBreak: 'break-word',
                    }}>${current.goal || shortId(current.id)}</div>
                </div>

                <!-- Next card -->
                ${next ? html`
                    <a href=${routes.task(next.id)} style=${sideCardStyle} class="foreman-chain-node">
                        <span style=${{
                            width: '6px', height: '6px', borderRadius: '50%',
                            background: statusColors[next.status] || colors.textTertiary,
                            flexShrink: 0,
                        }} />
                        <span style=${{
                            fontFamily: typography.fontMono, fontSize: typography.size.xs,
                            color: colors.textTertiary,
                            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                            flex: 1,
                        }}>${truncGoal(next.goal || shortId(next.id), 20)}</span>
                        <span style=${{ color: colors.textTertiary, flexShrink: 0 }}>→</span>
                    </a>
                ` : html`<div style=${{ width: '140px', flexShrink: 0 }} />`}
            </div>

            <!-- Step indicator + overflow links -->
            <div style=${{
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                gap: '12px', marginTop: '6px',
            }}>
                ${beforePrev > 0 ? html`
                    <a href=${routes.task(chain[0].id)} style=${{
                        fontSize: typography.size.xs, color: colors.textTertiary,
                        textDecoration: 'none', fontFamily: typography.fontMono,
                    }} class="foreman-chain-nav">← ${beforePrev} more</a>
                ` : null}

                <span style=${{
                    fontSize: typography.size.xs, color: colors.textTertiary,
                    fontFamily: typography.fontMono,
                }}>Step ${step} of ${total}</span>

                ${afterNext > 0 ? html`
                    <a href=${routes.task(chain[chain.length - 1].id)} style=${{
                        fontSize: typography.size.xs, color: colors.textTertiary,
                        textDecoration: 'none', fontFamily: typography.fontMono,
                    }} class="foreman-chain-nav">${afterNext} more →</a>
                ` : null}
            </div>
        </div>
    `;
}

// ── Blocked By ──────────────────────────────────────────────

function BlockedBy({ task, blockerTask }) {
    if (!task.depends_on) return null;

    const blockerStatus = blockerTask?.status || 'unknown';
    // Don't show blocked-by when blocker is resolved
    if (['completed', 'merged', 'cancelled'].includes(blockerStatus)) return null;

    const blockerGoal = blockerTask?.goal || task.depends_on;

    return html`
        <div style=${{
            display: 'flex', alignItems: 'center', gap: '8px',
            padding: '8px 12px', marginBottom: '12px',
            background: 'rgba(245, 166, 35, 0.08)',
            border: `1px solid rgba(245, 166, 35, 0.2)`,
            borderRadius: layout.borderRadius.md,
            fontSize: typography.size.sm,
        }}>
            <span style=${{ color: colors.yellow }}>⏳</span>
            <span style=${{ color: colors.yellow }}>Blocked by</span>
            <a href=${routes.task(task.depends_on)}
                style=${{ color: colors.text, textDecoration: 'none', fontFamily: typography.fontMono, fontSize: typography.size.xs }}
                class="foreman-task-goal-link">
                ${shortId(task.depends_on)}
            </a>
            <${StatusDot} status=${blockerStatus} size=${6} />
            <span style=${{ color: colors.textSecondary, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                ${blockerGoal}
            </span>
        </div>
    `;
}

// ── Action Toolbar + Checklist Summary ──────────────────────

const ACTION_TOOLTIPS = {
    approve:           'Release this held task and dispatch CC.',
    dispatch:          'Create worktree and launch CC session.',
    hold:              'Put on hold. Requires manual approval before dispatch.',
    stop:              'Pause the task. Session preserved for resume.',
    cancel:            'Kill the running CC process. Code changes preserved.',
    resume:            'Continue the existing CC session with full history.',
    retry:             'Start a fresh CC session. Previous context is lost.',
    reopen:            'Set to reopened. Post feedback, then click Start.',
    start:             'Dispatch CC with feedback as revision instructions.',
    close:             'Destroy worktree and delete branch. Permanent.',
    'skip-gate':       'Manually mark gate as passed, bypassing validation.',
    'advance-chain':   'Dispatch dependent tasks in the chain.',
    'release-worktree':'Detach worktree without closing. Frees disk space.',
    'cancel-chain':    'Cancel this task and all dependents.',
};

// Map lifecycle style names to color tokens
const STYLE_COLORS = {
    primary:   { bg: colors.greenBg,  fg: colors.green },
    secondary: { bg: colors.yellowBg, fg: colors.yellow },
    danger:    { bg: colors.redBg,    fg: colors.red },
};

function ActionToolbar({ task, chain, apiActions, onAction }) {
    const actions = [];

    const btn = (action, label, bg, fg, needsConfirm = true) => html`
        <button key=${action} onClick=${() => onAction(action, task.id, needsConfirm)}
            title=${ACTION_TOOLTIPS[action] || ''}
            style=${{
                padding: '4px 12px', borderRadius: layout.borderRadius.sm,
                background: bg, color: fg, border: 'none', cursor: 'pointer',
                fontFamily: typography.fontBody, fontSize: typography.size.sm,
                fontWeight: typography.weight.medium, whiteSpace: 'nowrap',
                transition: 'opacity 120ms',
            }}
            class="foreman-action-btn">
            ${label}
        </button>
    `;

    // API-driven buttons (from /actions endpoint)
    if (apiActions) {
        for (const action of apiActions) {
            const coloring = STYLE_COLORS[action.style] || STYLE_COLORS.secondary;
            actions.push(btn(action.name, action.label, coloring.bg, coloring.fg, action.confirm));
        }
    }

    // Special actions not in transition table — always hardcoded
    // Hold button: only on ready+dispatchable (not held, not queued)
    if (task.status === 'ready' && !task.held && !task.queued_at) {
        actions.push(btn('hold', 'Hold', colors.yellowBg, colors.yellow, true));
    }
    if (task.status === 'completed' && task.gate_status === 'passed') {
        actions.push(btn('advance-chain', 'Advance', colors.accentBg, colors.accent, true));
    }
    if (task.worktree_path) {
        actions.push(btn('release-worktree', 'Release WT', 'rgba(249, 115, 22, 0.12)', '#fb923c', true));
    }
    const done = task.checklist_done || 0;
    const total = task.checklist_total || 0;

    return html`
        <div style=${{
            display: 'flex', alignItems: 'center', gap: '8px',
            padding: '8px 0', marginBottom: '16px', flexWrap: 'wrap',
        }}>
            ${actions}

            ${total > 0 ? html`
                <span style=${{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <span style=${{
                        fontFamily: typography.fontMono, fontSize: typography.size.sm,
                        color: done === total ? colors.green : colors.textSecondary,
                    }}>
                        ✓ ${done}/${total}
                    </span>
                    <span style=${{
                        width: '60px', height: '4px', borderRadius: '2px',
                        background: colors.border, overflow: 'hidden',
                        display: 'inline-block',
                    }}>
                        <span style=${{
                            display: 'block', height: '100%', borderRadius: '2px',
                            background: done === total ? colors.green : colors.accent,
                            width: total > 0 ? `${(done / total) * 100}%` : '0%',
                            transition: 'width 200ms',
                        }} />
                    </span>
                </span>
            ` : null}
        </div>
    `;
}

// ── Haiku Message Line ──────────────────────────────────────

function HaikuLine({ msg, isExpanded, onToggle }) {
    const meta = getMsgMeta(msg.type);
    const verdict = reviewVerdict(msg);
    const [showFormatted, setShowFormatted] = useState(false);

    // Review tint
    let bgTint = 'transparent';
    if (verdict === 'approved') bgTint = 'rgba(61, 214, 140, 0.06)';
    if (verdict === 'rejected') bgTint = 'rgba(242, 92, 92, 0.06)';

    const reviewerModel = msg.type === 'review' && msg.author ? msg.author.replace('cc-', '') : null;

    const time = msg.created_at ? new Date(normTs(msg.created_at)).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';

    return html`
        <div>
            <div
                onClick=${onToggle}
                style=${{
                    display: 'flex', alignItems: 'center', gap: '8px',
                    padding: '6px 10px', cursor: 'pointer',
                    borderRadius: layout.borderRadius.sm,
                    background: bgTint,
                    transition: 'background 120ms',
                }}
                class="foreman-haiku-line"
            >
                <span style=${{ fontSize: '13px', flexShrink: 0, width: '18px', textAlign: 'center' }}>
                    ${meta.icon}
                </span>
                <span style=${{
                    fontFamily: typography.fontBody, fontSize: typography.size.sm,
                    color: colors.text, flex: 1,
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>
                    ${haikuSummary(msg)}
                </span>

                ${verdict ? html`
                    <span style=${{
                        fontFamily: typography.fontMono, fontSize: typography.size.xs,
                        fontWeight: typography.weight.medium,
                        color: verdict === 'approved' ? colors.green : colors.red,
                        padding: '1px 6px', borderRadius: '3px',
                        background: verdict === 'approved' ? colors.greenBg : colors.redBg,
                    }}>
                        ${verdict === 'approved' ? '✓ APPROVED' : '✗ REJECTED'}
                    </span>
                ` : null}

                ${reviewerModel ? html`
                    <span style=${{
                        fontFamily: typography.fontMono, fontSize: typography.size.xs,
                        color: colors.textTertiary,
                    }}>
                        ${reviewerModel}
                    </span>
                ` : null}

                ${msg.type === 'spec' ? html`
                    <button
                        style=${{
                            fontSize: '11px', padding: '2px 7px',
                            borderRadius: layout.borderRadius.sm,
                            border: `1px solid ${colors.border}`,
                            background: 'transparent', color: colors.textTertiary,
                            cursor: 'pointer', fontFamily: typography.fontMono,
                            flexShrink: 0,
                        }}
                        onClick=${(e) => { e.stopPropagation(); setShowFormatted(true); }}
                        title="View spec as formatted markdown"
                    >View formatted</button>
                ` : null}

                <span style=${{
                    fontFamily: typography.fontMono, fontSize: typography.size.xs,
                    color: colors.textTertiary, flexShrink: 0,
                }}>
                    ${time}
                </span>

                <span style=${{
                    fontSize: '10px', color: colors.textTertiary, flexShrink: 0,
                    transition: 'transform 150ms',
                    transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)',
                }}>▶</span>
            </div>

            ${isExpanded ? html`
                <div style=${{
                    borderLeft: `2px solid ${meta.borderColor}`,
                    marginLeft: '18px', paddingLeft: '14px',
                    paddingTop: '8px', paddingBottom: '12px',
                    marginBottom: '4px',
                }}>
                    ${msg.title ? html`
                        <div style=${{
                            fontFamily: typography.fontBody, fontSize: typography.size.sm,
                            fontWeight: typography.weight.semibold,
                            color: colors.text, marginBottom: '6px',
                        }}>${msg.title}</div>
                    ` : null}
                    <div
                        style=${{
                            fontFamily: typography.fontBody, fontSize: typography.size.sm,
                            color: colors.textSecondary, lineHeight: typography.lineHeight.relaxed,
                        }}
                        dangerouslySetInnerHTML=${{ __html: renderMarkdown(msg.content) }}
                    />
                    <div style=${{
                        fontFamily: typography.fontMono, fontSize: typography.size.xs,
                        color: colors.textTertiary, marginTop: '8px',
                    }}>
                        ${msg.author || ''} · ${meta.label} · ${relativeTime(msg.created_at)}
                    </div>
                </div>
            ` : null}

            ${showFormatted ? html`
                <${MarkdownLightbox}
                    content=${msg.content}
                    title=${msg.title || 'Task Spec'}
                    onClose=${() => setShowFormatted(false)}
                />
            ` : null}
        </div>
    `;
}

// ── Session Log for an Attempt ──────────────────────────────

const TYPE_BADGE = {
    text:   { label: 'TEXT',   bg: 'rgba(59, 130, 246, 0.15)', fg: '#60a5fa' },
    tool:   { label: 'TOOL',   bg: 'rgba(139, 92, 246, 0.15)', fg: '#a78bfa' },
    result: { label: 'RESULT', bg: 'rgba(34, 197, 94, 0.15)',  fg: '#4ade80' },
    error:  { label: 'ERROR',  bg: 'rgba(242, 92, 92, 0.15)',  fg: '#f87171' },
    system: { label: 'SYSTEM', bg: 'rgba(148, 163, 184, 0.12)',fg: '#94a3b8' },
    done:   { label: 'DONE',   bg: 'rgba(61, 214, 140, 0.15)', fg: '#3dd68c' },
};

function classifyEntry(entry) {
    const type = entry.type || '';
    if (type === 'AssistantMessage') {
        const blocks = entry.content || [];
        const hasToolUse = blocks.some(b => b.type === 'tool_use');
        return hasToolUse ? 'tool' : 'text';
    }
    if (type === 'UserMessage') {
        const blocks = entry.content || [];
        const hasError = blocks.some(b => b.is_error);
        return hasError ? 'error' : 'result';
    }
    if (type === 'SystemMessage') return 'system';
    if (type === 'ResultMessage') return 'done';
    return 'text';
}

function entryPreview(entry) {
    const type = entry.type || '';
    if (type === 'AssistantMessage') {
        const blocks = entry.content || [];
        for (const b of blocks) {
            if (b.type === 'text') return (b.text || '').slice(0, 120);
            if (b.type === 'tool_use') return `${b.name || 'tool'}(${JSON.stringify(b.input || {}).slice(0, 80)})`;
        }
        return '';
    }
    if (type === 'UserMessage') {
        const blocks = entry.content || [];
        for (const b of blocks) {
            if (b.type === 'tool_result') return (b.preview || '').slice(0, 120);
        }
        return '';
    }
    if (type === 'ResultMessage') {
        return `${entry.num_turns || '?'} turns | $${(entry.cost_usd || 0).toFixed(2)}`;
    }
    if (type === 'SystemMessage') return entry.subtype || 'system';
    return '';
}

const FILTER_TYPES = ['text', 'tool', 'result', 'error'];

function AttemptSessionLog({ taskId, attemptNumber, isLive }) {
    const [isOpen, setIsOpen] = useState(false);
    const [entries, setEntries] = useState(null);
    const [loading, setLoading] = useState(false);
    const [activeFilters, setActiveFilters] = useState(new Set(FILTER_TYPES));
    const timerRef = useRef(null);

    const load = useCallback(async () => {
        try {
            setLoading(true);
            // Only omit attempt param when the task is actively working (live worktree).
            // Once the task completes, the worktree is cleaned up and we need the
            // explicit attempt number to resolve the correct archive.
            const params = isLive ? {} : { attempt: attemptNumber };
            const data = await api.getSessionLog(taskId, params);
            setEntries(Array.isArray(data) ? data : []);
        } catch (e) {
            console.warn('Session log load error:', e.message);
            setEntries([]);
        } finally {
            setLoading(false);
        }
    }, [taskId, attemptNumber, isLive]);

    useEffect(() => {
        if (!isOpen) return;
        load();
        // Auto-refresh only while task is actively working
        if (isLive) {
            timerRef.current = setInterval(load, 8000);
        }
        return () => { if (timerRef.current) clearInterval(timerRef.current); };
    }, [isOpen, load, isLive]);

    // Preview line: last meaningful entry
    const previewEntry = entries && entries.length > 0 ? entries[entries.length - 1] : null;
    const previewText = previewEntry ? entryPreview(previewEntry) : null;

    const toggleFilter = (e, type) => {
        e.stopPropagation();
        setActiveFilters(prev => {
            const next = new Set(prev);
            if (next.has(type)) { next.delete(type); } else { next.add(type); }
            return next;
        });
    };

    const filteredEntries = entries
        ? entries.filter(e => {
            const cls = classifyEntry(e);
            return !FILTER_TYPES.includes(cls) || activeFilters.has(cls);
        })
        : null;

    return html`
        <div style=${{ marginTop: '8px', marginLeft: '18px' }}>
            <div
                onClick=${() => setIsOpen(!isOpen)}
                style=${{
                    display: 'flex', alignItems: 'center', gap: '8px',
                    padding: '6px 10px', cursor: 'pointer',
                    borderRadius: layout.borderRadius.sm,
                    background: colors.surface,
                    border: `1px solid ${colors.borderSubtle}`,
                    transition: 'background 120ms',
                    flexWrap: 'wrap',
                }}
                class="foreman-session-log-toggle"
            >
                <span style=${{ fontSize: '11px', color: colors.textTertiary }}>
                    ${isOpen ? '▾' : '▸'}
                </span>
                <span style=${{
                    fontFamily: typography.fontMono, fontSize: typography.size.xs,
                    color: colors.textSecondary,
                }}>Session Log</span>

                ${!isOpen && previewText ? html`
                    <span style=${{
                        fontFamily: typography.fontMono, fontSize: typography.size.xs,
                        color: colors.textTertiary, flex: 1,
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    }}>${previewText.slice(0, 60)}${previewText.length > 60 ? '…' : ''}</span>
                ` : null}

                ${isOpen ? html`
                    <div style=${{ display: 'flex', gap: '4px', marginLeft: '4px' }} onClick=${e => e.stopPropagation()}>
                        ${FILTER_TYPES.map(type => {
                            const isActive = activeFilters.has(type);
                            const badge = TYPE_BADGE[type];
                            const isError = type === 'error';
                            return html`
                                <button
                                    key=${type}
                                    onClick=${(e) => toggleFilter(e, type)}
                                    style=${{
                                        padding: '1px 6px', borderRadius: '10px',
                                        fontSize: '10px', fontFamily: typography.fontMono,
                                        fontWeight: typography.weight.medium,
                                        cursor: 'pointer', border: 'none',
                                        background: isActive
                                            ? (isError ? 'rgba(242, 92, 92, 0.25)' : badge.bg)
                                            : 'transparent',
                                        color: isActive ? badge.fg : colors.textTertiary,
                                        transition: 'background 100ms, color 100ms',
                                        lineHeight: '18px',
                                    }}
                                >${badge.label}</button>
                            `;
                        })}
                    </div>
                ` : null}

                ${entries !== null ? html`
                    <span style=${{
                        fontFamily: typography.fontMono, fontSize: typography.size.xs,
                        color: colors.textTertiary, marginLeft: 'auto',
                    }}>${isOpen && filteredEntries && filteredEntries.length !== entries.length
                        ? `${filteredEntries.length}/${entries.length}`
                        : entries.length} entries</span>
                ` : null}
            </div>

            ${isOpen ? html`
                <div style=${{
                    maxHeight: '400px', overflowY: 'auto',
                    padding: '8px 0', marginTop: '4px',
                    fontFamily: typography.fontMono, fontSize: typography.size.xs,
                }}>
                    ${loading && entries === null ? html`
                        <div style=${{ color: colors.textTertiary, padding: '8px' }}>Loading...</div>
                    ` : null}

                    ${entries && entries.length === 0 ? html`
                        <div style=${{ color: colors.textTertiary, padding: '8px' }}>No session log entries</div>
                    ` : null}

                    ${filteredEntries && filteredEntries.length === 0 && entries && entries.length > 0 ? html`
                        <div style=${{ color: colors.textTertiary, padding: '8px' }}>All entries filtered — toggle a type above to show</div>
                    ` : null}

                    ${filteredEntries && filteredEntries.map((entry, i) => {
                        const cls = classifyEntry(entry);
                        const badge = TYPE_BADGE[cls] || TYPE_BADGE.text;
                        const ts = entry.timestamp ? new Date(entry.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
                        const preview = entryPreview(entry);

                        return html`
                            <${SessionLogEntry} key=${i} badge=${badge} ts=${ts} preview=${preview} entry=${entry} cls=${cls} />
                        `;
                    })}
                </div>
            ` : null}
        </div>
    `;
}

function SessionLogEntry({ badge, ts, preview, entry, cls }) {
    const [expanded, setExpanded] = useState(false);

    // Build full content for expansion
    let fullContent = null;
    if (entry.type === 'AssistantMessage') {
        const blocks = entry.content || [];
        const texts = blocks.map(b => {
            if (b.type === 'text') return b.text || '';
            if (b.type === 'tool_use') return `${b.name}(${JSON.stringify(b.input, null, 2)})`;
            return '';
        }).filter(Boolean);
        if (texts.some(t => t.length > 120)) fullContent = texts.join('\n\n');
    } else if (entry.type === 'UserMessage') {
        const blocks = entry.content || [];
        const texts = blocks.map(b => b.preview || '').filter(Boolean);
        if (texts.some(t => t.length > 120)) fullContent = texts.join('\n\n');
    } else if (entry.type === 'ResultMessage' && entry.result) {
        fullContent = entry.result;
    }

    const isExpandable = !!fullContent;

    return html`
        <div
            style=${{
                display: 'flex', alignItems: 'flex-start', gap: '6px',
                padding: '3px 10px', cursor: isExpandable ? 'pointer' : 'default',
                borderRadius: '3px',
            }}
            onClick=${isExpandable ? () => setExpanded(!expanded) : undefined}
            class=${isExpandable ? 'foreman-log-entry' : ''}
        >
            <span style=${{ color: colors.textTertiary, flexShrink: 0, width: '55px' }}>${ts}</span>
            <span style=${{
                padding: '0 4px', borderRadius: '2px',
                background: badge.bg, color: badge.fg,
                fontWeight: typography.weight.medium, flexShrink: 0,
                fontSize: '10px',
            }}>${badge.label}</span>
            <span style=${{
                color: cls === 'error' ? colors.red : colors.textSecondary,
                whiteSpace: expanded ? 'pre-wrap' : 'normal',
                wordBreak: 'break-all',
                overflowWrap: 'anywhere',
                flex: 1,
            }}>
                ${expanded ? fullContent : (preview.length > 120 ? preview.slice(0, 117) + '…' : preview)}
                ${isExpandable && !expanded ? html`
                    <span style=${{ color: colors.textTertiary, fontSize: '9px', marginLeft: '4px' }}>▸ more</span>
                ` : null}
                ${isExpandable && expanded ? html`
                    <span style=${{ color: colors.textTertiary, fontSize: '9px', marginLeft: '4px' }}>▾ less</span>
                ` : null}
            </span>
        </div>
    `;
}

// ── Attempt Group ───────────────────────────────────────────

function AttemptGroup({ attempt, isLatest, isExpanded: defaultExpanded, taskId, taskStatus }) {
    const [isExpanded, setIsExpanded] = useState(defaultExpanded);
    const [expandedMsgs, setExpandedMsgs] = useState(new Set());

    const msgs = attempt.messages || [];
    const outcome = attempt.outcome || 'in-progress';

    const outcomeStyle = {
        'in-progress':       { color: colors.yellow, label: 'in progress' },
        'retried':           { color: colors.textTertiary, label: 'retried' },
        'success':           { color: colors.green,  label: 'completed' },
        'test-failure':      { color: colors.red,    label: 'tests failed' },
        'review-rejection':  { color: colors.red,    label: 'review rejected' },
        'failed':            { color: colors.red,    label: 'failed' },
        'cancelled':         { color: colors.textTertiary, label: 'cancelled' },
    };

    // For non-latest attempts: show actual outcome if recorded (failed/success/etc.),
    // fall back to 'retried' when outcome is missing or still shows in-progress
    // (meaning the attempt was cut short before outcome could be recorded).
    const effectiveOutcome = (!isLatest && outcome === 'in-progress') ? 'retried' : outcome;
    const os = outcomeStyle[effectiveOutcome] || outcomeStyle['in-progress'];

    const toggleMsg = useCallback((msgId) => {
        setExpandedMsgs(prev => {
            const next = new Set(prev);
            if (next.has(msgId)) next.delete(msgId);
            else next.add(msgId);
            return next;
        });
    }, []);

    return html`
        <div style=${{
            marginBottom: '8px',
            border: `1px solid ${isLatest ? 'rgba(124, 90, 246, 0.25)' : colors.borderSubtle}`,
            borderRadius: layout.borderRadius.md,
            overflow: 'hidden',
        }}>
            <!-- Attempt header -->
            <div
                onClick=${() => setIsExpanded(!isExpanded)}
                style=${{
                    display: 'flex', alignItems: 'center', gap: '10px',
                    padding: '10px 14px', cursor: 'pointer',
                    background: isLatest ? 'rgba(124, 90, 246, 0.04)' : 'transparent',
                    transition: 'background 120ms',
                }}
                class="foreman-attempt-header"
            >
                <span style=${{
                    fontSize: '10px', color: colors.textTertiary,
                    transition: 'transform 150ms',
                    transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)',
                }}>▶</span>

                <span style=${{
                    fontFamily: typography.fontMono, fontSize: typography.size.sm,
                    fontWeight: typography.weight.medium, color: colors.text,
                }}>
                    Attempt ${attempt.attempt_number}
                    ${isLatest ? html`<span style=${{ color: colors.accent, fontSize: typography.size.xs, marginLeft: '6px' }}>(current)</span>` : null}
                </span>

                <span style=${{
                    fontFamily: typography.fontMono, fontSize: typography.size.xs,
                    color: os.color,
                }}>${os.label}</span>

                <span style=${{ flex: 1 }} />

                <span style=${{
                    fontFamily: typography.fontMono, fontSize: typography.size.xs,
                    color: colors.textTertiary,
                }}>${msgs.length} msgs</span>
            </div>

            <!-- Attempt body: messages + session log -->
            ${isExpanded ? html`
                <div style=${{ padding: '4px 14px 12px' }}>
                    ${msgs.map(msg => html`
                        <${HaikuLine}
                            key=${msg.id}
                            msg=${msg}
                            isExpanded=${expandedMsgs.has(msg.id)}
                            onToggle=${() => toggleMsg(msg.id)}
                        />
                    `)}

                    <${AttemptSessionLog}
                        taskId=${taskId}
                        attemptNumber=${attempt.attempt_number}
                        isLive=${isLatest && taskStatus === 'working'}
                    />
                </div>
            ` : null}
        </div>
    `;
}

// ── Gate Dots Section ───────────────────────────────────────

function GateDotsSection({ task }) {
    if (!task.auto_test && !task.auto_review) return null;

    return html`
        <div style=${{
            display: 'flex', alignItems: 'center', gap: '12px',
            padding: '12px 0', borderTop: `1px solid ${colors.border}`,
            marginTop: '8px',
        }}>
            <span style=${{
                fontFamily: typography.fontMono, fontSize: typography.size.xs,
                color: colors.textTertiary,
            }}>Gate</span>
            <${GateDots}
                gateStatus=${task.gate_status}
                taskStatus=${task.status}
                showLabels=${true}
                size=${8}
            />
            ${task.gate_retries > 0 ? html`
                <span style=${{
                    fontFamily: typography.fontMono, fontSize: typography.size.xs,
                    color: colors.textTertiary,
                }}>Retries: ${task.gate_retries}/${task.max_gate_retries || 3}</span>
            ` : null}
        </div>
    `;
}

// ── Gate Activity Panel ─────────────────────────────────────

function GateActivityPanel({ task }) {
    const [isOpen, setIsOpen] = useState(true);
    const [testOutput, setTestOutput] = useState('');
    const [reviewEntries, setReviewEntries] = useState([]);
    const outputRef = useRef(null);
    const autoScrollRef = useRef(true);

    const isTesting = task.gate_status === 'testing';
    const isReviewing = task.gate_status === 'reviewing';

    // Don't render if no active gate
    if (!isTesting && !isReviewing) return null;

    // Poll test output
    useEffect(() => {
        if (!isTesting) return;
        let cancelled = false;

        async function load() {
            try {
                const data = await api.getTestOutput(task.id);
                if (!cancelled && typeof data === 'string') setTestOutput(data);
            } catch (e) { /* ignore */ }
        }

        load();
        const timer = setInterval(load, 3000);
        return () => { cancelled = true; clearInterval(timer); };
    }, [isTesting, task.id]);

    // Poll review session log
    useEffect(() => {
        if (!isReviewing) return;
        let cancelled = false;

        async function load() {
            try {
                const data = await api.getGateSessionLog(task.id, { type: 'review' });
                if (!cancelled && Array.isArray(data)) setReviewEntries(data);
            } catch (e) { /* ignore */ }
        }

        load();
        const timer = setInterval(load, 4000);
        return () => { cancelled = true; clearInterval(timer); };
    }, [isReviewing, task.id]);

    // Auto-scroll test output
    useEffect(() => {
        if (outputRef.current && autoScrollRef.current) {
            outputRef.current.scrollTop = outputRef.current.scrollHeight;
        }
    }, [testOutput]);

    const handleScroll = useCallback(() => {
        if (!outputRef.current) return;
        const el = outputRef.current;
        autoScrollRef.current = (el.scrollHeight - el.scrollTop - el.clientHeight) < 40;
    }, []);

    const label = isTesting ? 'Tests Running' : 'Review Running';
    const dotColor = '#eab308'; // yellow for active

    // Extract text blocks from review entries for display
    const reviewText = reviewEntries
        .filter(e => e.type === 'AssistantMessage' && e.content)
        .flatMap(e => e.content.filter(b => b.type === 'text').map(b => b.text))
        .join('\n\n');

    // Extract tool calls for a compact summary
    const toolCalls = reviewEntries
        .filter(e => e.type === 'AssistantMessage' && e.content)
        .flatMap(e => e.content.filter(b => b.type === 'tool_use').map(b => b.name));
    const lastTools = toolCalls.slice(-5);

    return html`
        <div style=${{
            border: '1px solid rgba(234, 179, 8, 0.3)',
            borderRadius: layout.borderRadius.md,
            marginBottom: '8px', overflow: 'hidden',
            background: 'rgba(234, 179, 8, 0.04)',
        }}>
            <div
                onClick=${() => setIsOpen(!isOpen)}
                style=${{
                    display: 'flex', alignItems: 'center', gap: '8px',
                    padding: '10px 14px', cursor: 'pointer',
                }}
                class="foreman-attempt-header"
            >
                <span style=${{
                    width: '8px', height: '8px', borderRadius: '50%',
                    background: dotColor, flexShrink: 0,
                }} class="foreman-status-dot-pulse" />
                <span style=${{
                    fontFamily: typography.fontMono, fontSize: typography.size.sm,
                    fontWeight: typography.weight.semibold,
                    color: '#eab308',
                }}>${label}</span>

                ${isTesting && testOutput ? html`
                    <span style=${{
                        marginLeft: 'auto',
                        fontFamily: typography.fontMono, fontSize: typography.size.xs,
                        color: colors.textTertiary,
                    }}>${testOutput.split('\\n').filter(Boolean).length} lines</span>
                ` : null}

                ${isReviewing && lastTools.length > 0 ? html`
                    <span style=${{
                        marginLeft: 'auto',
                        fontFamily: typography.fontMono, fontSize: typography.size.xs,
                        color: colors.textTertiary,
                    }}>${lastTools[lastTools.length - 1]}</span>
                ` : null}

                <span style=${{ fontSize: '10px', color: colors.textTertiary, marginLeft: lastTools.length > 0 || testOutput ? '8px' : 'auto' }}>
                    ${isOpen ? '▾' : '▸'}
                </span>
            </div>

            ${isOpen ? html`
                <div style=${{ padding: '0 14px 12px' }}>
                    ${isTesting ? html`
                        <pre
                            ref=${outputRef}
                            onScroll=${handleScroll}
                            style=${{
                                margin: 0, padding: '10px',
                                background: colors.surface,
                                borderRadius: layout.borderRadius.sm,
                                fontSize: typography.size.xs,
                                fontFamily: typography.fontMono,
                                color: colors.text,
                                maxHeight: '400px', overflow: 'auto',
                                whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                                lineHeight: '1.5',
                            }}
                        >${testOutput || 'Waiting for test output...'}</pre>
                    ` : null}

                    ${isReviewing ? html`
                        <div style=${{
                            background: colors.surface,
                            borderRadius: layout.borderRadius.sm,
                            padding: '10px',
                            maxHeight: '400px', overflow: 'auto',
                            fontSize: typography.size.xs,
                            fontFamily: typography.fontMono,
                            color: colors.text,
                            lineHeight: '1.5',
                        }}>
                            ${reviewEntries.length === 0 ? html`
                                <span style=${{ color: colors.textTertiary }}>Waiting for reviewer activity...</span>
                            ` : null}

                            ${reviewEntries.filter(e => e.type === 'AssistantMessage' && e.content).map((entry, i) => html`
                                <div key=${i} style=${{ marginBottom: '8px' }}>
                                    ${entry.content.map((block, j) => {
                                        if (block.type === 'text') {
                                            return html`<div key=${j} style=${{ whiteSpace: 'pre-wrap', color: colors.text }}>${block.text}</div>`;
                                        }
                                        if (block.type === 'tool_use') {
                                            return html`<div key=${j} style=${{
                                                color: colors.accent, fontSize: typography.size.xs,
                                                padding: '2px 0',
                                            }}>⚡ ${block.name}</div>`;
                                        }
                                        return null;
                                    })}
                                </div>
                            `)}
                        </div>

                        ${task.review_subtask ? html`
                            <div style=${{
                                marginTop: '6px',
                                fontSize: typography.size.xs,
                                fontFamily: typography.fontMono,
                                color: colors.textTertiary,
                            }}>
                                Subtask: ${task.review_subtask.task_id}
                                ${task.review_subtask.elapsed_s ? html` · ${Math.floor(task.review_subtask.elapsed_s / 60)}m ${task.review_subtask.elapsed_s % 60}s` : null}
                            </div>
                        ` : null}
                    ` : null}
                </div>
            ` : null}
        </div>
    `;
}

// ── Checklist Drawer ────────────────────────────────────────

function ChecklistDrawer({ task }) {
    const [isOpen, setIsOpen] = useState(false);
    const items = task.checklist || [];
    if (items.length === 0) return null;

    return html`
        <div style=${{
            border: `1px solid ${colors.borderSubtle}`,
            borderRadius: layout.borderRadius.md,
            marginBottom: '8px', overflow: 'hidden',
        }}>
            <div
                onClick=${() => setIsOpen(!isOpen)}
                style=${{
                    display: 'flex', alignItems: 'center', gap: '8px',
                    padding: '8px 14px', cursor: 'pointer',
                }}
                class="foreman-attempt-header"
            >
                <span style=${{ fontSize: '10px', color: colors.textTertiary }}>
                    ${isOpen ? '▾' : '▸'}
                </span>
                <span style=${{
                    fontFamily: typography.fontBody, fontSize: typography.size.sm,
                    color: colors.textSecondary,
                }}>Checklist</span>
                <span style=${{
                    fontFamily: typography.fontMono, fontSize: typography.size.xs,
                    color: (task.checklist_done === task.checklist_total) ? colors.green : colors.textTertiary,
                }}>
                    ${task.checklist_done}/${task.checklist_total}
                </span>
            </div>

            ${isOpen ? html`
                <div style=${{ padding: '4px 14px 12px' }}>
                    ${items.map(c => html`
                        <div key=${c.id} style=${{
                            display: 'flex', alignItems: 'flex-start', gap: '6px',
                            padding: '3px 0', fontSize: typography.size.sm,
                            color: c.done ? colors.textTertiary : colors.text,
                        }}>
                            <span style=${{ flexShrink: 0 }}>${c.done ? '✅' : '⬜'}</span>
                            <span style=${{ textDecoration: c.done ? 'line-through' : 'none' }}>${c.item}</span>
                        </div>
                    `)}
                </div>
            ` : null}
        </div>
    `;
}

// ── Files Drawer ────────────────────────────────────────────

function formatFileSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function taskFileIcon(mime) {
    if (!mime) return '📄';
    if (mime.startsWith('image/')) return '🖼️';
    if (mime === 'application/pdf') return '📑';
    if (mime.startsWith('text/')) return '📄';
    return '📎';
}

function FilesDrawer({ taskId, pollTick }) {
    const [isOpen, setIsOpen] = useState(false);
    const [files, setFiles] = useState([]);
    const [loading, setLoading] = useState(true);
    const [dragOver, setDragOver] = useState(false);
    const [uploading, setUploading] = useState(false);
    const [error, setError] = useState(null);
    const [deleteConfirm, setDeleteConfirm] = useState(null);
    const [copiedId, setCopiedId] = useState(null);
    const [lightboxFile, setLightboxFile] = useState(null);
    const [mdLightboxFile, setMdLightboxFile] = useState(null);
    const inputRef = useRef(null);

    const loadFiles = useCallback(async () => {
        try {
            const data = await api.getTaskFiles(taskId);
            const list = data.files || data || [];
            list.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
            // Only update if data changed — prevents flicker on poll
            setFiles(prev => {
                if (JSON.stringify(prev) === JSON.stringify(list)) return prev;
                return list;
            });
        } catch (e) {
            // Only show errors on initial load, not on polls
            if (loading) setError(e.message);
        } finally {
            setLoading(false);
        }
    }, [taskId]);

    // Load on mount and re-fetch whenever pollTick changes
    useEffect(() => { loadFiles(); }, [loadFiles, pollTick]);

    const handleUpload = useCallback(async (fileList) => {
        if (!fileList || fileList.length === 0) return;
        setError(null);
        setUploading(true);
        try {
            for (const file of fileList) {
                await api.uploadTaskFile(taskId, file);
            }
            await loadFiles();
        } catch (e) {
            setError(e.message);
        } finally {
            setUploading(false);
        }
    }, [taskId, loadFiles]);

    const handleDelete = useCallback(async (fileId) => {
        try {
            await api.deleteFile(fileId);
            setFiles(prev => prev.filter(f => f.id !== fileId));
            setDeleteConfirm(null);
        } catch (e) {
            setError(e.message);
        }
    }, []);

    const handleCopy = useCallback(async (fileId, path) => {
        try {
            await navigator.clipboard.writeText(path);
            setCopiedId(fileId);
            setTimeout(() => setCopiedId(null), 1500);
        } catch (_) { /* ignore */ }
    }, []);

    const onDrop = useCallback((e) => {
        e.preventDefault();
        setDragOver(false);
        handleUpload(e.dataTransfer.files);
    }, [handleUpload]);

    const fileCount = loading ? '…' : files.length;

    const smallBtn = {
        fontSize: '11px', padding: '2px 8px',
        borderRadius: layout.borderRadius.sm,
        border: `1px solid ${colors.border}`,
        background: 'transparent', color: colors.textTertiary,
        cursor: 'pointer', fontFamily: 'inherit', flexShrink: 0,
        textDecoration: 'none',
    };

    return html`
        <div style=${{
            border: `1px solid ${colors.borderSubtle}`,
            borderRadius: layout.borderRadius.md,
            marginTop: '8px', overflow: 'hidden',
        }}>
            <div
                onClick=${() => setIsOpen(!isOpen)}
                style=${{
                    display: 'flex', alignItems: 'center', gap: '8px',
                    padding: '8px 14px', cursor: 'pointer',
                }}
                class="foreman-attempt-header"
            >
                <span style=${{ fontSize: '10px', color: colors.textTertiary }}>
                    ${isOpen ? '▾' : '▸'}
                </span>
                <span style=${{
                    fontFamily: typography.fontBody, fontSize: typography.size.sm,
                    color: colors.textSecondary,
                }}>Files</span>
                <span style=${{
                    fontFamily: typography.fontMono, fontSize: typography.size.xs,
                    color: colors.textTertiary,
                }}>
                    (${fileCount})
                </span>
            </div>

            ${isOpen ? html`
                <div style=${{ padding: '4px 14px 12px' }}>
                    <!-- Drop zone -->
                    <div
                        style=${{
                            border: `1px dashed ${dragOver ? colors.accent : colors.border}`,
                            borderRadius: layout.borderRadius.md,
                            padding: '8px', textAlign: 'center',
                            cursor: uploading ? 'wait' : 'pointer',
                            transition: `border-color ${animation.durationNormal}`,
                            background: dragOver ? colors.accentBg : 'transparent',
                            marginBottom: files.length > 0 ? '8px' : 0,
                        }}
                        onClick=${() => !uploading && inputRef.current?.click()}
                        onDrop=${onDrop}
                        onDragOver=${(e) => { e.preventDefault(); setDragOver(true); }}
                        onDragLeave=${() => setDragOver(false)}
                    >
                        <input
                            ref=${inputRef}
                            type="file"
                            multiple
                            style=${{ display: 'none' }}
                            onChange=${(e) => { handleUpload(e.target.files); e.target.value = ''; }}
                        />
                        ${uploading
                            ? html`<span style=${{ fontSize: typography.size.xs, color: colors.textTertiary }}>Uploading…</span>`
                            : html`<span style=${{ fontSize: typography.size.xs, color: colors.textTertiary }}>Drop files here or click to upload</span>`
                        }
                    </div>

                    ${error ? html`
                        <div style=${{ fontSize: typography.size.xs, color: colors.red, marginTop: '4px' }}>${error}</div>
                    ` : null}

                    <!-- File list -->
                    ${files.map(f => {
                        const isImg = isImageFile(f.filename);
                        const isMd = isMarkdownFile(f.filename);
                        const downloadUrl = `/dashboard/api/files/${f.id}/download`;
                        return html`
                        <div key=${f.id} style=${{
                            display: 'flex', alignItems: 'flex-start', gap: '8px',
                            padding: '8px 0',
                            borderBottom: `1px solid ${colors.borderSubtle}`,
                        }}>
                            <!-- Thumbnail or icon -->
                            ${isImg
                                ? html`<img
                                    src=${downloadUrl}
                                    alt=${f.filename}
                                    style=${{
                                        width: '36px', height: '36px',
                                        objectFit: 'cover', flexShrink: 0,
                                        borderRadius: layout.borderRadius.sm,
                                        border: `1px solid ${colors.border}`,
                                        cursor: 'pointer',
                                    }}
                                    onClick=${() => setLightboxFile(f)}
                                    title="Click to preview"
                                />`
                                : html`<span style=${{ fontSize: '14px', flexShrink: 0, paddingTop: '2px' }}>${taskFileIcon(f.mime_type)}</span>`
                            }
                            <!-- Info -->
                            <div style=${{ flex: 1, minWidth: 0 }}>
                                <!-- Line 1: filename + source label -->
                                <div style=${{ display: 'flex', alignItems: 'center', gap: '6px', minWidth: 0, marginBottom: '4px' }}>
                                    <span style=${{
                                        fontSize: typography.size.sm, fontWeight: typography.weight.medium,
                                        color: (isImg || isMd) ? colors.accent : colors.text,
                                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                                        flex: 1, minWidth: 0, cursor: (isImg || isMd) ? 'pointer' : 'default',
                                    }}
                                    title=${f.filename}
                                    onClick=${isImg ? () => setLightboxFile(f) : isMd ? () => setMdLightboxFile(f) : undefined}
                                    >${f.filename}</span>
                                    <span style=${{
                                        fontSize: '10px', color: colors.textTertiary, flexShrink: 0,
                                        fontStyle: 'italic',
                                    }}>${f.uploaded_by ? `📎 uploaded` : `🤖 CC`}</span>
                                </div>
                                <!-- Line 2: size + path (truncated) -->
                                <div style=${{ display: 'flex', alignItems: 'center', gap: '6px', minWidth: 0, marginBottom: '4px' }}>
                                    <span style=${{
                                        fontSize: typography.size.xs, color: colors.textTertiary, flexShrink: 0,
                                    }}>${formatFileSize(f.size_bytes)}</span>
                                    <span style=${{
                                        fontFamily: typography.fontMono, fontSize: '11px',
                                        color: colors.textSecondary, overflow: 'hidden',
                                        textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                                        flex: 1, minWidth: 0,
                                    }} title=${f.stored_path}>${f.stored_path}</span>
                                </div>
                                <!-- Line 3: action buttons -->
                                <div style=${{ display: 'flex', alignItems: 'center', gap: '4px', flexWrap: 'wrap' }}>
                                    ${isMd ? html`
                                        <button
                                            style=${{ ...smallBtn, color: colors.accent, borderColor: colors.accent }}
                                            onClick=${() => setMdLightboxFile(f)}
                                        >Preview</button>
                                    ` : null}
                                    <button
                                        style=${{ ...smallBtn, color: copiedId === f.id ? colors.green : colors.textTertiary }}
                                        onClick=${() => handleCopy(f.id, f.stored_path)}
                                    >${copiedId === f.id ? 'Copied!' : 'Copy path'}</button>
                                    <a
                                        href=${downloadUrl}
                                        target="_blank" rel="noopener"
                                        style=${smallBtn}
                                    >↓ Download</a>
                                    ${deleteConfirm === f.id ? html`
                                        <span style=${{ display: 'flex', gap: '4px', alignItems: 'center', flexShrink: 0 }}>
                                            <span style=${{ fontSize: '11px', color: colors.textTertiary }}>Delete?</span>
                                            <button style=${{ ...smallBtn, color: colors.red, borderColor: colors.red }}
                                                onClick=${() => handleDelete(f.id)}>Yes</button>
                                            <button style=${smallBtn}
                                                onClick=${() => setDeleteConfirm(null)}>Cancel</button>
                                        </span>
                                    ` : html`
                                        <button style=${{ ...smallBtn, fontSize: '12px', padding: '1px 6px' }}
                                            onClick=${() => setDeleteConfirm(f.id)}>✕</button>
                                    `}
                                </div>
                            </div>
                        </div>
                    `;})}
                    ${lightboxFile && html`
                        <${ImageLightbox}
                            src=${`/dashboard/api/files/${lightboxFile.id}/download`}
                            alt=${lightboxFile.filename}
                            onClose=${() => setLightboxFile(null)}
                        />
                    `}
                    ${mdLightboxFile && html`
                        <${MarkdownLightbox}
                            src=${`/dashboard/api/files/${mdLightboxFile.id}/download`}
                            filename=${mdLightboxFile.filename}
                            onClose=${() => setMdLightboxFile(null)}
                        />
                    `}
                </div>
            ` : null}
        </div>
    `;
}

// ── Details Drawer ──────────────────────────────────────────

function DetailsDrawer({ task }) {
    const [isOpen, setIsOpen] = useState(false);

    const row = (label, value) => value ? html`
        <div style=${{
            display: 'flex', justifyContent: 'space-between',
            padding: '4px 0', borderBottom: `1px solid ${colors.borderSubtle}`,
        }}>
            <span style=${{ fontFamily: typography.fontBody, fontSize: typography.size.sm, color: colors.textTertiary }}>${label}</span>
            <span style=${{ fontFamily: typography.fontMono, fontSize: typography.size.sm, color: colors.textSecondary }}>${value}</span>
        </div>
    ` : null;

    return html`
        <div style=${{
            border: `1px solid ${colors.borderSubtle}`,
            borderRadius: layout.borderRadius.md,
            marginTop: '8px', overflow: 'hidden',
        }}>
            <div
                onClick=${() => setIsOpen(!isOpen)}
                style=${{
                    display: 'flex', alignItems: 'center', gap: '8px',
                    padding: '8px 14px', cursor: 'pointer',
                }}
                class="foreman-attempt-header"
            >
                <span style=${{ fontSize: '10px', color: colors.textTertiary }}>
                    ${isOpen ? '▾' : '▸'}
                </span>
                <span style=${{
                    fontFamily: typography.fontBody, fontSize: typography.size.sm,
                    color: colors.textSecondary,
                }}>Details</span>
            </div>

            ${isOpen ? html`
                <div style=${{ padding: '4px 14px 12px' }}>
                    ${row('Task ID', task.id)}
                    ${row('Model', task.model)}
                    ${row('Dispatches', task.dispatch_count)}
                    ${row('Current Attempt', task.current_attempt)}
                    ${row('Cost', `$${(task.total_cost_usd || 0).toFixed(2)}`)}
                    ${row('Tokens In', `${((task.total_input_tokens || 0) / 1000).toFixed(0)}K`)}
                    ${row('Tokens Out', `${((task.total_output_tokens || 0) / 1000).toFixed(1)}K`)}
                    ${row('Phase', task.phase)}
                    ${task.resolved_config?.test_command ? html`
                        <div style=${{
                            display: 'flex', justifyContent: 'space-between',
                            padding: '4px 0', borderBottom: `1px solid ${colors.borderSubtle}`,
                            gap: '8px',
                        }}>
                            <span style=${{ fontFamily: typography.fontBody, fontSize: typography.size.sm, color: colors.textTertiary, flexShrink: 0 }}>Test Command</span>
                            <span style=${{ fontFamily: typography.fontMono, fontSize: typography.size.xs, color: colors.textSecondary, wordBreak: 'break-all', textAlign: 'right' }}>${task.resolved_config.test_command}</span>
                        </div>
                    ` : null}
                    ${row('Auto Test', task.auto_test ? 'Yes' : 'No')}
                    ${row('Auto Review', task.auto_review ? 'Yes' : 'No')}
                    ${task.auto_review ? row('Review model', task.review_model === 'sonnet' ? 'Sonnet' : 'Opus') : null}
                    ${row('Auto PR', task.auto_pr ? 'Yes' : 'No')}
                    ${row('Auto Merge', task.auto_merge ? 'Yes' : 'No')}
                    ${row('Base Branch', task.base_branch || task.project_default_branch || 'main')}
                    ${row('Worktree', task.worktree_path)}
                    ${row('Created', task.created_at ? new Date(normTs(task.created_at)).toLocaleString() : null)}
                    ${row('Last Activity', relativeTime(task.last_activity))}
                    ${(task.tags || []).length > 0 ? html`
                        <div style=${{ display: 'flex', gap: '4px', marginTop: '8px', flexWrap: 'wrap' }}>
                            ${task.tags.map(t => html`<${Tag} key=${t}>${t}<//>`)}
                        </div>
                    ` : null}
                </div>
            ` : null}
        </div>
    `;
}

// ── Message Input ───────────────────────────────────────────

function MessageInput({ taskId, onMessageSent }) {
    const [content, setContent] = useState('');
    const [msgType, setMsgType] = useState('review');

    const handleSubmit = async (e) => {
        e.preventDefault();
        const text = content.trim();
        if (!text) return;
        try {
            await api.postMessage(taskId, text, msgType);
            setContent('');
            onMessageSent();
        } catch (err) {
            console.error('Post message error:', err);
        }
    };

    return html`
        <form onSubmit=${handleSubmit} style=${{
            display: 'flex', gap: '8px', marginTop: '16px',
            padding: '12px 0', borderTop: `1px solid ${colors.border}`,
        }}>
            <select
                value=${msgType}
                onChange=${e => setMsgType(e.target.value)}
                style=${{
                    background: colors.surface, border: `1px solid ${colors.border}`,
                    borderRadius: layout.borderRadius.sm,
                    padding: '4px 8px', color: colors.textSecondary,
                    fontFamily: typography.fontBody, fontSize: typography.size.sm,
                }}
            >
                <option value="review">Review</option>
                <option value="note">Note</option>
                <option value="answer">Answer</option>
            </select>
            <input
                type="text"
                placeholder="Post a message..."
                value=${content}
                onInput=${e => setContent(e.target.value)}
                style=${{
                    flex: 1, background: colors.surface,
                    border: `1px solid ${colors.border}`,
                    borderRadius: layout.borderRadius.sm,
                    padding: '4px 12px', color: colors.text,
                    fontFamily: typography.fontBody, fontSize: typography.size.sm,
                    outline: 'none',
                }}
            />
            <button type="submit" style=${{
                padding: '4px 16px', borderRadius: layout.borderRadius.sm,
                background: colors.accent, color: '#fff', border: 'none',
                fontFamily: typography.fontBody, fontSize: typography.size.sm,
                fontWeight: typography.weight.medium, cursor: 'pointer',
            }}>Send</button>
        </form>
    `;
}

// ── Start Config Overlay (inline, not modal) ─────────────────

function StartConfigOverlay({ task, onConfirm, onCancel }) {
    const [autoTest, setAutoTest] = useState(!!task.auto_test);
    const [autoReview, setAutoReview] = useState(!!task.auto_review);

    const rowStyle = {
        display: 'flex', alignItems: 'center', gap: '10px',
        padding: '6px 0',
    };
    const labelStyle = {
        fontFamily: typography.fontBody, fontSize: typography.size.sm,
        color: colors.text, cursor: 'pointer', flex: 1,
    };

    return html`
        <div style=${{
            margin: '8px 0 12px', padding: '14px 16px',
            background: colors.surface, border: `1px solid ${colors.border}`,
            borderRadius: layout.borderRadius.md,
        }}>
            <div style=${{
                fontFamily: typography.fontBody, fontSize: typography.size.sm,
                fontWeight: typography.weight.medium, color: colors.text,
                marginBottom: '10px',
            }}>Start revision — configure this attempt</div>

            <div style=${rowStyle}>
                <input type="checkbox" id="start-auto-test" checked=${autoTest}
                    onChange=${e => setAutoTest(e.target.checked)}
                    style=${{ accentColor: colors.accent, width: '15px', height: '15px', cursor: 'pointer' }} />
                <label for="start-auto-test" style=${labelStyle}>Run tests after completion</label>
            </div>
            <div style=${rowStyle}>
                <input type="checkbox" id="start-auto-review" checked=${autoReview}
                    onChange=${e => setAutoReview(e.target.checked)}
                    style=${{ accentColor: colors.accent, width: '15px', height: '15px', cursor: 'pointer' }} />
                <label for="start-auto-review" style=${labelStyle}>Run review after completion</label>
            </div>

            <div style=${{ display: 'flex', gap: '8px', marginTop: '12px' }}>
                <button onClick=${onCancel} style=${{
                    padding: '5px 14px', borderRadius: layout.borderRadius.sm,
                    background: 'transparent', border: `1px solid ${colors.border}`,
                    color: colors.textSecondary, cursor: 'pointer',
                    fontFamily: typography.fontBody, fontSize: typography.size.sm,
                }}>Cancel</button>
                <button onClick=${() => onConfirm({ auto_test: autoTest, auto_review: autoReview })} style=${{
                    padding: '5px 14px', borderRadius: layout.borderRadius.sm,
                    background: colors.green, border: 'none',
                    color: '#fff', cursor: 'pointer',
                    fontFamily: typography.fontBody, fontSize: typography.size.sm,
                    fontWeight: typography.weight.medium,
                }}>Start Revision</button>
            </div>
        </div>
    `;
}


// ── Chain Invalidation Warning ────────────────────────────────

function ChainInvalidationWarning({ task, chain }) {
    if (task.status !== 'reopened' || !chain || chain.length <= 1) return null;

    const idx = chain.findIndex(n => n.id === task.id);
    if (idx < 0) return null;

    const downstream = chain.slice(idx + 1).filter(
        n => ['completed', 'merged', 'working', 'ready'].includes(n.status)
    );
    if (downstream.length === 0) return null;

    const names = downstream.map(n => n.id.split('/').pop()).join(', ');
    const count = downstream.length;

    return html`
        <div style=${{
            display: 'flex', alignItems: 'flex-start', gap: '8px',
            padding: '8px 12px', marginBottom: '12px',
            background: 'rgba(245, 166, 35, 0.08)',
            border: '1px solid rgba(245, 166, 35, 0.25)',
            borderRadius: layout.borderRadius.md,
            fontSize: typography.size.sm,
        }}>
            <span>⚠️</span>
            <span style=${{ color: colors.yellow, lineHeight: '1.4' }}>
                Starting will invalidate ${count} downstream ${count === 1 ? 'task' : 'tasks'}
                ${' '}(${names}). They will need to be re-run.
            </span>
        </div>
    `;
}


// ── Confirm Dialog ──────────────────────────────────────────

const CONFIRM_TEXT = {
    cancel: { title: 'Cancel Task', body: 'Kill the running CC process? Code changes preserved.' },
    retry: { title: 'Retry Task', body: 'Start a fresh CC session? Previous context will be lost.' },
    resume: { title: 'Resume Session', body: 'Continue the existing CC session with full history?' },
    close: { title: 'Close Task', body: 'Destroy worktree and delete branch? Cannot be undone.' },
    reopen: { title: 'Reopen Task', body: 'Reopen for revisions? Post feedback then click Start.' },
    'cancel-reopen': { title: 'Cancel Re-open', body: 'Discard re-open and return task to completed state?' },
    'skip-gate': { title: 'Skip Gate', body: 'Manually mark gate as passed, bypassing validation?' },
    'advance-chain': { title: 'Advance Chain', body: 'Dispatch the next dependent task in the chain?' },
    'release-worktree': { title: 'Release Worktree', body: 'Detach worktree without closing the task?' },
    approve: { title: 'Approve & Dispatch', body: 'Release this held task for dispatch?' },
    hold: { title: 'Hold Task', body: 'Put this task on hold? It will require manual approval before dispatch.' },
    dispatch: { title: 'Dispatch Task', body: 'Create worktree and launch CC session?' },
    'cancel-chain': { title: 'Cancel Chain', body: 'Cancel this task and all dependent tasks in the chain? This cannot be undone.' },
};

function ConfirmOverlay({ action, onConfirm, onCancel }) {
    if (!action) return null;
    const cfg = CONFIRM_TEXT[action] || { title: action, body: `Proceed with ${action}?` };

    return html`
        <div onClick=${onCancel} style=${{
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
                }}>${cfg.title}</h3>
                <p style=${{
                    fontFamily: typography.fontBody, fontSize: typography.size.sm,
                    color: colors.textSecondary, margin: '0 0 20px',
                    lineHeight: typography.lineHeight.normal,
                }}>${cfg.body}</p>
                <div style=${{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
                    <button onClick=${onCancel} style=${{
                        padding: '6px 16px', borderRadius: layout.borderRadius.sm,
                        background: colors.surface, border: `1px solid ${colors.border}`,
                        color: colors.textSecondary, cursor: 'pointer',
                        fontFamily: typography.fontBody, fontSize: typography.size.sm,
                    }}>Cancel</button>
                    <button onClick=${onConfirm} style=${{
                        padding: '6px 16px', borderRadius: layout.borderRadius.sm,
                        background: colors.accent, border: 'none',
                        color: '#fff', cursor: 'pointer',
                        fontFamily: typography.fontBody, fontSize: typography.size.sm,
                        fontWeight: typography.weight.medium,
                    }}>${cfg.title}</button>
                </div>
            </div>
        </div>
    `;
}

// ── Main TaskView Component ─────────────────────────────────

// ── EditTaskPanel helpers ────────────────────────────────────

function EditFieldLabel({ label, tooltip }) {
    const [showTip, setShowTip] = useState(false);
    return html`
        <div style=${{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '5px' }}>
            <label style=${{
                fontSize: typography.size.sm,
                fontWeight: typography.weight.medium,
                color: colors.textSecondary,
            }}>${label}</label>
            ${tooltip ? html`
                <span
                    style=${{
                        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                        width: '15px', height: '15px', borderRadius: '50%',
                        border: `1px solid ${colors.border}`,
                        fontSize: '10px', color: colors.textTertiary,
                        cursor: 'default', position: 'relative', flexShrink: 0,
                    }}
                    onMouseEnter=${() => setShowTip(true)}
                    onMouseLeave=${() => setShowTip(false)}
                >?
                    ${showTip ? html`
                        <div style=${{
                            position: 'absolute', bottom: '120%', left: '50%',
                            transform: 'translateX(-50%)',
                            background: colors.surface, border: `1px solid ${colors.border}`,
                            borderRadius: layout.borderRadius.md,
                            padding: '7px 10px',
                            fontSize: typography.size.xs, color: colors.textSecondary,
                            whiteSpace: 'normal', width: '220px',
                            zIndex: 200, pointerEvents: 'none',
                            lineHeight: typography.lineHeight.relaxed,
                        }}>${tooltip}</div>
                    ` : null}
                </span>
            ` : null}
        </div>
    `;
}

const FIELD_TOOLTIPS = {
    component_id:       'Component this task belongs to within the project.',
    base_branch:        'Git branch this task was branched from. Usually main or the previous task\'s branch.',
    branch_target:      'Branch this task\'s PR will merge into.',
    tags:               'Free-form labels for filtering and grouping tasks.',
    auto_test:          'Run the test gate automatically after the task completes.',
    auto_review:        'Run the AI review gate automatically after tests pass.',
    auto_pr:            'Automatically open a pull request when the gate passes. Mutually exclusive with auto-merge.',
    auto_merge:         'Automatically merge the PR when gates pass. Mutually exclusive with auto-PR.',
    max_turns:          'Maximum number of tool-use turns before the CC session is cut off.',
    max_wall_clock:     'Maximum wall-clock seconds for the CC session (0 = unlimited).',
    max_test_retries:   'How many times to retry the test gate before failing.',
    max_review_retries: 'How many times to retry the review gate before failing.',
    model:              'Claude model for the CC worker session.',
    review_model:       'Claude model used for the AI review gate.',
    jira_ticket:        'Associated Jira ticket key (e.g. PROJ-123).',
    conversation_id:    'Linked Switchboard conversation ID for context injection.',
    claude_chat_url:    'URL of the Claude.ai chat used to spec this task.',
};

const MODEL_OPTIONS = [
    { value: 'sonnet', label: 'Sonnet (fast, efficient)' },
    { value: 'opus',   label: 'Opus (powerful, thorough)' },
];

function diffTaskFields(original, form) {
    const changed = {};
    for (const [key, value] of Object.entries(form)) {
        if (key === 'tags') {
            const orig = (original.tags || []).slice().sort().join(',');
            const next = (value || []).slice().sort().join(',');
            if (orig !== next) changed.tags = value;
        } else if (key === 'component_id') {
            const orig = original.component_id || null;
            const next = value || null;
            if (orig !== next) changed.component_id = next;
        } else if (['auto_test','auto_review','auto_pr','auto_merge'].includes(key)) {
            if (Boolean(original[key]) !== Boolean(value)) changed[key] = Boolean(value);
        } else if (['max_turns','max_wall_clock','max_test_retries','max_review_retries'].includes(key)) {
            const orig = original[key] != null ? Number(original[key]) : null;
            const next = value !== '' && value != null ? Number(value) : null;
            if (orig !== next) changed[key] = next;
        } else {
            const orig = original[key] || '';
            const next = value || '';
            if (orig !== next) changed[key] = next || null;
        }
    }
    return changed;
}

// ── EditTaskPanel ────────────────────────────────────────────

function EditTaskPanel({ task, onClose, onSaved }) {
    const [components, setComponents] = useState([]);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState(null);
    const [tagInput, setTagInput] = useState('');

    // Form state — all mutable fields
    const [form, setForm] = useState(() => ({
        component_id:       task.component_id || '',
        base_branch:        task.base_branch || '',
        branch_target:      task.branch_target || '',
        tags:               task.tags ? [...task.tags] : [],
        auto_test:          Boolean(task.auto_test),
        auto_review:        Boolean(task.auto_review),
        auto_pr:            Boolean(task.auto_pr),
        auto_merge:         Boolean(task.auto_merge),
        max_turns:          task.max_turns != null ? String(task.max_turns) : '',
        max_wall_clock:     task.max_wall_clock != null ? String(task.max_wall_clock) : '',
        max_test_retries:   task.max_test_retries != null ? String(task.max_test_retries) : '',
        max_review_retries: task.max_review_retries != null ? String(task.max_review_retries) : '',
        model:              task.model || 'sonnet',
        review_model:       task.review_model || 'opus',
        jira_ticket:        task.jira_ticket || '',
        conversation_id:    task.conversation_id || '',
        claude_chat_url:    task.claude_chat_url || '',
    }));

    // Load components for dropdown
    useEffect(() => {
        if (!task.project_id) return;
        api.getComponents(task.project_id)
            .then(data => {
                const list = Array.isArray(data) ? data : (data.components || []);
                setComponents(list);
            })
            .catch(() => setComponents([]));
    }, [task.project_id]);

    const set = (field, value) => setForm(f => ({ ...f, [field]: value }));

    const addTag = () => {
        const t = tagInput.trim().toLowerCase();
        if (!t || form.tags.includes(t)) { setTagInput(''); return; }
        set('tags', [...form.tags, t]);
        setTagInput('');
    };
    const removeTag = (t) => set('tags', form.tags.filter(x => x !== t));

    const handleTagKeyDown = (e) => {
        if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); addTag(); }
    };

    const handleSave = async () => {
        const changed = diffTaskFields(task, form);
        if (Object.keys(changed).length === 0) { onClose(); return; }
        setSaving(true);
        setError(null);
        try {
            await api.updateTask(task.id, changed);
            onSaved();
        } catch (e) {
            setError(e.message || 'Save failed');
            setSaving(false);
        }
    };

    const inputStyle = {
        width: '100%', boxSizing: 'border-box',
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.sm,
        color: colors.text,
        fontFamily: typography.fontBody,
        fontSize: typography.size.sm,
        padding: '7px 10px',
        outline: 'none',
    };

    const toggleStyle = (active) => ({
        display: 'inline-flex', alignItems: 'center', gap: '6px',
        padding: '5px 12px',
        borderRadius: layout.borderRadius.sm,
        border: `1px solid ${active ? colors.accent : colors.border}`,
        background: active ? colors.accentBg : colors.surface,
        color: active ? colors.accent : colors.textSecondary,
        cursor: 'pointer',
        fontSize: typography.size.sm,
        fontWeight: typography.weight.medium,
        userSelect: 'none',
    });

    const sectionStyle = {
        display: 'grid',
        gridTemplateColumns: '1fr 1fr',
        gap: '16px',
    };

    return html`
        <!-- Backdrop -->
        <div
            style=${{
                position: 'fixed', inset: 0,
                background: 'rgba(0,0,0,0.55)',
                zIndex: 1000,
                display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
                padding: '40px 16px',
                overflowY: 'auto',
            }}
            onClick=${(e) => e.target === e.currentTarget && onClose()}
        >
            <div style=${{
                background: colors.bg,
                border: `1px solid ${colors.border}`,
                borderRadius: layout.borderRadius.lg,
                width: '100%', maxWidth: '680px',
                boxShadow: '0 24px 64px rgba(0,0,0,0.45)',
            }}>
                <!-- Header -->
                <div style=${{
                    padding: '20px 24px 16px',
                    borderBottom: `1px solid ${colors.border}`,
                    display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '12px',
                }}>
                    <div style=${{ flex: 1, minWidth: 0 }}>
                        <div style=${{
                            fontFamily: typography.fontMono, fontSize: typography.size.xs,
                            color: colors.textTertiary, marginBottom: '4px',
                        }}>${task.id}</div>
                        <div style=${{
                            fontSize: typography.size.md, fontWeight: typography.weight.semibold,
                            color: colors.text, wordBreak: 'break-word',
                        }}>${task.goal}</div>
                        <div style=${{
                            display: 'flex', alignItems: 'center', gap: '8px', marginTop: '8px', flexWrap: 'wrap',
                        }}>
                            <span style=${{
                                fontFamily: typography.fontMono, fontSize: typography.size.xs,
                                fontWeight: typography.weight.semibold,
                                color: statusColors[task.status] || colors.textSecondary,
                                textTransform: 'uppercase', letterSpacing: '0.05em',
                            }}>${(task.status || 'ready').toUpperCase()}</span>
                            ${task.project_id ? html`
                                <span style=${{ fontSize: typography.size.xs, color: colors.textTertiary }}>
                                    ${task.project_id}
                                </span>
                            ` : null}
                            ${task.branch ? html`
                                <span style=${{
                                    fontFamily: typography.fontMono, fontSize: typography.size.xs,
                                    color: colors.accent,
                                    background: colors.accentBg, borderRadius: '3px',
                                    padding: '1px 6px',
                                }}>${task.branch}</span>
                            ` : null}
                        </div>
                    </div>
                    <button
                        onClick=${onClose}
                        style=${{
                            background: 'transparent', border: 'none',
                            color: colors.textTertiary, cursor: 'pointer',
                            fontSize: '18px', padding: '0', lineHeight: 1, flexShrink: 0,
                        }}
                    >✕</button>
                </div>

                <!-- Error banner -->
                ${error ? html`
                    <div style=${{
                        margin: '0', padding: '10px 24px',
                        background: 'rgba(242, 92, 92, 0.12)',
                        borderBottom: `1px solid rgba(242, 92, 92, 0.3)`,
                        color: colors.red,
                        fontSize: typography.size.sm,
                    }}>⚠ ${error}</div>
                ` : null}

                <!-- Form body -->
                <div style=${{ padding: '20px 24px', display: 'flex', flexDirection: 'column', gap: '20px' }}>

                    <!-- Component -->
                    <div>
                        <${EditFieldLabel} label="Component" tooltip=${FIELD_TOOLTIPS.component_id} />
                        <select
                            value=${form.component_id}
                            onChange=${e => set('component_id', e.target.value)}
                            style=${{ ...inputStyle }}
                        >
                            <option value="">No component</option>
                            ${components.map(c => html`
                                <option key=${c.id} value=${c.id}>${c.name || c.id}</option>
                            `)}
                        </select>
                    </div>

                    <!-- Branch fields -->
                    <div style=${sectionStyle}>
                        <div>
                            <${EditFieldLabel} label="Base branch" tooltip=${FIELD_TOOLTIPS.base_branch} />
                            <input
                                type="text"
                                value=${form.base_branch}
                                onInput=${e => set('base_branch', e.target.value)}
                                placeholder="main"
                                style=${inputStyle}
                            />
                        </div>
                        <div>
                            <${EditFieldLabel} label="Branch target" tooltip=${FIELD_TOOLTIPS.branch_target} />
                            <input
                                type="text"
                                value=${form.branch_target}
                                onInput=${e => set('branch_target', e.target.value)}
                                placeholder="main"
                                style=${inputStyle}
                            />
                        </div>
                    </div>

                    <!-- Tags -->
                    <div>
                        <${EditFieldLabel} label="Tags" tooltip=${FIELD_TOOLTIPS.tags} />
                        <div style=${{
                            display: 'flex', flexWrap: 'wrap', gap: '6px',
                            padding: '6px 8px', minHeight: '38px',
                            background: colors.surface,
                            border: `1px solid ${colors.border}`,
                            borderRadius: layout.borderRadius.sm,
                        }}>
                            ${form.tags.map(t => html`
                                <span key=${t} style=${{
                                    display: 'inline-flex', alignItems: 'center', gap: '4px',
                                    background: colors.accentBg,
                                    border: `1px solid ${colors.accent}`,
                                    borderRadius: '3px', padding: '2px 7px',
                                    fontSize: typography.size.xs, color: colors.accent,
                                    fontFamily: typography.fontMono,
                                }}>
                                    ${t}
                                    <span
                                        onClick=${() => removeTag(t)}
                                        style=${{ cursor: 'pointer', opacity: 0.7, marginLeft: '2px' }}
                                    >✕</span>
                                </span>
                            `)}
                            <input
                                type="text"
                                value=${tagInput}
                                onInput=${e => setTagInput(e.target.value)}
                                onKeyDown=${handleTagKeyDown}
                                onBlur=${addTag}
                                placeholder=${form.tags.length ? '' : 'Add tag…'}
                                style=${{
                                    border: 'none', outline: 'none', background: 'transparent',
                                    color: colors.text, fontSize: typography.size.xs,
                                    fontFamily: typography.fontMono,
                                    minWidth: '80px', flex: 1, padding: '2px 4px',
                                }}
                            />
                        </div>
                        <div style=${{
                            fontSize: typography.size.xs, color: colors.textTertiary, marginTop: '4px',
                        }}>Press Enter or comma to add a tag.</div>
                    </div>

                    <!-- Automation toggles -->
                    <div>
                        <${EditFieldLabel} label="Automation" />
                        <div style=${{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
                            ${[
                                { key: 'auto_test',   label: 'Auto test',   tooltip: FIELD_TOOLTIPS.auto_test },
                                { key: 'auto_review', label: 'Auto review', tooltip: FIELD_TOOLTIPS.auto_review },
                                { key: 'auto_pr',     label: 'Auto PR',     tooltip: FIELD_TOOLTIPS.auto_pr },
                                { key: 'auto_merge',  label: 'Auto merge',  tooltip: FIELD_TOOLTIPS.auto_merge },
                            ].map(({ key, label, tooltip }) => html`
                                <button
                                    key=${key}
                                    title=${tooltip}
                                    onClick=${() => {
                                        // auto_pr and auto_merge are mutually exclusive
                                        if (key === 'auto_pr' && !form.auto_pr) {
                                            setForm(f => ({ ...f, auto_pr: true, auto_merge: false }));
                                        } else if (key === 'auto_merge' && !form.auto_merge) {
                                            setForm(f => ({ ...f, auto_merge: true, auto_pr: false }));
                                        } else {
                                            set(key, !form[key]);
                                        }
                                    }}
                                    style=${toggleStyle(form[key])}
                                >
                                    <span style=${{ fontSize: '11px' }}>${form[key] ? '✓' : '○'}</span>
                                    ${label}
                                </button>
                            `)}
                        </div>
                        ${form.auto_pr && form.auto_merge ? html`
                            <div style=${{
                                fontSize: typography.size.xs, color: colors.yellow, marginTop: '6px',
                            }}>⚠ auto-PR and auto-merge are mutually exclusive — only one will apply.</div>
                        ` : null}
                    </div>

                    <!-- Limits -->
                    <div>
                        <${EditFieldLabel} label="Limits" />
                        <div style=${sectionStyle}>
                            ${[
                                { key: 'max_turns',          label: 'Max turns',          tip: FIELD_TOOLTIPS.max_turns },
                                { key: 'max_wall_clock',     label: 'Max wall clock (s)', tip: FIELD_TOOLTIPS.max_wall_clock },
                                { key: 'max_test_retries',   label: 'Max test retries',   tip: FIELD_TOOLTIPS.max_test_retries },
                                { key: 'max_review_retries', label: 'Max review retries', tip: FIELD_TOOLTIPS.max_review_retries },
                            ].map(({ key, label, tip }) => html`
                                <div key=${key}>
                                    <${EditFieldLabel} label=${label} tooltip=${tip} />
                                    <input
                                        type="number"
                                        value=${form[key]}
                                        onInput=${e => set(key, e.target.value)}
                                        min="0"
                                        placeholder="default"
                                        style=${inputStyle}
                                    />
                                </div>
                            `)}
                        </div>
                    </div>

                    <!-- Models -->
                    <div style=${sectionStyle}>
                        <div>
                            <${EditFieldLabel} label="Worker model" tooltip=${FIELD_TOOLTIPS.model} />
                            <select value=${form.model} onChange=${e => set('model', e.target.value)} style=${inputStyle}>
                                ${MODEL_OPTIONS.map(o => html`<option key=${o.value} value=${o.value}>${o.label}</option>`)}
                            </select>
                        </div>
                        <div>
                            <${EditFieldLabel} label="Review model" tooltip=${FIELD_TOOLTIPS.review_model} />
                            <select value=${form.review_model} onChange=${e => set('review_model', e.target.value)} style=${inputStyle}>
                                ${MODEL_OPTIONS.map(o => html`<option key=${o.value} value=${o.value}>${o.label}</option>`)}
                            </select>
                        </div>
                    </div>

                    <!-- External links -->
                    <div>
                        <${EditFieldLabel} label="External links" />
                        <div style=${{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                            ${[
                                { key: 'jira_ticket',     label: 'Jira ticket',      tip: FIELD_TOOLTIPS.jira_ticket,     ph: 'PROJ-123' },
                                { key: 'conversation_id', label: 'Conversation ID',  tip: FIELD_TOOLTIPS.conversation_id, ph: 'my-conversation' },
                                { key: 'claude_chat_url', label: 'Claude chat URL',  tip: FIELD_TOOLTIPS.claude_chat_url, ph: 'https://claude.ai/chat/...' },
                            ].map(({ key, label, tip, ph }) => html`
                                <div key=${key}>
                                    <${EditFieldLabel} label=${label} tooltip=${tip} />
                                    <input
                                        type="text"
                                        value=${form[key]}
                                        onInput=${e => set(key, e.target.value)}
                                        placeholder=${ph}
                                        style=${inputStyle}
                                    />
                                </div>
                            `)}
                        </div>
                    </div>

                </div>

                <!-- Footer -->
                <div style=${{
                    padding: '16px 24px',
                    borderTop: `1px solid ${colors.border}`,
                    display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: '10px',
                }}>
                    <button
                        onClick=${onClose}
                        disabled=${saving}
                        style=${{
                            background: 'transparent',
                            border: `1px solid ${colors.border}`,
                            borderRadius: layout.borderRadius.sm,
                            color: colors.textSecondary,
                            cursor: 'pointer', padding: '7px 18px',
                            fontSize: typography.size.sm,
                        }}
                    >Cancel</button>
                    <button
                        onClick=${handleSave}
                        disabled=${saving}
                        class="foreman-save-task-btn"
                        style=${{
                            background: colors.accent,
                            border: 'none',
                            borderRadius: layout.borderRadius.sm,
                            color: '#fff',
                            cursor: saving ? 'wait' : 'pointer',
                            padding: '7px 20px',
                            fontSize: typography.size.sm,
                            fontWeight: typography.weight.medium,
                            opacity: saving ? 0.7 : 1,
                        }}
                    >${saving ? 'Saving…' : 'Save changes'}</button>
                </div>
            </div>
        </div>
    `;
}

// ── TaskView ─────────────────────────────────────────────────

export function TaskView({ id, mode = 'expanded', onClose }) {
    const [task, setTask] = useState(null);
    const [attempts, setAttempts] = useState(null);
    const [blockerTask, setBlockerTask] = useState(null);
    const [chain, setChain] = useState(null);
    const [error, setError] = useState(null);
    const [confirmAction, setConfirmAction] = useState(null);
    const [showStartOverlay, setShowStartOverlay] = useState(false);
    const [showEditPanel, setShowEditPanel] = useState(false);
    const [taskActions, setTaskActions] = useState(null);
    const [taskState, setTaskState] = useState(null);
    const mountedRef = useRef(true);
    const loadedRef = useRef(false);

    const loadTask = useCallback(async () => {
        try {
            const data = await api.getTask(id);
            if (mountedRef.current) {
                // Only update if data changed — prevents unnecessary re-renders
                setTask(prev => {
                    if (prev && JSON.stringify(prev) === JSON.stringify(data)) return prev;
                    return data;
                });
                setError(null);
                loadedRef.current = true;
            }
        } catch (e) {
            if (mountedRef.current) {
                // Only show error on initial load failure. Once loaded, poll errors
                // are silently logged to avoid flashing the error screen.
                if (!loadedRef.current) setError(e.message);
            }
        }
    }, [id]);

    const loadActions = useCallback(async () => {
        try {
            const data = await api.getTaskActions(id);
            if (mountedRef.current && data) {
                setTaskActions(prev => {
                    if (prev && JSON.stringify(prev) === JSON.stringify(data.actions)) return prev;
                    return data.actions || [];
                });
                setTaskState(prev => {
                    if (prev && JSON.stringify(prev) === JSON.stringify(data.state)) return prev;
                    return data.state || null;
                });
            }
        } catch {
            // Silent on errors — actions will fall back to legacy rendering
        }
    }, [id]);

    const loadAttempts = useCallback(async () => {
        try {
            const data = await api.getAttempts(id);
            if (mountedRef.current && data) {
                // API returns { attempts: [...] } or just [...]
                const list = Array.isArray(data) ? data : (data.attempts || []);
                // Only update if data actually changed — prevents re-render flicker
                setAttempts(prev => {
                    if (prev && JSON.stringify(prev) === JSON.stringify(list)) return prev;
                    return list;
                });
            }
        } catch (e) {
            // Silent on poll errors — only set empty on initial load
            if (mountedRef.current) {
                setAttempts(prev => prev === null ? [] : prev);
            }
        }
    }, [id]);

    // Reusable loaders for blocker + chain (called on mount and polled)
    const loadBlocker = useCallback(async () => {
        if (!task?.depends_on) { setBlockerTask(null); return; }
        try {
            const d = await api.getTask(task.depends_on);
            if (mountedRef.current) {
                setBlockerTask(prev => {
                    if (prev && JSON.stringify(prev) === JSON.stringify(d)) return prev;
                    return d;
                });
            }
        } catch { if (mountedRef.current) setBlockerTask(null); }
    }, [task?.depends_on]);

    const loadChain = useCallback(async () => {
        if (!id) return;
        try {
            const data = await api.getChain(id);
            const list = data?.chain || [];
            if (mountedRef.current) {
                const next = list.length > 1 ? list : null;
                setChain(prev => {
                    if (JSON.stringify(prev) === JSON.stringify(next)) return prev;
                    return next;
                });
            }
        } catch { if (mountedRef.current) setChain(null); }
    }, [id]);

    // pollTick — increments every 5s, passed to children to trigger their refresh
    const [pollTick, setPollTick] = useState(0);

    // Refs to hold latest versions of callbacks — avoids stale closures in setInterval
    const loadTaskRef = useRef(loadTask);
    const loadAttemptsRef = useRef(loadAttempts);
    const loadChainRef = useRef(loadChain);
    const loadBlockerRef = useRef(loadBlocker);
    const loadActionsRef = useRef(loadActions);
    useEffect(() => { loadTaskRef.current = loadTask; }, [loadTask]);
    useEffect(() => { loadAttemptsRef.current = loadAttempts; }, [loadAttempts]);
    useEffect(() => { loadChainRef.current = loadChain; }, [loadChain]);
    useEffect(() => { loadBlockerRef.current = loadBlocker; }, [loadBlocker]);
    useEffect(() => { loadActionsRef.current = loadActions; }, [loadActions]);

    // Initial load + unified polling — always runs while view is mounted
    useEffect(() => {
        mountedRef.current = true;
        loadedRef.current = false;
        setTask(null);
        setAttempts(null);
        setError(null);
        setBlockerTask(null);
        setChain(null);
        setTaskActions(null);
        setTaskState(null);
        setPollTick(0);

        // Immediate first load
        loadTask();
        loadAttempts();
        loadChain();
        loadActions();

        // Unified 5s poll — task + attempts + actions + pollTick (drives FilesDrawer)
        const mainTimer = setInterval(() => {
            loadTaskRef.current();
            loadAttemptsRef.current();
            loadActionsRef.current();
            setPollTick(t => (t + 1) % 1000);
        }, 5000);

        // Slower 15s poll — chain + blocker (change less frequently)
        const slowTimer = setInterval(() => {
            loadChainRef.current();
            loadBlockerRef.current();
        }, 15000);

        return () => {
            mountedRef.current = false;
            clearInterval(mainTimer);
            clearInterval(slowTimer);
        };
    }, [id]);

    // Load blocker whenever depends_on changes (initial + when task data arrives)
    useEffect(() => { loadBlocker(); }, [loadBlocker]);

    // Action handler
    // Shared action map — used by both immediate and confirmed execution paths
    const actionMap = useMemo(() => ({
        stop: () => api.stopTask(id),
        cancel: () => api.cancelTask(id),
        'cancel-reopen': () => api.cancelReopen(id),
        retry: () => api.retryTask(id),
        resume: () => api.resumeTask(id),
        close: () => api.closeTask(id),
        'skip-gate': () => api.skipGate(id),
        'advance-chain': () => api.advanceChain(id),
        'release-worktree': () => api.releaseWorktree(id),
        approve: () => api.approveTask(id),
        hold: () => api.holdTask(id),
        dispatch: () => api.dispatchTask(id),
        reopen: () => api.reopenTask(id),
        'cancel-chain': () => api.cancelChain(id),
    }), [id]);

    // Normalize action names: lifecycle uses underscores (skip_gate), actionMap uses dashes (skip-gate)
    const resolveAction = useCallback((action) => {
        return actionMap[action] || actionMap[action.replace(/_/g, '-')];
    }, [actionMap]);

    const handleAction = useCallback(async (action, taskId, needsConfirm = true) => {
        if (action === 'start') {
            setShowStartOverlay(true);
        } else if (!needsConfirm) {
            // Execute immediately without confirmation dialog
            try {
                const fn = resolveAction(action);
                if (fn) await fn();
                setTimeout(() => { loadTask(); loadAttempts(); loadActions(); }, 500);
            } catch (e) {
                console.error('Action error:', e);
            }
        } else {
            setConfirmAction(action);
        }
    }, [id, resolveAction, loadTask, loadAttempts, loadActions]);

    const executeAction = useCallback(async () => {
        if (!confirmAction || !task) return;
        const action = confirmAction;
        setConfirmAction(null);
        try {
            const fn = resolveAction(action);
            if (fn) await fn();
            // Reload after action
            setTimeout(() => { loadTask(); loadAttempts(); loadActions(); }, 500);
        } catch (e) {
            console.error('Action error:', e);
        }
    }, [confirmAction, resolveAction, task, loadTask, loadAttempts, loadActions]);

    const executeStart = useCallback(async (overrides) => {
        setShowStartOverlay(false);
        try {
            await api.startTask(id, overrides);
            setTimeout(() => { loadTask(); loadAttempts(); loadActions(); }, 500);
        } catch (e) {
            console.error('Start action error:', e);
        }
    }, [id, loadTask, loadAttempts, loadActions]);

    // Loading state
    if (error) {
        return html`
            <div style=${{ padding: layout.contentPadding }}>
                <a href="#/" style=${{ color: colors.textTertiary, textDecoration: 'none', fontSize: typography.size.sm }}
                    class="foreman-back-link">← Projects</a>
                <div style=${{
                    textAlign: 'center', padding: '40px 20px',
                    color: colors.red, fontSize: typography.size.sm,
                }}>
                    Error loading task: ${error}
                    <br /><br />
                    <button onClick=${loadTask} style=${{
                        background: colors.surface, border: `1px solid ${colors.border}`,
                        color: colors.textSecondary, padding: '4px 16px',
                        borderRadius: layout.borderRadius.sm, cursor: 'pointer',
                        fontSize: typography.size.sm,
                    }}>Retry</button>
                </div>
            </div>
        `;
    }

    if (!task) {
        return html`
            <div style=${{ padding: layout.contentPadding }}>
                <a href="#/" style=${{ color: colors.textTertiary, textDecoration: 'none', fontSize: typography.size.sm }}
                    class="foreman-back-link">← Projects</a>
                <div style=${{ textAlign: 'center', padding: '40px', color: colors.textTertiary }}>
                    Loading task...
                </div>
            </div>
        `;
    }

    // ── Compact panel mode ──────────────────────────────────
    if (mode === 'compact') {
        const safeUrl = (url) => (typeof url === 'string' && (url.startsWith('https://') || url.startsWith('http://'))) ? url : null;
        const prUrl = task.pr_url || (task.artifacts || []).find(a => a.type === 'pr_url')?.ref;
        const statusLabel = (task.status || 'ready').toUpperCase();

        const pillStyle = (bg, fg) => ({
            display: 'inline-flex', alignItems: 'center',
            fontFamily: typography.fontMono, fontSize: typography.size.xs,
            padding: '2px 7px', borderRadius: '4px',
            background: bg, color: fg, textDecoration: 'none',
            whiteSpace: 'nowrap',
        });

        // Attempt info
        const totalAttempts = task.current_attempt || (attempts ? attempts.length : 0);
        const maxAttempts = task.max_gate_retries ? totalAttempts + task.max_gate_retries - task.gate_retries : null;
        const showAttemptSummary = totalAttempts > 1;
        const attemptLabel = showAttemptSummary
            ? `Attempt ${totalAttempts}${maxAttempts ? ` of ${maxAttempts}` : ''}${task.status === 'working' ? ' — running' : ''}`
            : null;

        // Extract test result + review verdict from latest attempt
        let testResult = null;
        let reviewResult = null;
        if (attempts && attempts.length > 0) {
            const latestAttempt = attempts[attempts.length - 1];
            const msgs = latestAttempt.messages || [];

            // Test result — find last test-result message
            const testMsg = [...msgs].reverse().find(m => m.type === 'test-result');
            if (testMsg) {
                const content = (testMsg.content || '').toLowerCase();
                const title = (testMsg.title || '').toLowerCase();
                const passed = title.includes('pass') || content.includes('passed') || content.includes('all tests passed');
                const countMatch = (testMsg.content || '').match(/(\d+)\s*(passed|tests?\s+passed)/i);
                const count = countMatch ? countMatch[1] : null;
                testResult = {
                    passed,
                    label: passed
                        ? `✓ Tests passed${count ? ` — ${count}` : ''}`
                        : '✕ Tests failed',
                };
            }

            // Review verdict — find last review message
            const reviewMsg = [...msgs].reverse().find(m => m.type === 'review');
            if (reviewMsg) {
                const verdict = reviewVerdict(reviewMsg);
                if (verdict === 'approved') {
                    reviewResult = { verdict, label: '✓ APPROVED' };
                } else if (verdict === 'rejected') {
                    const excerpt = (reviewMsg.content || '').split('\n').find(l => l.trim() && !l.startsWith('#'))?.slice(0, 60) || '';
                    reviewResult = { verdict, label: `✕ REJECTED${excerpt ? ' — ' + excerpt : ''}` };
                } else if (task.gate_status === 'reviewing') {
                    reviewResult = { verdict: 'running', label: '● Review running…' };
                }
            } else if (task.gate_status === 'reviewing') {
                reviewResult = { verdict: 'running', label: '● Review running…' };
            }
        }

        // Cost
        const cost = task.total_cost_usd || 0;
        const showCost = cost > 0;

        return html`
            <div style=${{ display: 'flex', flexDirection: 'column', gap: '12px' }}>

                <!-- Status dot + label + timestamp -->
                <div style=${{
                    display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap',
                }}>
                    <${StatusDot} status=${task.status} size=${9} />
                    <span style=${{
                        fontFamily: typography.fontMono, fontSize: typography.size.sm,
                        fontWeight: typography.weight.semibold,
                        color: statusColors[task.status] || colors.textSecondary,
                        textTransform: 'uppercase', letterSpacing: '0.05em',
                    }}>${statusLabel}</span>
                    <span style=${{ flex: 1 }} />
                    <span style=${{ fontSize: typography.size.xs, color: colors.textTertiary }}>
                        ${relativeTime(task.last_activity || task.updated_at)}
                    </span>
                </div>

                <!-- Goal (wraps, never truncated) -->
                <div style=${{
                    fontSize: typography.size.md,
                    fontWeight: typography.weight.medium,
                    color: colors.text,
                    lineHeight: typography.lineHeight.normal,
                    wordBreak: 'break-word',
                }}>${task.goal || shortId(task.id)}</div>

                <!-- Task ID monospace -->
                <div style=${{
                    fontFamily: typography.fontMono, fontSize: typography.size.xs,
                    color: colors.textTertiary,
                }}>${shortId(task.id)}</div>

                <!-- Attempt summary (only if >1) -->
                ${attemptLabel ? html`
                    <div style=${{
                        fontSize: typography.size.sm,
                        color: colors.textSecondary,
                    }}>${attemptLabel}</div>
                ` : null}

                <!-- Gate dots with labels -->
                <div style=${{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <${GateDots}
                        gateStatus=${task.gate_status}
                        taskStatus=${task.status}
                        showLabels=${true}
                        size=${7}
                    />
                </div>

                <!-- Chain position (only if chain > 1) -->
                ${chain && chain.length > 1 ? (() => {
                    const idx = chain.findIndex(n => n.id === task.id);
                    const pos = idx >= 0 ? idx + 1 : null;
                    const prevId = idx > 0 ? chain[idx - 1].id : null;
                    const nextId = idx < chain.length - 1 ? chain[idx + 1].id : null;
                    return pos ? html`
                        <div style=${{
                            display: 'flex', alignItems: 'center', gap: '8px',
                            fontSize: typography.size.sm, color: colors.textSecondary,
                        }}>
                            ${prevId ? html`
                                <a href=${routes.task(prevId)} style=${{
                                    color: colors.accent, textDecoration: 'none',
                                    fontSize: typography.size.sm,
                                }} class="foreman-chain-nav">←</a>
                            ` : html`<span style=${{ color: colors.borderSubtle }}>←</span>`}
                            <span style=${{ fontFamily: typography.fontMono }}>Step ${pos} of ${chain.length}</span>
                            ${nextId ? html`
                                <a href=${routes.task(nextId)} style=${{
                                    color: colors.accent, textDecoration: 'none',
                                    fontSize: typography.size.sm,
                                }} class="foreman-chain-nav">→</a>
                            ` : html`<span style=${{ color: colors.borderSubtle }}>→</span>`}
                        </div>
                    ` : null;
                })() : null}

                <!-- Test result -->
                ${testResult ? html`
                    <div style=${{
                        fontSize: typography.size.sm,
                        fontFamily: typography.fontMono,
                        color: testResult.passed ? colors.green : colors.red,
                    }}>${testResult.label}</div>
                ` : null}

                <!-- Review verdict -->
                ${reviewResult ? html`
                    <div style=${{
                        fontSize: typography.size.sm,
                        fontFamily: typography.fontMono,
                        color: reviewResult.verdict === 'approved' ? colors.green
                            : reviewResult.verdict === 'rejected' ? colors.red
                            : colors.yellow,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                    }}>${reviewResult.label}</div>
                ` : null}

                <!-- Git flow pills -->
                ${(task.branch || prUrl) ? html`
                    <div style=${{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
                        ${task.branch ? html`
                            <span style=${pillStyle(colors.surface, colors.textSecondary)}>
                                ${task.branch}
                            </span>
                        ` : null}
                        ${task.branch_target ? html`
                            <span style=${{ color: colors.textTertiary, fontSize: typography.size.xs }}>→</span>
                            <span style=${pillStyle(colors.surface, colors.textTertiary)}>
                                ${task.branch_target}
                            </span>
                        ` : null}
                        ${prUrl && safeUrl(prUrl) ? html`
                            <a href=${safeUrl(prUrl)} target="_blank" rel="noopener"
                                style=${task.pr_status === 'merged'
                                    ? pillStyle(colors.greenBg, colors.green)
                                    : task.pr_status === 'closed'
                                        ? pillStyle('rgba(92, 94, 102, 0.12)', colors.textTertiary)
                                        : pillStyle('rgba(124, 90, 246, 0.15)', colors.accent)}
                                class="foreman-task-pr-link"
                                onClick=${e => e.stopPropagation()}>
                                ${task.pr_status === 'merged' ? 'PR merged ↗' : 'PR ↗'}
                            </a>
                        ` : null}
                    </div>
                ` : null}

                <!-- Cost (only if > $0) -->
                ${showCost ? html`
                    <div style=${{
                        fontFamily: typography.fontMono, fontSize: typography.size.xs,
                        color: colors.textTertiary,
                    }}>$${cost.toFixed(2)}</div>
                ` : null}

                <!-- Actions -->
                <${ActionToolbar} task=${task} chain=${chain} apiActions=${taskActions} onAction=${handleAction} />

                <!-- Open → full task page -->
                <a href=${routes.task(id)}
                    style=${{
                        display: 'inline-flex', alignItems: 'center',
                        padding: '6px 16px', borderRadius: layout.borderRadius.sm,
                        background: colors.accentBg,
                        color: colors.accent,
                        fontFamily: typography.fontBody, fontSize: typography.size.sm,
                        fontWeight: typography.weight.medium,
                        textDecoration: 'none', whiteSpace: 'nowrap',
                        alignSelf: 'flex-start',
                    }}
                    class="foreman-open-full-link">
                    Open full page →
                </a>

                <${ConfirmOverlay} action=${confirmAction} onConfirm=${executeAction} onCancel=${() => setConfirmAction(null)} />
            </div>
        `;
    }

    // Full page mode
    // Determine back link — go to project if we know it, otherwise landing
    const backHref = task.project_id ? routes.project(task.project_id) : '#/';
    const backLabel = task.project_id ? `← ${shortId(task.project_id)}` : '← Projects';

    return html`
        <div class="foreman-content" style=${{ padding: '0' }}>
            <a href=${backHref}
                style=${{
                    color: colors.textTertiary, textDecoration: 'none',
                    fontSize: typography.size.sm, display: 'inline-block',
                    marginBottom: '8px',
                }}
                class="foreman-back-link">
                ${backLabel}
            </a>

            <${StatusLine} task=${task} taskState=${taskState} onEdit=${() => setShowEditPanel(true)} />
            <${GitFlowLineage} task=${task} chain=${chain} />
            <${ChainStrip} task=${task} chain=${chain} />
            <${BlockedBy} task=${task} blockerTask=${blockerTask} />
            <${ChainInvalidationWarning} task=${task} chain=${chain} />
            <${ActionToolbar} task=${task} chain=${chain} apiActions=${taskActions} onAction=${handleAction} />
            ${showStartOverlay ? html`
                <${StartConfigOverlay}
                    task=${task}
                    onConfirm=${executeStart}
                    onCancel=${() => setShowStartOverlay(false)}
                />
            ` : null}

            <!-- ATTEMPT GROUPS — the hero -->
            <div style=${{ marginBottom: '16px' }}>
                ${attempts && attempts.length > 0 ? attempts.map((attempt, i) => html`
                    <${AttemptGroup}
                        key=${attempt.attempt_number}
                        attempt=${attempt}
                        isLatest=${i === attempts.length - 1}
                        isExpanded=${i === attempts.length - 1}
                        taskId=${id}
                        taskStatus=${task.status}
                    />
                `) : null}

                ${attempts && attempts.length === 0 ? html`
                    <div style=${{
                        padding: '20px', textAlign: 'center',
                        color: colors.textTertiary, fontSize: typography.size.sm,
                        border: `1px solid ${colors.borderSubtle}`,
                        borderRadius: layout.borderRadius.md,
                    }}>
                        No messages yet — task may be waiting to start
                    </div>
                ` : null}

                ${attempts === null ? html`
                    <div style=${{
                        padding: '20px', textAlign: 'center',
                        color: colors.textTertiary, fontSize: typography.size.sm,
                    }}>Loading attempts...</div>
                ` : null}
            </div>

            ${task.status !== 'completed' ? html`
                <${MessageInput} taskId=${id} onMessageSent=${() => { loadTask(); loadAttempts(); }} />
            ` : null}

            <${GateActivityPanel} task=${task} />
            <${GateDotsSection} task=${task} />
            <${ChecklistDrawer} task=${task} />
            <${FilesDrawer} taskId=${id} pollTick=${pollTick} />
            <${DetailsDrawer} task=${task} />

            <${ConfirmOverlay} action=${confirmAction} onConfirm=${executeAction} onCancel=${() => setConfirmAction(null)} />

            ${showEditPanel ? html`
                <${EditTaskPanel}
                    task=${task}
                    onClose=${() => setShowEditPanel(false)}
                    onSaved=${() => { setShowEditPanel(false); loadTask(); }}
                />
            ` : null}
        </div>
    `;
}
