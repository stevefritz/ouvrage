// Foreman Conversation View — Clean thread reader
// Chronological posts, pinned message at top, type icons, post input
// Spec: foreman-design conversation, message 2787

import { h } from 'https://esm.sh/preact@10.25.4';
import htm from 'https://esm.sh/htm@3.1.1';
import { useState, useEffect, useRef, useCallback } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { colors, typography, layout, animation } from '../tokens.js';
import { relativeTime } from '../components/utils.js';
import { routes } from '../router.js';

const html = htm.bind(h);

const POLL_INTERVAL_MS = 15_000;

// ── Message type metadata ────────────────────────────────────

const MSG_META = {
    spec:          { icon: '📌', label: 'Spec' },
    plan:          { icon: '📋', label: 'Plan' },
    progress:      { icon: '⚡', label: 'Progress' },
    result:        { icon: '✅', label: 'Result' },
    review:        { icon: '🔍', label: 'Review' },
    question:      { icon: '❓', label: 'Question' },
    answer:        { icon: '💬', label: 'Answer' },
    handoff:       { icon: '🤝', label: 'Handoff' },
    'test-result': { icon: '🧪', label: 'Tests' },
    note:          { icon: '📝', label: 'Note' },
    status:        { icon: '📊', label: 'Status' },
};

function getMsgMeta(type) {
    return MSG_META[type] || MSG_META.note;
}

// ── Markdown rendering ───────────────────────────────────────

let _domPurifyWarned = false;
function sanitize(dirty) {
    if (typeof DOMPurify?.sanitize === 'function') return DOMPurify.sanitize(dirty);
    if (!_domPurifyWarned) {
        console.warn('[ConversationView] DOMPurify not loaded');
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

// ── Message component ────────────────────────────────────────

function ConversationMessage({ msg, isPinned }) {
    const [expanded, setExpanded] = useState(isPinned);
    const meta = getMsgMeta(msg.type);
    const ts = msg.created_at;

    const containerStyle = {
        display: 'flex',
        gap: '12px',
        padding: '12px 16px',
        borderBottom: `1px solid ${colors.border}22`,
        ...(isPinned ? {
            background: colors.surfaceActive,
            borderLeft: `3px solid ${colors.accent}`,
            borderRadius: `0 ${layout.borderRadius.md} ${layout.borderRadius.md} 0`,
            marginBottom: '8px',
        } : {}),
    };

    const iconStyle = {
        fontSize: '14px',
        flexShrink: 0,
        width: '20px',
        textAlign: 'center',
        marginTop: '2px',
    };

    const bodyStyle = {
        flex: 1,
        minWidth: 0,
        display: 'flex',
        flexDirection: 'column',
        gap: '4px',
    };

    const headerStyle = {
        display: 'flex',
        alignItems: 'baseline',
        gap: '8px',
        flexWrap: 'wrap',
    };

    const authorStyle = {
        fontSize: typography.size.sm,
        fontWeight: typography.weight.medium,
        color: colors.text,
    };

    const typeStyle = {
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        fontFamily: typography.fontMono,
    };

    const timeStyle = {
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        marginLeft: 'auto',
        flexShrink: 0,
    };

    const titleStyle = {
        fontSize: typography.size.sm,
        fontWeight: typography.weight.medium,
        color: colors.text,
        cursor: 'pointer',
    };

    // Collapsed: show title or first line
    const previewText = msg.title || (msg.content || '').split('\n')[0].replace(/^#+\s*/, '').slice(0, 120);
    const hasMore = (msg.content || '').length > 120 || (msg.content || '').includes('\n');

    return html`
        <div style=${containerStyle} class="foreman-conv-message">
            <span style=${iconStyle}>${meta.icon}</span>
            <div style=${bodyStyle}>
                <div style=${headerStyle}>
                    <span style=${authorStyle}>${msg.author || 'unknown'}</span>
                    <span style=${typeStyle}>${meta.label}</span>
                    ${isPinned ? html`
                        <span style=${{
                            fontSize: typography.size.xs,
                            color: colors.accent,
                            fontWeight: typography.weight.medium,
                        }}>📌 Pinned</span>
                    ` : null}
                    <span style=${timeStyle}>${relativeTime(ts)}</span>
                </div>

                ${expanded ? html`
                    ${msg.title ? html`<div style=${titleStyle}>${msg.title}</div>` : null}
                    <div style=${{
                        fontSize: typography.size.sm,
                        color: colors.textSecondary,
                        lineHeight: typography.lineHeight.relaxed,
                    }}
                        dangerouslySetInnerHTML=${{ __html: renderMarkdown(msg.content) }}
                    />
                    ${hasMore && !isPinned ? html`
                        <button onClick=${() => setExpanded(false)} style=${{
                            background: 'none',
                            border: 'none',
                            color: colors.textTertiary,
                            fontSize: typography.size.xs,
                            cursor: 'pointer',
                            padding: '2px 0',
                            textAlign: 'left',
                        }}>▴ Collapse</button>
                    ` : null}
                ` : html`
                    <div style=${{
                        fontSize: typography.size.sm,
                        color: colors.textSecondary,
                        cursor: hasMore ? 'pointer' : 'default',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                    }} onClick=${hasMore ? () => setExpanded(true) : null}>
                        ${previewText}${hasMore ? '…' : ''}
                    </div>
                `}
            </div>
        </div>
    `;
}

// ── Post input ───────────────────────────────────────────────

const POST_TYPES = ['note', 'spec', 'plan', 'question', 'answer', 'review'];

function PostInput({ conversationId, onPosted }) {
    const [content, setContent] = useState('');
    const [type, setType] = useState('note');
    const [sending, setSending] = useState(false);
    const [error, setError] = useState(null);
    const textRef = useRef(null);

    const handleSubmit = useCallback(async () => {
        const text = content.trim();
        if (!text || sending) return;

        setSending(true);
        setError(null);
        try {
            await api.postConversationMessage(conversationId, text, type);
            setContent('');
            if (onPosted) onPosted();
        } catch (e) {
            setError(e.message || 'Failed to send');
        } finally {
            setSending(false);
        }
    }, [content, type, conversationId, sending, onPosted]);

    const handleKeyDown = useCallback((e) => {
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            handleSubmit();
        }
    }, [handleSubmit]);

    const containerStyle = {
        padding: '16px',
        borderTop: `1px solid ${colors.border}`,
        display: 'flex',
        flexDirection: 'column',
        gap: '8px',
        background: colors.surface,
    };

    const topRowStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
    };

    const selectStyle = {
        background: colors.surfaceActive,
        color: colors.text,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.sm,
        padding: '4px 8px',
        fontSize: typography.size.xs,
        fontFamily: typography.fontMono,
        cursor: 'pointer',
    };

    const textareaStyle = {
        background: colors.bg,
        color: colors.text,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.md,
        padding: '10px 12px',
        fontSize: typography.size.sm,
        fontFamily: typography.fontBody,
        lineHeight: typography.lineHeight.normal,
        resize: 'vertical',
        minHeight: '60px',
        width: '100%',
        boxSizing: 'border-box',
    };

    const btnStyle = {
        background: colors.accent,
        color: '#fff',
        border: 'none',
        borderRadius: layout.borderRadius.sm,
        padding: '6px 16px',
        fontSize: typography.size.sm,
        fontWeight: typography.weight.medium,
        cursor: sending ? 'not-allowed' : 'pointer',
        opacity: sending ? 0.6 : 1,
        alignSelf: 'flex-end',
    };

    return html`
        <div style=${containerStyle}>
            <div style=${topRowStyle}>
                <select style=${selectStyle} value=${type} onChange=${e => setType(e.target.value)}>
                    ${POST_TYPES.map(t => html`
                        <option key=${t} value=${t}>${getMsgMeta(t).icon} ${getMsgMeta(t).label}</option>
                    `)}
                </select>
                <span style=${{ fontSize: typography.size.xs, color: colors.textTertiary }}>
                    ⌘+Enter to send
                </span>
            </div>
            <textarea
                ref=${textRef}
                style=${textareaStyle}
                placeholder="Write a message..."
                value=${content}
                onInput=${e => setContent(e.target.value)}
                onKeyDown=${handleKeyDown}
                disabled=${sending}
            />
            ${error ? html`
                <div style=${{ fontSize: typography.size.xs, color: colors.red }}>${error}</div>
            ` : null}
            <button style=${btnStyle} onClick=${handleSubmit} disabled=${sending || !content.trim()}>
                ${sending ? 'Sending…' : 'Send'}
            </button>
        </div>
    `;
}

// ── Main view ────────────────────────────────────────────────

export function ConversationView({ id }) {
    const [thread, setThread] = useState(null);
    const [error, setError] = useState(null);
    const [cursor, setCursor] = useState(null);
    const mountedRef = useRef(true);
    const messagesEndRef = useRef(null);

    useEffect(() => {
        mountedRef.current = true;
        return () => { mountedRef.current = false; };
    }, []);

    const loadFull = useCallback(async () => {
        try {
            const data = await api.getConversation(id);
            if (!mountedRef.current) return;
            setThread(data);
            setCursor(data.cursor || null);
            setError(null);
        } catch (e) {
            if (mountedRef.current) setError(e.message || 'Failed to load');
        }
    }, [id]);

    // Initial load
    useEffect(() => {
        setThread(null);
        setError(null);
        setCursor(null);
        loadFull();
    }, [loadFull]);

    // Polling for new messages
    useEffect(() => {
        if (!cursor) return;
        const poll = async () => {
            try {
                const data = await api.getConversation(id, { after: cursor });
                if (!mountedRef.current) return;
                const newMsgs = data.messages || [];
                if (newMsgs.length > 0) {
                    setThread(prev => ({
                        ...prev,
                        messages: [...(prev?.messages || []), ...newMsgs],
                    }));
                    setCursor(data.cursor);
                }
            } catch { /* ignore poll errors */ }
        };
        const timer = setInterval(poll, POLL_INTERVAL_MS);
        return () => clearInterval(timer);
    }, [id, cursor]);

    const handlePosted = useCallback(() => {
        // Reload full thread after posting
        loadFull();
    }, [loadFull]);

    // ── Render ──

    const messages = thread?.messages || [];
    const pinned = messages.filter(m => m._pinned_marker || m.pinned);
    const regular = messages.filter(m => !m._pinned_marker && !m.pinned);

    // Derive conversation metadata from the first pinned message or thread
    const convGoal = pinned[0]?.title || pinned[0]?.content?.split('\n')[0]?.replace(/^#+\s*/, '').slice(0, 100) || id;

    const pageStyle = {
        display: 'flex',
        flexDirection: 'column',
        gap: '0',
        minHeight: 'calc(100vh - 52px)',
    };

    const backLinkStyle = {
        fontSize: typography.size.sm,
        color: colors.textTertiary,
        textDecoration: 'none',
        display: 'inline-flex',
        alignItems: 'center',
        gap: '4px',
        marginBottom: '16px',
        transition: `color ${animation.durationFast}`,
    };

    const headerStyle = {
        padding: '0 0 16px',
        borderBottom: `1px solid ${colors.border}`,
        marginBottom: '0',
    };

    const titleStyle = {
        fontFamily: typography.fontBody,
        fontSize: typography.size.xl,
        fontWeight: typography.weight.semibold,
        color: colors.text,
        letterSpacing: '-0.02em',
        margin: '0 0 4px',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
    };

    const metaStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '12px',
        fontSize: typography.size.xs,
        color: colors.textTertiary,
    };

    const errorStyle = {
        padding: '24px',
        borderRadius: layout.borderRadius.md,
        background: colors.redBg,
        border: `1px solid ${colors.red}44`,
        color: colors.red,
        fontSize: typography.size.sm,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: '16px',
        margin: '24px 0',
    };

    const retryBtnStyle = {
        padding: '4px 12px',
        borderRadius: layout.borderRadius.sm,
        background: `${colors.red}22`,
        border: `1px solid ${colors.red}44`,
        color: colors.red,
        fontSize: typography.size.sm,
        cursor: 'pointer',
        flexShrink: 0,
    };

    // Loading skeleton
    if (!thread && !error) {
        return html`
            <div style=${pageStyle}>
                <a href="#/" style=${backLinkStyle} class="foreman-back-link">← Back</a>
                <div style=${headerStyle}>
                    <div style=${{ height: '22px', width: '60%', background: colors.surfaceActive, borderRadius: '4px' }} class="foreman-skeleton" />
                    <div style=${{ height: '12px', width: '30%', background: colors.surfaceActive, borderRadius: '4px', marginTop: '8px' }} class="foreman-skeleton" />
                </div>
                ${[1,2,3].map(i => html`
                    <div key=${i} style=${{ padding: '12px 16px', display: 'flex', gap: '12px' }}>
                        <div style=${{ width: '20px', height: '20px', borderRadius: '50%', background: colors.surfaceActive }} class="foreman-skeleton" />
                        <div style=${{ flex: 1 }}>
                            <div style=${{ height: '12px', width: '40%', background: colors.surfaceActive, borderRadius: '4px', marginBottom: '8px' }} class="foreman-skeleton" />
                            <div style=${{ height: '12px', width: '80%', background: colors.surfaceActive, borderRadius: '4px' }} class="foreman-skeleton" />
                        </div>
                    </div>
                `)}
            </div>
        `;
    }

    return html`
        <div style=${pageStyle}>
            <a href="#/" style=${backLinkStyle} class="foreman-back-link">← Back</a>

            ${error ? html`
                <div style=${errorStyle}>
                    <span>Failed to load: ${error}</span>
                    <button style=${retryBtnStyle} onClick=${loadFull}>Retry</button>
                </div>
            ` : null}

            <div style=${headerStyle}>
                <h1 style=${titleStyle}>${id}</h1>
                <div style=${metaStyle}>
                    <span>${messages.length} message${messages.length !== 1 ? 's' : ''}</span>
                    ${pinned.length > 0 ? html`<span>📌 ${pinned.length} pinned</span>` : null}
                </div>
            </div>

            <!-- Pinned messages -->
            ${pinned.map(msg => html`
                <${ConversationMessage} key=${msg.id} msg=${msg} isPinned=${true} />
            `)}

            <!-- Regular messages (chronological) -->
            <div style=${{ flex: 1 }}>
                ${regular.length === 0 && pinned.length === 0 ? html`
                    <div style=${{
                        display: 'flex',
                        flexDirection: 'column',
                        alignItems: 'center',
                        justifyContent: 'center',
                        padding: '60px 24px',
                        gap: '8px',
                        textAlign: 'center',
                    }}>
                        <div style=${{ fontSize: '24px', marginBottom: '4px' }}>💬</div>
                        <div style=${{ fontSize: typography.size.sm, color: colors.textSecondary }}>
                            No messages yet
                        </div>
                        <div style=${{ fontSize: typography.size.xs, color: colors.textTertiary }}>
                            Start the conversation below
                        </div>
                    </div>
                ` : regular.map(msg => html`
                    <${ConversationMessage} key=${msg.id} msg=${msg} isPinned=${false} />
                `)}
                <div ref=${messagesEndRef} />
            </div>

            <!-- Post input -->
            <${PostInput} conversationId=${id} onPosted=${handlePosted} />
        </div>
    `;
}
