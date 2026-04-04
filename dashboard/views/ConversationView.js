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

// ── Inject CSS once (CSS-first responsive + animations) ───────

let _cssInjected = false;
function injectConvStyles() {
    if (_cssInjected || typeof document === 'undefined') return;
    _cssInjected = true;
    const style = document.createElement('style');
    style.textContent = `
/* TOC responsive: sidebar on desktop, accordion on mobile */
.foreman-toc-sidebar { display: flex; }
.foreman-toc-mobile  { display: none; }

@media (max-width: 767px) {
    .foreman-toc-sidebar { display: none !important; }
    .foreman-toc-mobile  { display: block !important; }
}

/* Permalink button — visible on message hover */
.foreman-conv-message { position: relative; }
.foreman-permalink {
    position: absolute;
    top: 10px;
    right: 10px;
    opacity: 0;
    background: none;
    border: none;
    cursor: pointer;
    padding: 2px 6px;
    border-radius: 4px;
    color: ${colors.textTertiary};
    font-size: 13px;
    transition: opacity 120ms, color 120ms, background 120ms;
    line-height: 1;
}
.foreman-conv-message:hover .foreman-permalink {
    opacity: 1;
}
.foreman-permalink:hover {
    color: ${colors.accent};
    background: ${colors.surfaceActive};
}

/* Permalink flash animation for hash-targeted messages */
@keyframes foreman-permalink-flash {
    0%   { background: rgba(217, 119, 6, 0.25); }
    60%  { background: rgba(217, 119, 6, 0.12); }
    100% { background: transparent; }
}
.foreman-permalink-flash {
    animation: foreman-permalink-flash 1.2s ease-out forwards;
}

/* Accordion TOC styling */
.foreman-toc-accordion {
    border-bottom: 1px solid ${colors.border};
    background: ${colors.surface};
}
.foreman-toc-accordion summary {
    list-style: none;
    cursor: pointer;
    padding: 9px 16px;
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: ${typography.size.sm};
    color: ${colors.textSecondary};
    font-family: ${typography.fontBody};
    user-select: none;
}
.foreman-toc-accordion summary::-webkit-details-marker { display: none; }
.foreman-toc-accordion summary .toc-chevron {
    margin-left: auto;
    font-size: 10px;
    color: ${colors.textTertiary};
    transition: transform 120ms;
}
.foreman-toc-accordion[open] summary .toc-chevron {
    transform: rotate(180deg);
}
.foreman-toc-accordion-body {
    padding: 4px 0 8px;
    max-height: 280px;
    overflow-y: auto;
}
.foreman-toc-accordion-label {
    padding: 4px 16px 2px;
    font-size: ${typography.size.xs};
    font-weight: ${typography.weight.semibold};
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: ${colors.textTertiary};
}
.foreman-toc-accordion-item {
    display: block;
    width: 100%;
    text-align: left;
    background: none;
    border: none;
    cursor: pointer;
    padding: 5px 16px;
    font-size: ${typography.size.xs};
    color: ${colors.textSecondary};
    font-family: ${typography.fontBody};
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    line-height: 1.4;
}
.foreman-toc-accordion-item:hover {
    color: ${colors.text};
    background: ${colors.surfaceHover};
}

/* Scroll spy active item */
.foreman-toc-item.toc-active {
    color: ${colors.accent} !important;
    font-weight: ${typography.weight.medium};
}

/* Heading anchor links in pinned messages */
.foreman-conv-message h2,
.foreman-conv-message h3 {
    position: relative;
}
.foreman-heading-anchor {
    position: absolute;
    left: -18px;
    top: 50%;
    transform: translateY(-50%);
    opacity: 0;
    color: ${colors.textTertiary};
    text-decoration: none;
    font-size: 13px;
    font-weight: normal;
    transition: opacity 120ms;
    padding: 0 4px;
}
.foreman-conv-message h2:hover .foreman-heading-anchor,
.foreman-conv-message h3:hover .foreman-heading-anchor {
    opacity: 1;
}
`;
    document.head.appendChild(style);
}

// ── Slug helper ───────────────────────────────────────────────

function slugify(text) {
    return text
        .toLowerCase()
        .replace(/[^\w\s-]/g, '')
        .trim()
        .replace(/[\s_]+/g, '-')
        .replace(/-+/g, '-');
}

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

// Wrap text matches in <mark> tags, skipping content inside HTML tags
function highlightQuery(html, query) {
    if (!query || !html) return html;
    const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return html.replace(
        new RegExp(`(<[^>]*>)|(${escaped})`, 'gi'),
        (match, tag) => tag ? match : `<mark style="background:rgba(255,220,50,0.35);color:inherit;border-radius:2px;padding:0 1px">${match}</mark>`,
    );
}

// Extract ## and ### headings from markdown text (returns {level, text, slug})
function extractHeadings(markdown) {
    if (!markdown) return [];
    const headings = [];
    const lines = markdown.split('\n');
    for (const line of lines) {
        const m = line.match(/^(#{2,3})\s+(.+)$/);
        if (m) {
            const text = m[2].trim();
            headings.push({ level: m[1].length, text, slug: slugify(text) });
        }
    }
    return headings;
}

// ── TypeBadge ────────────────────────────────────────────────

function TypeBadge({ type, mini }) {
    const meta = getMsgMeta(type);
    return html`
        <span style=${{
            display: 'inline-block',
            padding: mini ? '1px 5px' : '2px 7px',
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

function ConversationMessage({ msg, isPinned, highlighted, searchQuery }) {
    const renderedContent = useMemo(() => {
        const html = renderMarkdown(msg.content);
        return searchQuery ? highlightQuery(html, searchQuery) : html;
    }, [msg.content, searchQuery]);

    const handleCopyLink = useCallback(() => {
        const url = window.location.origin
            + window.location.pathname
            + window.location.search
            + '#msg-' + msg.id;
        navigator.clipboard.writeText(url).catch(() => {
            // fallback: prompt copy
            const ta = document.createElement('textarea');
            ta.value = url;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        });
    }, [msg.id]);

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
            data-msg-id=${String(msg.id)}
            style=${containerStyle}
            class="foreman-conv-message"
        >
            <button
                class="foreman-permalink"
                onClick=${handleCopyLink}
                title="Copy link to this message"
                aria-label="Copy link"
            >⧉</button>
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

// ── TOC Sidebar (desktop) ─────────────────────────────────────

function TocSidebar({ pinnedHeadings, messages, activeId, onScrollToHeading, onScrollToMsg, matchIds }) {
    const [showAll, setShowAll] = useState(false);
    const visibleMsgs = showAll ? messages : messages.slice(0, TOC_MSG_LIMIT);
    const hiddenCount = messages.length - TOC_MSG_LIMIT;
    const hasFilter = matchIds && matchIds.size > 0;

    const sidebarStyle = {
        width: layout.sidebarWidth,
        flexShrink: 0,
        borderRight: `1px solid ${colors.border}`,
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

    const tocItemStyle = (isHeading, isActive, msgId) => {
        const isMatch = msgId && hasFilter && matchIds.has(msgId);
        const isDimmed = hasFilter && msgId && !matchIds.has(msgId);
        return {
            display: 'flex',
            alignItems: 'center',
            gap: '5px',
            width: '100%',
            textAlign: 'left',
            background: 'none',
            border: 'none',
            cursor: 'pointer',
            padding: isHeading ? '3px 14px 3px 20px' : '4px 14px',
            fontSize: typography.size.xs,
            color: isMatch ? colors.accent : isActive ? colors.accent : isDimmed ? `${colors.textSecondary}55` : colors.textSecondary,
            fontWeight: (isMatch || isActive) ? typography.weight.medium : typography.weight.normal,
            lineHeight: '1.4',
            overflow: 'hidden',
            transition: `color ${animation.durationFast}`,
            fontFamily: typography.fontBody,
            minWidth: 0,
        };
    };

    const labelSpanStyle = {
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        flex: 1,
        minWidth: 0,
    };

    return html`
        <nav style=${sidebarStyle} class="foreman-toc-sidebar" aria-label="Table of contents">
            ${pinnedHeadings.length > 0 ? html`
                <div style=${sectionLabelStyle}>Pinned</div>
                ${pinnedHeadings.map((h, i) => html`
                    <button
                        key=${i}
                        style=${tocItemStyle(true, false, null)}
                        class="foreman-toc-item"
                        onClick=${() => onScrollToHeading(h)}
                        title=${h.text}
                    >${h.level === 3 ? '  · ' : ''}${h.text}</button>
                `)}
            ` : null}

            ${messages.length > 0 ? html`
                <div style=${sectionLabelStyle}>Messages</div>
                ${visibleMsgs.map(msg => {
                    // eslint-disable-next-line eqeqeq -- activeId is string from DOM, msg.id may be number
                    const isActive = activeId != null && activeId == msg.id;
                    const label = msg.title || (msg.content || '').split('\n')[0].replace(/^#+\s*/,'').slice(0,40) || msg.id;
                    return html`
                        <button
                            key=${msg.id}
                            style=${tocItemStyle(false, isActive, msg.id)}
                            class=${'foreman-toc-item' + (isActive ? ' toc-active' : '')}
                            onClick=${() => onScrollToMsg(msg.id)}
                            title=${label}
                        >
                            <${TypeBadge} type=${msg.type || 'note'} mini=${true} />
                            <span style=${labelSpanStyle}>${label}</span>
                        </button>
                    `;
                })}
                ${!showAll && hiddenCount > 0 ? html`
                    <button
                        style=${{ ...tocItemStyle(false, false, null), color: colors.accent, fontWeight: typography.weight.medium }}
                        onClick=${() => setShowAll(true)}
                    >+${hiddenCount} more</button>
                ` : null}
            ` : null}
        </nav>
    `;
}

// ── Mobile TOC Accordion ──────────────────────────────────────

function MobileTocAccordion({ pinnedHeadings, messages, onScrollToHeading, onScrollToMsg }) {
    const detailsRef = useRef(null);
    const [showAll, setShowAll] = useState(false);
    const visibleMsgs = showAll ? messages : messages.slice(0, TOC_MSG_LIMIT);
    const hiddenCount = messages.length - TOC_MSG_LIMIT;

    const totalSections = pinnedHeadings.length + messages.length;
    if (totalSections === 0) return null;

    const handleItemClick = useCallback((fn) => {
        fn();
        if (detailsRef.current) detailsRef.current.open = false;
    }, []);

    return html`
        <details class="foreman-toc-accordion foreman-toc-mobile" ref=${detailsRef}>
            <summary>
                ☰ Contents (${totalSections} section${totalSections !== 1 ? 's' : ''})
                <span class="toc-chevron">▼</span>
            </summary>
            <div class="foreman-toc-accordion-body">
                ${pinnedHeadings.length > 0 ? html`
                    <div class="foreman-toc-accordion-label">Pinned</div>
                    ${pinnedHeadings.map((h, i) => html`
                        <button
                            key=${i}
                            class="foreman-toc-accordion-item"
                            onClick=${() => handleItemClick(() => onScrollToHeading(h))}
                        >${h.level === 3 ? '  · ' : ''}${h.text}</button>
                    `)}
                ` : null}
                ${messages.length > 0 ? html`
                    <div class="foreman-toc-accordion-label">Messages</div>
                    ${visibleMsgs.map(msg => html`
                        <button
                            key=${msg.id}
                            class="foreman-toc-accordion-item"
                            onClick=${() => handleItemClick(() => onScrollToMsg(msg.id))}
                        >${msg.title || (msg.content || '').split('\n')[0].replace(/^#+\s*/,'').slice(0,40) || msg.id}</button>
                    `)}
                    ${!showAll && hiddenCount > 0 ? html`
                        <button
                            class="foreman-toc-accordion-item"
                            style=${{ color: colors.accent }}
                            onClick=${() => setShowAll(true)}
                        >+${hiddenCount} more</button>
                    ` : null}
                ` : null}
            </div>
        </details>
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

    // Scroll spy active message ID
    const [activeId, setActiveId] = useState(null);

    // Search state
    const [searchQuery, setSearchQuery] = useState('');
    const [searchResults, setSearchResults] = useState(null);
    const [searchLoading, setSearchLoading] = useState(false);
    const [highlightedIds, setHighlightedIds] = useState(new Set());
    const searchTimer = useRef(null);

    // Inject CSS on first render
    useEffect(() => { injectConvStyles(); }, []);

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

    // Hash scroll on load — when thread loads, scroll to #msg-{id} and flash
    useEffect(() => {
        if (!thread) return;
        const hash = window.location.hash;
        if (!hash) return;
        const targetId = hash.slice(1); // e.g. "msg-42" or "heading-foo"
        // Delay to let DOM render
        const timer = setTimeout(() => {
            const el = document.getElementById(targetId);
            if (el) {
                el.scrollIntoView({ behavior: 'smooth', block: 'start' });
                // Flash highlight if it's a message
                if (targetId.startsWith('msg-')) {
                    el.classList.add('foreman-permalink-flash');
                    setTimeout(() => el.classList.remove('foreman-permalink-flash'), 1400);
                }
            }
        }, 150);
        return () => clearTimeout(timer);
    }, [thread]);

    // Scroll spy — IntersectionObserver on message elements
    useEffect(() => {
        if (!thread) return;
        const observer = new IntersectionObserver(
            (entries) => {
                // Find the topmost intersecting entry
                const visible = entries.filter(e => e.isIntersecting);
                if (visible.length > 0) {
                    // Take the one with the highest Y position (closest to top of viewport)
                    const topmost = visible.reduce((a, b) =>
                        a.boundingClientRect.top < b.boundingClientRect.top ? a : b
                    );
                    const msgId = topmost.target.getAttribute('data-msg-id');
                    if (msgId) setActiveId(msgId);
                }
            },
            {
                rootMargin: '-10% 0px -75% 0px',
                threshold: 0,
            }
        );

        // Observe all message elements
        const elements = document.querySelectorAll('.foreman-conv-message[data-msg-id]');
        elements.forEach(el => observer.observe(el));

        return () => observer.disconnect();
    }, [thread]);

    // Add heading anchors to pinned message content after render
    useEffect(() => {
        if (!thread || !pinnedMsg) return;
        const pinnedEl = document.getElementById('pinned-msg');
        if (!pinnedEl) return;
        const container = pinnedEl;
        const headings = container.querySelectorAll('h2, h3');
        headings.forEach(h => {
            const text = h.textContent.trim();
            const slug = slugify(text);
            if (!h.id) {
                h.id = 'heading-' + slug;
                h.style.position = 'relative';
                h.style.paddingLeft = '4px';
                // Add anchor link if not already present
                if (!h.querySelector('.foreman-heading-anchor')) {
                    const a = document.createElement('a');
                    a.className = 'foreman-heading-anchor';
                    a.href = '#heading-' + slug;
                    a.textContent = '¶';
                    a.title = 'Link to this section';
                    a.setAttribute('aria-hidden', 'true');
                    h.appendChild(a);
                }
            }
        });
    }, [thread]);

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
                const data = await api.searchConversation(id, q);
                const results = data.results || [];
                setSearchResults(results);
                // Highlight matched messages — dedicated endpoint returns r.id directly
                const ids = new Set(results.map(r => r.id).filter(Boolean));
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
    }, [id]);

    // Scroll helpers
    const scrollToMsg = useCallback((msgId) => {
        const el = document.getElementById('msg-' + msgId);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, []);

    const scrollToHeading = useCallback((heading) => {
        // heading is {text, slug, level}
        const el = document.getElementById('heading-' + heading.slug);
        if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'start' });
            return;
        }
        // Fallback: find by text content
        const pinnedEl = document.getElementById('pinned-msg');
        if (!pinnedEl) return;
        const headings = pinnedEl.querySelectorAll('h2, h3');
        for (const h of headings) {
            if (h.textContent.trim() === heading.text || h.textContent.trim().startsWith(heading.text)) {
                h.scrollIntoView({ behavior: 'smooth', block: 'start' });
                return;
            }
        }
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

            <!-- Mobile TOC accordion (hidden on desktop via CSS) -->
            <${MobileTocAccordion}
                pinnedHeadings=${pinnedHeadings}
                messages=${tocMessages}
                onScrollToHeading=${scrollToHeading}
                onScrollToMsg=${scrollToMsg}
            />

            <!-- Body: sidebar + content -->
            <div style=${bodyStyle}>
                <!-- Desktop TOC sidebar (hidden on mobile via CSS) -->
                <${TocSidebar}
                    pinnedHeadings=${pinnedHeadings}
                    messages=${tocMessages}
                    activeId=${activeId}
                    onScrollToHeading=${scrollToHeading}
                    onScrollToMsg=${scrollToMsg}
                    matchIds=${highlightedIds}
                />

                <!-- Content area -->
                <div style=${contentStyle}>
                    <!-- Search status -->
                    ${searchQuery.trim() ? html`
                        <div style=${searchStatusStyle}>
                            ${searchLoading ? 'Searching…' : searchResults
                                ? `${searchResults.length} match${searchResults.length !== 1 ? 'es' : ''} for "${searchQuery}"`
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
                                searchQuery=${highlightedIds.has(pinnedMsg.id) ? searchQuery : null}
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
                            searchQuery=${highlightedIds.has(msg.id) ? searchQuery : null}
                        />
                    `)}

                    <!-- Post input -->
                    <${PostInput} conversationId=${id} onPosted=${handlePosted} />
                </div>
            </div>
        </div>
    `;
}
