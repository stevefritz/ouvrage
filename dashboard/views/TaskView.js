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
import { colors, typography, statusColors, statusBgs, layout } from '../tokens.js';
import { StatusDot } from '../components/StatusDot.js';
import { GateDots } from '../components/GateDots.js';
import { Tag } from '../components/Tag.js';
import { relativeTime } from '../components/utils.js';
import { routes } from '../router.js';

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

function StatusLine({ task }) {
    const gateLabel = task.gate_status && task.gate_status !== 'passed' && task.gate_status !== 'stale'
        ? task.gate_status.toUpperCase().replace(/-/g, ' ')
        : null;

    const statusLabel = (task.status || 'ready').toUpperCase();
    const isPulsing = task.status === 'working';

    return html`
        <div style=${{
            display: 'flex', alignItems: 'center', gap: '10px',
            padding: '12px 0', flexWrap: 'wrap',
        }}>
            <${StatusDot} status=${task.status} size=${10} />
            <span style=${{
                fontFamily: typography.fontMono, fontSize: typography.size.sm,
                fontWeight: typography.weight.semibold,
                color: statusColors[task.status] || colors.textSecondary,
                textTransform: 'uppercase', letterSpacing: '0.05em',
            }}>${statusLabel}</span>

            ${gateLabel ? html`
                <span style=${{
                    fontFamily: typography.fontMono, fontSize: typography.size.xs,
                    padding: '2px 8px', borderRadius: '4px',
                    background: statusBgs[task.status] || 'rgba(92, 94, 102, 0.12)',
                    color: statusColors[task.status] || colors.textSecondary,
                }}>${gateLabel}</span>
            ` : null}

            ${task.status === 'working' ? html`
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
        </div>
    `;
}

// ── Git Flow Bar ────────────────────────────────────────────

function GitFlowBar({ task }) {
    const prUrl = task.pr_url || (task.artifacts || []).find(a => a.type === 'pr_url')?.ref;
    const safeUrl = (url) => (typeof url === 'string' && (url.startsWith('https://') || url.startsWith('http://'))) ? url : null;

    const pillStyle = (bg, fg) => ({
        display: 'inline-flex', alignItems: 'center', gap: '4px',
        fontFamily: typography.fontMono, fontSize: typography.size.xs,
        padding: '2px 8px', borderRadius: '4px',
        background: bg, color: fg, textDecoration: 'none',
        whiteSpace: 'nowrap',
    });

    return html`
        <div style=${{
            display: 'flex', alignItems: 'center', gap: '8px',
            padding: '8px 0', flexWrap: 'wrap',
            borderBottom: `1px solid ${colors.border}`, marginBottom: '16px',
        }}>
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
                    style=${pillStyle('rgba(124, 90, 246, 0.15)', colors.accent)}
                    class="foreman-task-pr-link">
                    PR ↗
                </a>
            ` : null}

            ${task.conversation_id ? html`
                <a href=${routes.conversation(task.conversation_id)}
                    style=${pillStyle('rgba(99, 102, 241, 0.12)', '#818cf8')}
                    class="foreman-task-conv-link">
                    💬 ${task.conversation_id}
                </a>
            ` : null}

            ${task.claude_chat_url && safeUrl(task.claude_chat_url) ? html`
                <a href=${safeUrl(task.claude_chat_url)} target="_blank" rel="noopener"
                    style=${pillStyle('rgba(249, 115, 22, 0.12)', '#fb923c')}
                    class="foreman-task-claude-link">
                    Claude ↗
                </a>
            ` : null}
        </div>
    `;
}

// ── Chain Strip ─────────────────────────────────────────────

function ChainStrip({ task }) {
    const [chain, setChain] = useState(null);

    useEffect(() => {
        if (!task?.id) return;
        api.getChain(task.id)
            .then(data => {
                const list = data?.chain || [];
                if (list.length > 1) setChain(list);
            })
            .catch(() => {});
    }, [task?.id]);

    if (!chain || chain.length <= 1) return null;

    const MAX_NODES = 7;
    const truncated = chain.length > MAX_NODES;
    const displayNodes = truncated ? chain.slice(0, MAX_NODES) : chain;

    const containerStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '0',
        padding: '10px 0',
        marginBottom: '12px',
        overflowX: 'auto',
    };

    return html`
        <div style=${containerStyle}>
            ${displayNodes.map((node, i) => {
                const isCurrent = node.id === task.id;
                const color = statusColors[node.status] || colors.textTertiary;
                const name = (node.goal || node.id.split('/').pop() || '').slice(0, 20);

                return html`
                    ${i > 0 ? html`
                        <div style=${{
                            width: '20px', height: '2px',
                            background: colors.border, flexShrink: 0,
                        }} />
                    ` : null}
                    <a key=${node.id}
                       href=${routes.task(node.id)}
                       style=${{
                           display: 'inline-flex', alignItems: 'center', gap: '5px',
                           padding: '4px 8px', borderRadius: layout.borderRadius.sm,
                           border: isCurrent ? '1px solid ' + colors.accent : '1px solid ' + colors.borderSubtle,
                           background: isCurrent ? colors.accentBg : 'transparent',
                           textDecoration: 'none', whiteSpace: 'nowrap', flexShrink: 0,
                       }}
                       class="foreman-chain-node"
                    >
                        <span style=${{
                            width: '6px', height: '6px', borderRadius: '50%',
                            background: color, flexShrink: 0,
                        }} />
                        <span style=${{
                            fontFamily: typography.fontMono, fontSize: typography.size.xs,
                            color: isCurrent ? colors.text : colors.textSecondary,
                            overflow: 'hidden', textOverflow: 'ellipsis',
                        }}>${name}</span>
                    </a>
                `;
            })}
            ${truncated ? html`
                <div style=${{
                    width: '20px', height: '2px',
                    background: colors.border, flexShrink: 0,
                }} />
                <span style=${{
                    fontFamily: typography.fontMono, fontSize: typography.size.xs,
                    color: colors.textTertiary, padding: '4px 6px',
                }}>… +${chain.length - MAX_NODES}</span>
            ` : null}
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

function ActionToolbar({ task, onAction }) {
    const actions = [];

    const btn = (action, label, bg, fg) => html`
        <button key=${action} onClick=${() => onAction(action, task.id)}
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

    if (task.status === 'ready' && task.held) {
        actions.push(btn('approve', 'Approve', colors.greenBg, colors.green));
    } else if (task.status === 'ready') {
        actions.push(btn('dispatch', 'Dispatch', colors.blueBg, colors.blue));
    }
    if (task.status === 'working') {
        actions.push(btn('cancel', 'Cancel', colors.redBg, colors.red));
    }
    if (['turns-exhausted', 'needs-review', 'rate-limited'].includes(task.status)) {
        actions.push(btn('resume', 'Resume', colors.greenBg, colors.green));
    }
    if (['failed', 'needs-review', 'completed'].includes(task.status)) {
        actions.push(btn('retry', 'Retry', colors.yellowBg, colors.yellow));
    }
    if (task.gate_status && ['testing', 'test-passed', 'reviewing', 'test-failed', 'review-failed'].includes(task.gate_status)) {
        actions.push(btn('skip-gate', 'Skip Gate', 'rgba(139, 92, 246, 0.12)', '#8b5cf6'));
    }
    if (task.status === 'completed' && task.gate_status === 'passed') {
        actions.push(btn('advance-chain', 'Advance', colors.accentBg, colors.accent));
    }
    if (['completed', 'failed'].includes(task.status)) {
        actions.push(btn('close', 'Close', statusBgs.cancelled, colors.textTertiary));
    }
    if (task.worktree_path) {
        actions.push(btn('release-worktree', 'Release WT', 'rgba(249, 115, 22, 0.12)', '#fb923c'));
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

function AttemptSessionLog({ taskId, attemptNumber, isLive }) {
    const [isOpen, setIsOpen] = useState(false);
    const [entries, setEntries] = useState(null);
    const [loading, setLoading] = useState(false);
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

                ${entries !== null ? html`
                    <span style=${{
                        fontFamily: typography.fontMono, fontSize: typography.size.xs,
                        color: colors.textTertiary, marginLeft: 'auto',
                    }}>${entries.length} entries</span>
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

                    ${entries && entries.map((entry, i) => {
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
                overflow: 'hidden', textOverflow: 'ellipsis',
                whiteSpace: expanded ? 'pre-wrap' : 'nowrap', flex: 1,
                wordBreak: expanded ? 'break-word' : 'normal',
            }}>
                ${expanded ? fullContent : (preview.length > 120 ? preview.slice(0, 117) + '…' : preview)}
            </span>
            ${isExpandable ? html`
                <span style=${{ color: colors.textTertiary, flexShrink: 0, fontSize: '9px' }}>
                    ${expanded ? '▾' : '▸'}
                </span>
            ` : null}
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
                    ${row('Auto Test', task.auto_test ? 'Yes' : 'No')}
                    ${row('Auto Review', task.auto_review ? 'Yes' : 'No')}
                    ${row('Auto PR', task.auto_pr ? 'Yes' : 'No')}
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

// ── Confirm Dialog ──────────────────────────────────────────

const CONFIRM_TEXT = {
    cancel: { title: 'Cancel Task', body: 'Kill the running CC process? Code changes preserved.' },
    retry: { title: 'Retry Task', body: 'Start a fresh CC session? Previous context will be lost.' },
    resume: { title: 'Resume Session', body: 'Continue the existing CC session with full history?' },
    close: { title: 'Close Task', body: 'Destroy worktree and delete branch? Cannot be undone.' },
    'skip-gate': { title: 'Skip Gate', body: 'Manually mark gate as passed, bypassing validation?' },
    'advance-chain': { title: 'Advance Chain', body: 'Dispatch the next dependent task in the chain?' },
    'release-worktree': { title: 'Release Worktree', body: 'Detach worktree without closing the task?' },
    approve: { title: 'Approve & Dispatch', body: 'Release this held task for dispatch?' },
    dispatch: { title: 'Dispatch Task', body: 'Create worktree and launch CC session?' },
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

export function TaskView({ id, mode = 'expanded', onClose }) {
    const [task, setTask] = useState(null);
    const [attempts, setAttempts] = useState(null);
    const [blockerTask, setBlockerTask] = useState(null);
    const [chain, setChain] = useState(null);
    const [error, setError] = useState(null);
    const [confirmAction, setConfirmAction] = useState(null);
    const mountedRef = useRef(true);
    const loadedRef = useRef(false);

    const loadTask = useCallback(async () => {
        try {
            const data = await api.getTask(id);
            if (mountedRef.current) { setTask(data); setError(null); loadedRef.current = true; }
        } catch (e) {
            if (mountedRef.current) {
                // Only show error on initial load failure. Once loaded, poll errors
                // are silently logged to avoid flashing the error screen.
                if (!loadedRef.current) setError(e.message);
                else console.warn('Poll error:', e.message);
            }
        }
    }, [id]);

    const loadAttempts = useCallback(async () => {
        try {
            const data = await api.getAttempts(id);
            if (mountedRef.current && data) {
                // API returns { attempts: [...] } or just [...]
                const list = Array.isArray(data) ? data : (data.attempts || []);
                setAttempts(list);
            }
        } catch (e) {
            console.warn('Attempts load error:', e.message);
            if (mountedRef.current) setAttempts([]);
        }
    }, [id]);

    // Load blocker task if depends_on
    useEffect(() => {
        if (!task?.depends_on) { setBlockerTask(null); return; }
        api.getTask(task.depends_on)
            .then(d => mountedRef.current && setBlockerTask(d))
            .catch(() => mountedRef.current && setBlockerTask(null));
    }, [task?.depends_on]);

    // Load chain for compact mode chain position
    useEffect(() => {
        if (!id) return;
        api.getChain(id)
            .then(data => {
                const list = data?.chain || [];
                if (mountedRef.current) setChain(list.length > 1 ? list : null);
            })
            .catch(() => mountedRef.current && setChain(null));
    }, [id]);

    // Initial load
    useEffect(() => {
        mountedRef.current = true;
        loadedRef.current = false;
        setTask(null);
        setAttempts(null);
        setError(null);
        setBlockerTask(null);
        setChain(null);
        loadTask();
        loadAttempts();
        return () => { mountedRef.current = false; };
    }, [id]);

    // Polling — task status at 5s when active, attempts at 10s only while working
    useEffect(() => {
        if (!task) return;
        const gateActive = ['testing', 'test-passed', 'reviewing'].includes(task.gate_status);
        const shouldPoll = task.status === 'working' || task.status === 'needs-review' || gateActive;
        if (!shouldPoll) return;

        const taskTimer = setInterval(loadTask, 5000);
        // Only poll attempts while actively working (expensive — re-reads all messages)
        const attemptTimer = task.status === 'working'
            ? setInterval(loadAttempts, 10000)
            : null;
        return () => {
            clearInterval(taskTimer);
            if (attemptTimer) clearInterval(attemptTimer);
        };
    }, [task?.status, task?.gate_status, loadTask, loadAttempts]);

    // Action handler
    const handleAction = useCallback((action, taskId) => {
        setConfirmAction(action);
    }, []);

    const executeAction = useCallback(async () => {
        if (!confirmAction || !task) return;
        const action = confirmAction;
        setConfirmAction(null);
        try {
            const actionMap = {
                cancel: () => api.cancelTask(id),
                retry: () => api.retryTask(id),
                resume: () => api.resumeTask(id),
                close: () => api.closeTask(id),
                'skip-gate': () => api.skipGate(id),
                'advance-chain': () => api.advanceChain(id),
                'release-worktree': () => api.releaseWorktree(id),
                approve: () => api.approveTask(id),
                dispatch: () => api.dispatchTask(id),
            };
            const fn = actionMap[action];
            if (fn) await fn();
            // Reload after action
            setTimeout(() => { loadTask(); loadAttempts(); }, 500);
        } catch (e) {
            console.error('Action error:', e);
        }
    }, [confirmAction, id, task, loadTask, loadAttempts]);

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
                                style=${pillStyle('rgba(124, 90, 246, 0.15)', colors.accent)}
                                class="foreman-task-pr-link"
                                onClick=${e => e.stopPropagation()}>
                                PR ↗
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
                <${ActionToolbar} task=${task} onAction=${handleAction} />

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

            <${StatusLine} task=${task} />
            <${GitFlowBar} task=${task} />
            <${ChainStrip} task=${task} />
            <${BlockedBy} task=${task} blockerTask=${blockerTask} />
            <${ActionToolbar} task=${task} onAction=${handleAction} />

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

            <${MessageInput} taskId=${id} onMessageSent=${() => { loadTask(); loadAttempts(); }} />

            <${GateDotsSection} task=${task} />
            <${ChecklistDrawer} task=${task} />
            <${DetailsDrawer} task=${task} />

            <${ConfirmOverlay} action=${confirmAction} onConfirm=${executeAction} onCancel=${() => setConfirmAction(null)} />
        </div>
    `;
}
