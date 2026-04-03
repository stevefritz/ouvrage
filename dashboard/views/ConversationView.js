// Foreman Conversation View — TOC sidebar, scoped search, newest-first, markdown
// Routes:
//   #/conversation/:id            (standalone, back → #/)
//   #/project/:pid/conversation/:id  (project-scoped, back → #/project/:pid/conversations)

import { h } from 'https://esm.sh/preact@10.25.4';
import htm from 'https://esm.sh/htm@3.1.1';
import { useState, useEffect, useRef, useCallback, useMemo } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { colors, typography, layout, animation } from '../tokens.js';
import { relativeTime } from '../components/utils.js';
import { routes } from '../router.js';

const html = htm.bind(h);

const POLL_INTERVAL_MS = 15_000;
const TOC_MSG_LIMIT = 8;

// ── Message type metadata ─────────────────────────────────────

const MSG_META = {
    spec:          { label: 'Spec',     bg: 'rgba(217,119,6,0.18)',    color: '#d97706' },
    plan:          { label: 'Plan',     bg: 'rgba(139,92,246,0.18)',   color: '#8b5cf6' },
    progress:      { label: 'Progress', bg: 'rgba(77,163,255,0.15)',   color: '#4da3ff' },
    result:        { label: 'Result',   bg: 'rgba(61,214,140,0.15)',   color: '#3dd68c' },
    review:        { label: 'Review',   bg: 'rgba(236,72,153,0.15)',   color: '#ec4899' },
    question:      { label: 'Question', bg: 'rgba(245,166,35,0.15)',   color: '#f5a623' },
    answer:        { label: 'Answer',   bg: 'rgba(77,163,255,0.15)',   color: '#4da3ff' },
    handoff:       { label: 'Handoff',  bg: 'rgba(139,92,246,0.18)',   color: '#8b5cf6' },
    'test-result': { label: 'Tests',    bg: 'rgba(61,214,140,0.15)',   color: '#3dd68c' },
    note:          { label: 'Note',     bg: 'rgba(136,126,114,0.15)',  color: '#b0a89e' },
    status:        { label: 'Status',   bg: 'rgba(136,126,114,0.12)',  color: '#887e72' },
};

function getMsgMeta(type) {
    return MSG_META[type] || MSG_META.note;
}

// ── Markdown rendering ────────────────────────────────────────

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

// Extract ## and ### headings from markdown text
function extractHeadings(markdown) {
    if (!markdown) return [];
    const headings = [];
    const lines = markdown.split('\n');
    for (const line of lines) {
        const m = line.match(/^(#{2,3})\s+(.+)$/);
        if (m) {
            headings.push({ level: m[1].length, text: m[2].trim() });
        }
    }
    return headings;
}

// ── TypeBadge ────────────────────────────────────────────────

function TypeBadge({ type }) {
    const meta = getMsgMeta(type);
    return html`
        <span style=${{
            display: 'inline-block',
            padding: '2px 7px',
            borderRadius: layout.borderRadius.pill,
            background: meta.bg,
            color: meta.color,
            fontSize: typography.size.xs,
            fontWeight: typography.weight.medium,
            fontFamily: typography.fontMono,
            lineHeight: '1.4',
            flexShrink: 0,
        }}>${meta.label}</span>
    `;
}

// ── Message component ─────────────────────────────────────────

function ConversationMessage({ msg, isPinned, highlighted }) {
    const renderedContent = useMemo(() => renderMarkdown(msg.content), [msg.content]);

    const containerStyle = {
        padding: '14px 16px',
        borderBottom: `1px solid ${colors.border}22`,
        ...(isPinned ? {
            background: `${colors.accentBg || 'rgba(217,119,6,0.07)'}`,
            borderLeft: `3px solid #d97706`,
            marginBottom: '2px',
        } : {}),
        ...(highlighted ? {
            background: `rgba(77,163,255,0.08)`,
            borderLeft: `3px solid ${colors.blue}`,
        } : {}),
    };

    const headerStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        flexWrap: 'wrap',
        marginBottom: msg.title ? '6px' : '8px',
    };

    const authorStyle = {
        fontSize: typography.size.sm,
        fontWeight: typography.weight.medium,
        color: colors.text,
    };

    const timeStyle = {
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        marginLeft: 'auto',
        flexShrink: 0,
    };

    const titleStyle = {
        fontSize: '15px',
        fontWeight: typography.weight.medium,
        color: colors.text,
        marginBottom: '6px',
        lineHeight: typography.lineHeight.tight,
    };

    const contentStyle = {
        fontSize: typography.size.sm,
        color: colors.textSecondary,
        lineHeight: typography.lineHeight.relaxed,
    };

    return html`
        <div
            id=${'msg-' + msg.id}
            style=${containerStyle}
            class="foreman-conv-message"
        >
            <div style=${headerStyle}>
                <span style=${authorStyle}>${msg.author || 'unknown'}</span>
                <${TypeBadge} type=${msg.type || 'note'} />
                ${isPinned ? html`
                    <span style=${{
                        fontSize: typography.size.xs,
                        color: '#d97706',
                        fontWeight: typography.weight.medium,
                    }}>📌 Pinned</span>
                ` : null}
                <span style=${timeStyle}>${relativeTime(msg.created_at)}</span>
            </div>

            ${msg.title ? html`<div style=${titleStyle}>${msg.title}</div>` : null}

            <div
                style=${contentStyle}
                dangerouslySetInnerHTML=${{ __html: renderedContent }}
            />
        </div>
    `;
}

// ── TOC Sidebar ───────────────────────────────────────────────

function TocSidebar({ pinnedHeadings, messages, onScrollToHeading, onScrollToMsg, collapsed, onToggle }) {
    const [showAll, setShowAll] = useState(false);
    const visibleMsgs = showAll ? messages : messages.slice(0, TOC_MSG_LIMIT);
    const hiddenCount = messages.length - TOC_MSG_LIMIT;

    const sidebarStyle = {
        width: layout.sidebarWidth,
        flexShrink: 0,
        borderRight: `1px solid ${colors.border}`,
        display: 'flex',
        flexDirection: 'column',
        overflowY: 'auto',
        maxHeight: 'calc(100vh - 52px)',
        position: 'sticky',
        top: '0',
        alignSelf: 'flex-start',
        padding: '12px 0',
    };

    const sectionLabelStyle = {
        fontSize: typography.size.xs,
        fontWeight: typography.weight.semibold,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        color: colors.textTertiary,
        padding: '4px 14px 6px',
        marginTop: '8px',
    };

    const tocItemStyle = (isHeading) => ({
        display: 'block',
        width: '100%',
        textAlign: 'left',
        background: 'none',
        border: 'none',
        cursor: 'pointer',
        padding: isHeading ? '3px 14px 3px 20px' : '4px 14px',
        fontSize: typography.size.xs,
        color: colors.textSecondary,
        lineHeight: '1.4',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        transition: `color ${animation.durationFast}`,
        fontFamily: typography.fontBody,
    });

    return html`
        <nav style=${sidebarStyle} class="foreman-toc-sidebar" aria-label="Table of contents">
            ${pinnedHeadings.length > 0 ? html`
                <div style=${sectionLabelStyle}>Pinned</div>
                ${pinnedHeadings.map((h, i) => html`
                    <button
                        key=${i}
                        style=${tocItemStyle(true)}
                        class="foreman-toc-item"
                        onClick=${() => onScrollToHeading(h.text)}
                        title=${h.text}
                    >${h.level === 3 ? '  · ' : ''}${h.text}</button>
                `)}
            ` : null}

            ${messages.length > 0 ? html`
                <div style=${sectionLabelStyle}>Messages</div>
                ${visibleMsgs.map(msg => html`
                    <button
                        key=${msg.id}
                        style=${tocItemStyle(false)}
                        class="foreman-toc-item"
                        onClick=${() => onScrollToMsg(msg.id)}
                        title=${msg.title || msg.content?.split('\n')[0]?.replace(/^#+\s*/,'') || msg.id}
                    >${msg.title || (msg.content || '').split('\n')[0].replace(/^#+\s*/,'').slice(0,40) || msg.id}</button>
                `)}
                ${!showAll && hiddenCount > 0 ? html`
                    <button
                        style=${{ ...tocItemStyle(false), color: colors.accent, fontWeight: typography.weight.medium }}
                        onClick=${() => setShowAll(true)}
                    >+${hiddenCount} more</button>
                ` : null}
            ` : null}
        </nav>
    `;
}

// ── Mobile TOC Dropdown ───────────────────────────────────────

function MobileTocDropdown({ pinnedHeadings, messages, onScrollToHeading, onScrollToMsg }) {
    const [open, setOpen] = useState(false);
    const [showAll, setShowAll] = useState(false);
    const visibleMsgs = showAll ? messages : messages.slice(0, TOC_MSG_LIMIT);
    const hiddenCount = messages.length - TOC_MSG_LIMIT;

    const btnStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '6px',
        padding: '7px 12px',
        background: colors.surfaceActive,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.md,
        color: colors.text,
        fontSize: typography.size.sm,
        cursor: 'pointer',
        fontFamily: typography.fontBody,
    };

    const dropdownStyle = {
        position: 'absolute',
        top: '100%',
        left: 0,
        right: 0,
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.md,
        marginTop: '4px',
        zIndex: 100,
        maxHeight: '300px',
        overflowY: 'auto',
        padding: '8px 0',
    };

    const itemStyle = {
        display: 'block',
        width: '100%',
        textAlign: 'left',
        background: 'none',
        border: 'none',
        cursor: 'pointer',
        padding: '6px 14px',
        fontSize: typography.size.xs,
        color: colors.textSecondary,
        fontFamily: typography.fontBody,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
    };

    const labelStyle = {
        padding: '4px 14px 2px',
        fontSize: typography.size.xs,
        fontWeight: typography.weight.semibold,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        color: colors.textTertiary,
    };

    if (pinnedHeadings.length === 0 && messages.length === 0) return null;

    return html`
        <div style={{ position: 'relative', marginBottom: '8px' }} class="foreman-toc-mobile">
            <button style=${btnStyle} onClick=${() => setOpen(o => !o)} aria-expanded=${open}>
                ☰ Contents ${open ? '▴' : '▾'}
            </button>
            ${open ? html`
                <div style=${dropdownStyle}>
                    ${pinnedHeadings.length > 0 ? html`
                        <div style=${labelStyle}>Pinned</div>
                        ${pinnedHeadings.map((h, i) => html`
                            <button key=${i} style=${itemStyle} onClick=${() => { onScrollToHeading(h.text); setOpen(false); }}>
                                ${h.level === 3 ? '  · ' : ''}${h.text}
                            </button>
                        `)}
                    ` : null}
                    ${messages.length > 0 ? html`
                        <div style=${labelStyle}>Messages</div>
                        ${visibleMsgs.map(msg => html`
                            <button key=${msg.id} style=${itemStyle} onClick=${() => { onScrollToMsg(msg.id); setOpen(false); }}>
                                ${msg.title || (msg.content || '').split('\n')[0].replace(/^#+\s*/,'').slice(0,40) || msg.id}
                            </button>
                        `)}
                        ${!showAll && hiddenCount > 0 ? html`
                            <button style=${{ ...itemStyle, color: colors.accent }} onClick=${() => setShowAll(true)}>
                                +${hiddenCount} more
                            </button>
                        ` : null}
                    ` : null}
                </div>
            ` : null}
        </div>
    `;
}

// ── Post input ────────────────────────────────────────────────

const POST_TYPES = ['note', 'spec', 'plan', 'question', 'answer', 'review'];

function PostInput({ conversationId, onPosted }) {
    const [content, setContent] = useState('');
    const [type, setType] = useState('note');
    const [sending, setSending] = useState(false);
    const [error, setError] = useState(null);

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
        padding: '14px 16px',
        borderTop: `1px solid ${colors.border}`,
        display: 'flex',
        flexDirection: 'column',
        gap: '8px',
        background: colors.surface,
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
            <div style=${{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <select style=${selectStyle} value=${type} onChange=${e => setType(e.target.value)}>
                    ${POST_TYPES.map(t => html`
                        <option key=${t} value=${t}>${getMsgMeta(t).label}</option>
                    `)}
                </select>
                <span style=${{ fontSize: typography.size.xs, color: colors.textTertiary }}>⌘+Enter to send</span>
            </div>
            <textarea
                style=${textareaStyle}
                placeholder="Write a message..."
                value=${content}
                onInput=${e => setContent(e.target.value)}
                onKeyDown=${handleKeyDown}
                disabled=${sending}
            />
            ${error ? html`<div style=${{ fontSize: typography.size.xs, color: colors.red }}>${error}</div>` : null}
            <button style=${btnStyle} onClick=${handleSubmit} disabled=${sending || !content.trim()}>
                ${sending ? 'Sending…' : 'Send'}
            </button>
        </div>
    `;
}

// ── Main view ─────────────────────────────────────────────────

export function ConversationView({ id, projectId }) {
    const [thread, setThread] = useState(null);
    const [error, setError] = useState(null);
    const [cursor, setCursor] = useState(null);
    const mountedRef = useRef(true);

    // Search state
    const [searchQuery, setSearchQuery] = useState('');
    const [searchResults, setSearchResults] = useState(null);
    const [searchLoading, setSearchLoading] = useState(false);
    const [highlightedIds, setHighlightedIds] = useState(new Set());
    const searchTimer = useRef(null);

    // Mobile detection
    const [isMobile, setIsMobile] = useState(() => window.innerWidth < 768);

    useEffect(() => {
        mountedRef.current = true;
        const onResize = () => setIsMobile(window.innerWidth < 768);
        window.addEventListener('resize', onResize);
        return () => {
            mountedRef.current = false;
            window.removeEventListener('resize', onResize);
        };
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

    useEffect(() => {
        window.scrollTo(0, 0);
    }, [id]);

    useEffect(() => {
        setThread(null);
        setError(null);
        setCursor(null);
        setSearchQuery('');
        setSearchResults(null);
        setHighlightedIds(new Set());
        loadFull();
    }, [loadFull]);

    // Polling
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

    // Scoped search — debounced 300ms, filter by conversation_id === id
    const handleSearch = useCallback((q) => {
        setSearchQuery(q);
        clearTimeout(searchTimer.current);
        if (!q.trim()) {
            setSearchResults(null);
            setHighlightedIds(new Set());
            setSearchLoading(false);
            return;
        }
        setSearchLoading(true);
        searchTimer.current = setTimeout(async () => {
            try {
                const params = { q };
                if (projectId) params.project_id = projectId;
                const data = await api.search(params);
                // Scope to this conversation
                const results = (data.results || []).filter(r => r.conversation_id === id);
                setSearchResults(results);
                // Highlight matched messages
                const ids = new Set(results.map(r => r.message_id).filter(Boolean));
                setHighlightedIds(ids);
                // Scroll to first match
                if (ids.size > 0) {
                    const firstId = [...ids][0];
                    const el = document.getElementById('msg-' + firstId);
                    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
            } catch {
                setSearchResults([]);
                setHighlightedIds(new Set());
            } finally {
                setSearchLoading(false);
            }
        }, 300);
    }, [id, projectId]);

    // Scroll helpers
    const scrollToMsg = useCallback((msgId) => {
        const el = document.getElementById('msg-' + msgId);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, []);

    const scrollToHeading = useCallback((headingText) => {
        // Find the pinned message container, then look for matching heading inside it
        const pinnedEl = document.getElementById('pinned-msg');
        if (!pinnedEl) return;
        // Search h2/h3 elements inside pinned message for matching text
        const headings = pinnedEl.querySelectorAll('h2, h3');
        for (const h of headings) {
            if (h.textContent.trim() === headingText) {
                h.scrollIntoView({ behavior: 'smooth', block: 'start' });
                return;
            }
        }
        // Fallback: scroll to pinned message
        pinnedEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, []);

    const handlePosted = useCallback(() => {
        loadFull();
    }, [loadFull]);

    // ── Derived data ──

    const messages = thread?.messages || [];
    const pinned = messages.filter(m => m._pinned_marker || m.pinned);
    const pinnedMsg = pinned[0] || null;
    const pinnedHeadings = useMemo(() => extractHeadings(pinnedMsg?.content), [pinnedMsg?.content]);

    // Sort regular messages newest-first
    const regular = useMemo(() => {
        return messages
            .filter(m => !m._pinned_marker && !m.pinned)
            .slice()
            .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    }, [messages]);

    // TOC message list (newest-first, pinned excluded)
    const tocMessages = regular;

    const convGoal = thread?.goal || pinnedMsg?.title || id;

    // ── Back link ──
    const backHref = projectId
        ? routes.projectTab(projectId, 'conversations')
        : '#/';
    const backLabel = projectId ? '← Conversations' : '← Back';

    // ── Styles ──

    const pageStyle = {
        display: 'flex',
        flexDirection: 'column',
        minHeight: 'calc(100vh - 52px)',
    };

    const headerStyle = {
        padding: '16px 20px 12px',
        borderBottom: `1px solid ${colors.border}`,
        background: colors.surface,
        position: 'sticky',
        top: 0,
        zIndex: 10,
    };

    const backLinkStyle = {
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        textDecoration: 'none',
        display: 'inline-flex',
        alignItems: 'center',
        gap: '4px',
        marginBottom: '8px',
        transition: `color ${animation.durationFast}`,
    };

    const titleStyle = {
        fontSize: typography.size.xl,
        fontWeight: typography.weight.semibold,
        color: colors.text,
        letterSpacing: '-0.02em',
        margin: '0 0 6px',
        wordBreak: 'break-word',
    };

    const metaRowStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '12px',
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        marginBottom: '10px',
    };

    const searchInputStyle = {
        width: '100%',
        padding: '8px 12px',
        background: colors.input,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.md,
        color: colors.text,
        fontSize: typography.size.sm,
        fontFamily: typography.fontBody,
        outline: 'none',
        boxSizing: 'border-box',
    };

    const bodyStyle = {
        display: 'flex',
        flex: 1,
        minHeight: 0,
    };

    const contentStyle = {
        flex: 1,
        minWidth: 0,
        display: 'flex',
        flexDirection: 'column',
    };

    const errorStyle = {
        padding: '20px',
        borderRadius: layout.borderRadius.md,
        background: colors.redBg,
        border: `1px solid ${colors.red}44`,
        color: colors.red,
        fontSize: typography.size.sm,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: '16px',
        margin: '20px',
    };

    const searchStatusStyle = {
        padding: '10px 16px',
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        borderBottom: `1px solid ${colors.border}22`,
    };

    // Loading skeleton
    if (!thread && !error) {
        return html`
            <div style=${pageStyle}>
                <div style=${headerStyle}>
                    <a href=${backHref} style=${backLinkStyle}>${backLabel}</a>
                    <div style=${{ height: '22px', width: '55%', background: colors.surfaceActive, borderRadius: '4px', marginBottom: '6px' }} class="foreman-skeleton" />
                    <div style=${{ height: '12px', width: '30%', background: colors.surfaceActive, borderRadius: '4px' }} class="foreman-skeleton" />
                </div>
                <div style=${bodyStyle}>
                    <div style=${{ flex: 1 }}>
                        ${[1,2,3].map(i => html`
                            <div key=${i} style=${{ padding: '14px 16px', borderBottom: `1px solid ${colors.border}22` }}>
                                <div style=${{ height: '12px', width: '40%', background: colors.surfaceActive, borderRadius: '4px', marginBottom: '8px' }} class="foreman-skeleton" />
                                <div style=${{ height: '12px', width: '70%', background: colors.surfaceActive, borderRadius: '4px' }} class="foreman-skeleton" />
                            </div>
                        `)}
                    </div>
                </div>
            </div>
        `;
    }

    return html`
        <div style=${pageStyle}>
            <!-- Sticky header -->
            <div style=${headerStyle}>
                <a href=${backHref} style=${backLinkStyle} class="foreman-back-link">${backLabel}</a>
                <h1 style=${titleStyle}>${convGoal}</h1>
                <div style=${metaRowStyle}>
                    <span>${messages.length} message${messages.length !== 1 ? 's' : ''}</span>
                    ${pinned.length > 0 ? html`<span>📌 ${pinned.length} pinned</span>` : null}
                </div>
                <!-- Scoped search -->
                <input
                    type="search"
                    placeholder="Search this conversation…"
                    value=${searchQuery}
                    onInput=${e => handleSearch(e.target.value)}
                    style=${searchInputStyle}
                    class="foreman-conv-scoped-search"
                />
            </div>

            ${error ? html`
                <div style=${errorStyle}>
                    <span>Failed to load: ${error}</span>
                    <button
                        style=${{ padding: '4px 12px', borderRadius: layout.borderRadius.sm, background: `${colors.red}22`, border: `1px solid ${colors.red}44`, color: colors.red, fontSize: typography.size.sm, cursor: 'pointer' }}
                        onClick=${loadFull}
                    >Retry</button>
                </div>
            ` : null}

            <!-- Mobile TOC -->
            ${isMobile ? html`
                <div style=${{ padding: '10px 16px', borderBottom: `1px solid ${colors.border}` }}>
                    <${MobileTocDropdown}
                        pinnedHeadings=${pinnedHeadings}
                        messages=${tocMessages}
                        onScrollToHeading=${scrollToHeading}
                        onScrollToMsg=${scrollToMsg}
                    />
                </div>
            ` : null}

            <!-- Body: sidebar + content -->
            <div style=${bodyStyle}>
                <!-- Desktop TOC sidebar -->
                ${!isMobile ? html`
                    <${TocSidebar}
                        pinnedHeadings=${pinnedHeadings}
                        messages=${tocMessages}
                        onScrollToHeading=${scrollToHeading}
                        onScrollToMsg=${scrollToMsg}
                    />
                ` : null}

                <!-- Content area -->
                <div style=${contentStyle}>
                    <!-- Search status -->
                    ${searchQuery.trim() ? html`
                        <div style=${searchStatusStyle}>
                            ${searchLoading ? 'Searching…' : searchResults
                                ? `${searchResults.length} result${searchResults.length !== 1 ? 's' : ''} for "${searchQuery}"`
                                : ''}
                        </div>
                    ` : null}

                    <!-- Pinned message always at top -->
                    ${pinnedMsg ? html`
                        <div id="pinned-msg">
                            <${ConversationMessage}
                                msg=${pinnedMsg}
                                isPinned=${true}
                                highlighted=${false}
                            />
                        </div>
                    ` : null}

                    <!-- Regular messages, newest-first -->
                    ${regular.length === 0 && !pinnedMsg ? html`
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
                        </div>
                    ` : regular.map(msg => html`
                        <${ConversationMessage}
                            key=${msg.id}
                            msg=${msg}
                            isPinned=${false}
                            highlighted=${highlightedIds.has(msg.id)}
                        />
                    `)}

                    <!-- Post input -->
                    <${PostInput} conversationId=${id} onPosted=${handlePosted} />
                </div>
            </div>
        </div>
    `;
}
