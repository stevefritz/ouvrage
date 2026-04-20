// Ouvrage Conversation View — TOC sidebar, scoped search, newest-first, markdown
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
const TOC_MSG_LIMIT = 30;

// Message types that render as compact single-line rows by default
const COMPACT_TYPES = new Set(['progress', 'status', 'handoff', 'test-result']);

// ── Inject CSS once (CSS-first responsive + animations) ───────

let _cssInjected = false;
function injectConvStyles() {
    if (_cssInjected || typeof document === 'undefined') return;
    _cssInjected = true;
    const style = document.createElement('style');
    style.textContent = `
/* TOC responsive: sidebar on desktop, accordion on mobile */
.ouvrage-toc-sidebar { display: flex; }
.ouvrage-toc-mobile  { display: none; }

@media (max-width: 767px) {
    .ouvrage-toc-sidebar { display: none !important; }
    .ouvrage-toc-mobile  { display: block !important; }
}

/* Permalink button — visible on message hover */
.ouvrage-conv-message { position: relative; }
.ouvrage-permalink {
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
.ouvrage-conv-message:hover .ouvrage-permalink {
    opacity: 1;
}
.ouvrage-permalink:hover {
    color: ${colors.accent};
    background: ${colors.surfaceActive};
}

/* Permalink flash animation for hash-targeted messages */
@keyframes ouvrage-permalink-flash {
    0%   { background: rgba(217, 119, 6, 0.25); }
    60%  { background: rgba(217, 119, 6, 0.12); }
    100% { background: transparent; }
}
.ouvrage-permalink-flash {
    animation: ouvrage-permalink-flash 1.2s ease-out forwards;
}

/* Accordion TOC styling */
.ouvrage-toc-accordion {
    border-bottom: 1px solid ${colors.border};
    background: ${colors.surface};
}
.ouvrage-toc-accordion summary {
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
.ouvrage-toc-accordion summary::-webkit-details-marker { display: none; }
.ouvrage-toc-accordion summary .toc-chevron {
    margin-left: auto;
    font-size: 10px;
    color: ${colors.textTertiary};
    transition: transform 120ms;
}
.ouvrage-toc-accordion[open] summary .toc-chevron {
    transform: rotate(180deg);
}
.ouvrage-toc-accordion-body {
    padding: 4px 0 8px;
    max-height: 280px;
    overflow-y: auto;
}
.ouvrage-toc-accordion-label {
    padding: 4px 16px 2px;
    font-size: ${typography.size.xs};
    font-weight: ${typography.weight.semibold};
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: ${colors.textTertiary};
}
.ouvrage-toc-accordion-item {
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
.ouvrage-toc-accordion-item:hover {
    color: ${colors.text};
    background: ${colors.surfaceHover};
}

/* Scroll spy active item */
.ouvrage-toc-item.toc-active {
    color: ${colors.accent} !important;
    font-weight: ${typography.weight.medium};
}

/* Code blocks: horizontal scroll within block, not page */
.ouvrage-conv-message pre,
.ouvrage-conv-message code {
    overflow-x: auto;
    max-width: 100%;
}

/* Heading anchor links in pinned messages */
.ouvrage-conv-message h2,
.ouvrage-conv-message h3 {
    position: relative;
}
.ouvrage-heading-anchor {
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
.ouvrage-conv-message h2:hover .ouvrage-heading-anchor,
.ouvrage-conv-message h3:hover .ouvrage-heading-anchor {
    opacity: 1;
}

/* Collapsed message preview fade */
.ouvrage-msg-preview-fade {
    position: absolute;
    bottom: 0; left: 0; right: 0;
    height: 32px;
    pointer-events: none;
}

/* Compact message row hover */
.ouvrage-msg-compact:hover {
    background: ${colors.surfaceHover} !important;
}

/* Search results panel mark highlighting */
.ouvrage-search-results mark {
    background: rgba(255, 220, 50, 0.35);
    color: inherit;
    border-radius: 2px;
    padding: 0 1px;
}

/* Search nav button */
.ouvrage-search-nav-btn {
    background: none;
    border: 1px solid ${colors.border};
    color: ${colors.textSecondary};
    border-radius: ${layout.borderRadius.sm};
    width: 24px;
    height: 24px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    font-size: 11px;
    flex-shrink: 0;
    transition: color 120ms, border-color 120ms;
}
.ouvrage-search-nav-btn:hover {
    color: ${colors.text};
    border-color: ${colors.borderHover};
}
.ouvrage-search-nav-btn:disabled {
    opacity: 0.35;
    cursor: default;
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

// ── HighlightedSnippet — inline <mark> on plain text ─────────

function HighlightedSnippet({ text, query }) {
    if (!query || !text) return html`<span>${text || ''}</span>`;
    const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const parts = text.split(new RegExp(`(${escaped})`, 'gi'));
    return html`<span>${parts.map((part, i) =>
        i % 2 === 1
            ? html`<mark key=${i}>${part}</mark>`
            : part
    )}</span>`;
}

// ── Message component ─────────────────────────────────────────

function ConversationMessage({ msg, isPinned, isCurrentMatch, isMatch, isSearchActive, searchQuery, isExpanded, onToggle }) {
    const isCompactType = COMPACT_TYPES.has(msg.type);
    const showAsCompact = isCompactType && !isPinned && !isExpanded;

    const renderedContent = useMemo(() => {
        const rawHtml = renderMarkdown(msg.content);
        return searchQuery ? highlightQuery(rawHtml, searchQuery) : rawHtml;
    }, [msg.content, searchQuery]);

    const handleCopyLink = useCallback((e) => {
        e.stopPropagation();
        const url = window.location.origin
            + window.location.pathname
            + window.location.search
            + '#msg-' + msg.id;
        navigator.clipboard.writeText(url).catch(() => {
            const ta = document.createElement('textarea');
            ta.value = url;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        });
    }, [msg.id]);

    // Dimming: search is active and this message doesn't match (pinned never dimmed)
    const isDimmed = isSearchActive && !isMatch && !isPinned;

    // Left border based on match state
    let leftBorder;
    if (isPinned) leftBorder = `3px solid ${colors.accent}`;
    else if (isCurrentMatch) leftBorder = `3px solid ${colors.accent}`;
    else if (isMatch) leftBorder = `2px solid rgba(217,119,6,0.4)`;

    // Background based on match state
    let bg;
    if (isPinned) bg = colors.accentBg || 'rgba(217,119,6,0.07)';
    else if (isCurrentMatch) bg = 'rgba(217,119,6,0.10)';
    else if (isMatch) bg = 'rgba(217,119,6,0.04)';

    const baseTransition = `opacity ${animation.durationFast}, background ${animation.durationFast}`;

    // ── Compact single-line row ──
    if (showAsCompact) {
        const firstLine = (msg.title || (msg.content || '').replace(/^#+\s*/m, '').split('\n')[0]).trim();
        return html`
            <div
                id=${'msg-' + msg.id}
                data-msg-id=${String(msg.id)}
                class="ouvrage-msg-compact"
                style=${{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                    padding: '4px 16px',
                    borderBottom: `1px solid ${colors.border}33`,
                    cursor: 'pointer',
                    opacity: isDimmed ? 0.35 : 1,
                    transition: baseTransition,
                    ...(leftBorder ? { borderLeft: leftBorder } : {}),
                    ...(bg ? { background: bg } : {}),
                }}
                onClick=${onToggle}
            >
                <${TypeBadge} type=${msg.type} mini=${true} />
                <span style=${{
                    fontSize: typography.size.sm,
                    color: colors.textSecondary,
                    fontWeight: typography.weight.medium,
                    flexShrink: 0,
                }}>${msg.author || 'unknown'}</span>
                <span style=${{ color: colors.textTertiary, flexShrink: 0 }}>—</span>
                <span style=${{
                    fontSize: typography.size.sm,
                    color: colors.textTertiary,
                    flex: 1,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                    minWidth: 0,
                }}>${firstLine}</span>
                <span style=${{
                    fontSize: typography.size.xs,
                    color: colors.textTertiary,
                    flexShrink: 0,
                }}>${relativeTime(msg.created_at)}</span>
            </div>
        `;
    }

    // ── Card view (collapsed or expanded, or pinned) ──
    const firstLineText = (msg.content || '').replace(/^#+\s*/m, '').split('\n').slice(0, 4).join(' ').slice(0, 300);

    return html`
        <div
            id=${'msg-' + msg.id}
            data-msg-id=${String(msg.id)}
            style=${{
                padding: '12px 16px',
                borderBottom: `1px solid ${colors.border}22`,
                position: 'relative',
                cursor: isPinned ? 'default' : 'pointer',
                opacity: isDimmed ? 0.35 : 1,
                transition: baseTransition,
                ...(leftBorder ? { borderLeft: leftBorder } : {}),
                ...(bg ? { background: bg } : {}),
            }}
            class="ouvrage-conv-message"
            onClick=${isPinned ? null : onToggle}
        >
            ${!isPinned ? html`
                <button
                    class="ouvrage-permalink"
                    onClick=${handleCopyLink}
                    title="Copy link to this message"
                    aria-label="Copy link"
                >⧉</button>
            ` : null}

            <!-- Header: author + badge + [pinned label] + timestamp -->
            <div style=${{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                flexWrap: 'wrap',
                marginBottom: '4px',
            }}>
                <span style=${{
                    fontSize: typography.size.sm,
                    fontWeight: typography.weight.medium,
                    color: colors.text,
                }}>${msg.author || 'unknown'}</span>
                <${TypeBadge} type=${msg.type || 'note'} />
                ${isPinned ? html`
                    <span style=${{
                        fontSize: typography.size.xs,
                        color: colors.accent,
                        fontWeight: typography.weight.medium,
                    }}>📌 Pinned</span>
                ` : null}
                <span style=${{
                    fontSize: typography.size.xs,
                    color: colors.textTertiary,
                    marginLeft: 'auto',
                    flexShrink: 0,
                }}>${relativeTime(msg.created_at)}</span>
            </div>

            <!-- Title line (14px/500) -->
            ${msg.title ? html`
                <div style=${{
                    fontSize: '14px',
                    fontWeight: typography.weight.medium,
                    color: colors.text,
                    marginBottom: '4px',
                    lineHeight: typography.lineHeight.tight,
                }}>${msg.title}</div>
            ` : null}

            ${(isPinned || isExpanded) ? html`
                <!-- Expanded / pinned: separator + full content -->
                ${!isPinned ? html`
                    <div style=${{
                        borderTop: `1px solid ${colors.border}44`,
                        margin: '6px 0',
                    }} />
                ` : null}
                <div
                    style=${{
                        fontSize: typography.size.sm,
                        color: colors.textSecondary,
                        lineHeight: typography.lineHeight.relaxed,
                        overflowWrap: 'break-word',
                        maxWidth: '100%',
                    }}
                    dangerouslySetInnerHTML=${{ __html: renderedContent }}
                />
            ` : html`
                <!-- Collapsed: ~2-line preview with gradient fade -->
                <div style=${{ position: 'relative', maxHeight: '120px', overflow: 'hidden' }}>
                    <div style=${{
                        fontSize: typography.size.sm,
                        color: colors.textTertiary,
                        lineHeight: '1.5',
                        overflowWrap: 'break-word',
                    }}>${firstLineText}</div>
                    <div
                        class="ouvrage-msg-preview-fade"
                        style=${{ background: `linear-gradient(to bottom, transparent, ${bg || colors.surface})` }}
                    />
                </div>
            `}
        </div>
    `;
}

// ── Search Results Panel ──────────────────────────────────────

function SearchResultsPanel({ results, matchList, currentMatchIndex, searchQuery, onSelectMatch }) {
    if (!results || results.length === 0 || matchList.length === 0) return null;

    const resultMap = useMemo(() => {
        const m = new Map();
        for (const r of results) m.set(r.id, r);
        return m;
    }, [results]);

    return html`
        <div
            class="ouvrage-search-results"
            style=${{
                maxHeight: '200px',
                overflowY: 'auto',
                borderBottom: `1px solid ${colors.border}`,
                background: colors.bg,
            }}
        >
            ${matchList.map((id, idx) => {
                const r = resultMap.get(id);
                if (!r) return null;
                const isActive = idx === currentMatchIndex;
                const snippet = r.snippet || (r.content || '').slice(0, 200);
                return html`
                    <div
                        key=${id}
                        onClick=${() => onSelectMatch(idx)}
                        style=${{
                            display: 'flex',
                            alignItems: 'flex-start',
                            gap: '8px',
                            padding: '6px 12px',
                            cursor: 'pointer',
                            borderLeft: isActive
                                ? `2px solid ${colors.accent}`
                                : '2px solid transparent',
                            background: isActive ? colors.accentBg : 'transparent',
                            transition: `background ${animation.durationFast}`,
                        }}
                    >
                        <${TypeBadge} type=${r.type || 'note'} mini=${true} />
                        <div style=${{
                            fontSize: typography.size.xs,
                            color: colors.textSecondary,
                            lineHeight: '1.4',
                            flex: 1,
                            minWidth: 0,
                            overflow: 'hidden',
                        }}>
                            <${HighlightedSnippet} text=${snippet} query=${searchQuery} />
                        </div>
                    </div>
                `;
            })}
        </div>
    `;
}

// ── TOC Sidebar (desktop) ─────────────────────────────────────

function TocSidebar({ pinnedHeadings, messages, activeId, onScrollToHeading, onScrollToMsg, matchIds, currentMatchId }) {
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
        maxHeight: 'calc(100vh - 140px)',
        position: 'sticky',
        top: '16px',
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
        // eslint-disable-next-line eqeqeq -- currentMatchId is string, msgId may be number
        const isCurrent = currentMatchId != null && msgId != null && currentMatchId == msgId;
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
            color: isCurrent ? colors.accent : isMatch ? colors.accent : isActive ? colors.accent : isDimmed ? `${colors.textSecondary}55` : colors.textSecondary,
            fontWeight: (isCurrent || isMatch || isActive) ? typography.weight.medium : typography.weight.normal,
            lineHeight: '1.4',
            overflow: 'hidden',
            transition: `color ${animation.durationFast}`,
            fontFamily: typography.fontBody,
            minWidth: 0,
            // Accent bar for active scroll-spy item and active search match
            borderLeft: (isCurrent || isActive) ? `2px solid ${colors.accent}` : '2px solid transparent',
            opacity: isDimmed ? 0.35 : 1,
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
        <nav style=${sidebarStyle} class="ouvrage-toc-sidebar" aria-label="Table of contents">
            ${pinnedHeadings.length > 0 ? html`
                <div style=${sectionLabelStyle}>Pinned</div>
                ${pinnedHeadings.map((h, i) => html`
                    <button
                        key=${i}
                        style=${tocItemStyle(true, false, null)}
                        class="ouvrage-toc-item"
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
                            class=${'ouvrage-toc-item' + (isActive ? ' toc-active' : '')}
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
        <details class="ouvrage-toc-accordion ouvrage-toc-mobile" ref=${detailsRef}>
            <summary>
                ☰ Contents (${totalSections} section${totalSections !== 1 ? 's' : ''})
                <span class="toc-chevron">▼</span>
            </summary>
            <div class="ouvrage-toc-accordion-body">
                ${pinnedHeadings.length > 0 ? html`
                    <div class="ouvrage-toc-accordion-label">Pinned</div>
                    ${pinnedHeadings.map((h, i) => html`
                        <button
                            key=${i}
                            class="ouvrage-toc-accordion-item"
                            onClick=${() => handleItemClick(() => onScrollToHeading(h))}
                        >${h.level === 3 ? '  · ' : ''}${h.text}</button>
                    `)}
                ` : null}
                ${messages.length > 0 ? html`
                    <div class="ouvrage-toc-accordion-label">Messages</div>
                    ${visibleMsgs.map(msg => html`
                        <button
                            key=${msg.id}
                            class="ouvrage-toc-accordion-item"
                            style=${{ display: 'flex', alignItems: 'center', gap: '5px' }}
                            onClick=${() => handleItemClick(() => onScrollToMsg(msg.id))}
                        >
                            <${TypeBadge} type=${msg.type || 'note'} mini=${true} />
                            <span style=${{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1, minWidth: 0 }}>${msg.title || (msg.content || '').split('\n')[0].replace(/^#+\s*/,'').slice(0,40) || msg.id}</span>
                        </button>
                    `)}
                    ${!showAll && hiddenCount > 0 ? html`
                        <button
                            class="ouvrage-toc-accordion-item"
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

    // Expanded message IDs (non-pinned messages the user has clicked to expand)
    const [expandedIds, setExpandedIds] = useState(new Set());

    // Search state
    const [searchQuery, setSearchQuery] = useState('');
    const [searchResults, setSearchResults] = useState(null);
    const [searchLoading, setSearchLoading] = useState(false);
    const [currentMatchIndex, setCurrentMatchIndex] = useState(0);
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
        setExpandedIds(new Set());
        setCurrentMatchIndex(0);
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
        const targetId = hash.slice(1);
        const timer = setTimeout(() => {
            const el = document.getElementById(targetId);
            if (el) {
                el.scrollIntoView({ behavior: 'smooth', block: 'start' });
                if (targetId.startsWith('msg-')) {
                    el.classList.add('ouvrage-permalink-flash');
                    setTimeout(() => el.classList.remove('ouvrage-permalink-flash'), 1400);
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
                const visible = entries.filter(e => e.isIntersecting);
                if (visible.length > 0) {
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

        const elements = document.querySelectorAll('[data-msg-id]');
        elements.forEach(el => observer.observe(el));

        return () => observer.disconnect();
    }, [thread, expandedIds]);

    // Add heading anchors to pinned message content after render
    useEffect(() => {
        if (!thread || !pinnedMsg) return;
        const pinnedEl = document.getElementById('pinned-msg');
        if (!pinnedEl) return;
        const headings = pinnedEl.querySelectorAll('h2, h3');
        headings.forEach(h => {
            const text = h.textContent.trim();
            const slug = slugify(text);
            if (!h.id) {
                h.id = 'heading-' + slug;
                h.style.position = 'relative';
                h.style.paddingLeft = '4px';
                if (!h.querySelector('.ouvrage-heading-anchor')) {
                    const a = document.createElement('a');
                    a.className = 'ouvrage-heading-anchor';
                    a.href = '#heading-' + slug;
                    a.textContent = '¶';
                    a.title = 'Link to this section';
                    a.setAttribute('aria-hidden', 'true');
                    h.appendChild(a);
                }
            }
        });
    }, [thread]);

    // Derived data (needs to be before handleSearch uses it)
    const messages = thread?.messages || [];
    const pinned = messages.filter(m => m._pinned_marker || m.pinned);
    const pinnedMsg = pinned[0] || null;
    const pinnedHeadings = useMemo(() => extractHeadings(pinnedMsg?.content), [pinnedMsg?.content]);

    const regular = useMemo(() => {
        return messages
            .filter(m => !m._pinned_marker && !m.pinned)
            .slice()
            .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    }, [messages]);

    // Search match list — ordered by display position (pinned first, then regular newest-first)
    const matchList = useMemo(() => {
        if (!searchResults || searchResults.length === 0) return [];
        const resultIds = new Set(searchResults.map(r => r.id));
        const ordered = [];
        if (pinnedMsg && resultIds.has(pinnedMsg.id)) ordered.push(pinnedMsg.id);
        for (const msg of regular) {
            if (resultIds.has(msg.id)) ordered.push(msg.id);
        }
        return ordered;
    }, [searchResults, pinnedMsg, regular]);

    const matchIds = useMemo(() => new Set(matchList), [matchList]);

    const currentMatchId = matchList.length > 0 ? matchList[currentMatchIndex] : null;

    // Reset match index when results change
    useEffect(() => {
        setCurrentMatchIndex(0);
    }, [searchResults]);

    // Scroll to current match when it changes
    useEffect(() => {
        if (!currentMatchId) return;
        const el = document.getElementById('msg-' + currentMatchId);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, [currentMatchId]);

    // Toggle expanded state for a message
    const toggleExpanded = useCallback((msgId) => {
        setExpandedIds(prev => {
            const next = new Set(prev);
            if (next.has(msgId)) next.delete(msgId);
            else next.add(msgId);
            return next;
        });
    }, []);

    // Navigate between matches
    const navigateMatch = useCallback((delta) => {
        setCurrentMatchIndex(prev => {
            const n = matchList.length;
            if (n === 0) return 0;
            return ((prev + delta) % n + n) % n;
        });
    }, [matchList]);

    // Scoped search — debounced 300ms
    const handleSearch = useCallback((q) => {
        setSearchQuery(q);
        clearTimeout(searchTimer.current);
        if (!q.trim()) {
            setSearchResults(null);
            setSearchLoading(false);
            return;
        }
        setSearchLoading(true);
        searchTimer.current = setTimeout(async () => {
            try {
                const data = await api.searchConversation(id, q);
                const results = data.results || [];
                setSearchResults(results);
            } catch {
                setSearchResults([]);
            } finally {
                setSearchLoading(false);
            }
        }, 300);
    }, [id]);

    const handleSearchKeyDown = useCallback((e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            if (e.shiftKey) navigateMatch(-1);
            else navigateMatch(1);
        } else if (e.key === 'Escape') {
            handleSearch('');
        }
    }, [navigateMatch, handleSearch]);

    const handleSelectMatch = useCallback((idx) => {
        setCurrentMatchIndex(idx);
    }, []);

    // Scroll helpers
    const scrollToMsg = useCallback((msgId) => {
        const el = document.getElementById('msg-' + msgId);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, []);

    const scrollToHeading = useCallback((heading) => {
        const el = document.getElementById('heading-' + heading.slug);
        if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'start' });
            return;
        }
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

    const isSearchActive = searchQuery.trim().length > 0;
    const hasMatches = matchList.length > 0;

    // Loading skeleton
    if (!thread && !error) {
        return html`
            <div style=${pageStyle}>
                <div style=${headerStyle}>
                    <a href=${backHref} style=${backLinkStyle}>${backLabel}</a>
                    <div style=${{ height: '22px', width: '55%', background: colors.surfaceActive, borderRadius: '4px', marginBottom: '6px' }} class="ouvrage-skeleton" />
                    <div style=${{ height: '12px', width: '30%', background: colors.surfaceActive, borderRadius: '4px' }} class="ouvrage-skeleton" />
                </div>
                <div style=${bodyStyle}>
                    <div style=${{ flex: 1 }}>
                        ${[1,2,3].map(i => html`
                            <div key=${i} style=${{ padding: '14px 16px', borderBottom: `1px solid ${colors.border}22` }}>
                                <div style=${{ height: '12px', width: '40%', background: colors.surfaceActive, borderRadius: '4px', marginBottom: '8px' }} class="ouvrage-skeleton" />
                                <div style=${{ height: '12px', width: '70%', background: colors.surfaceActive, borderRadius: '4px' }} class="ouvrage-skeleton" />
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
                <a href=${backHref} style=${backLinkStyle} class="ouvrage-back-link">${backLabel}</a>
                <h1 style=${titleStyle}>${convGoal}</h1>
                <div style=${metaRowStyle}>
                    <span>${messages.length} message${messages.length !== 1 ? 's' : ''}</span>
                    ${pinned.length > 0 ? html`<span>📌 ${pinned.length} pinned</span>` : null}
                </div>

                <!-- Search bar with navigation controls -->
                <div style=${{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <input
                        type="search"
                        placeholder="Search this conversation…"
                        value=${searchQuery}
                        onInput=${e => handleSearch(e.target.value)}
                        onKeyDown=${handleSearchKeyDown}
                        style=${{
                            flex: 1,
                            padding: '7px 10px',
                            background: colors.input,
                            border: `1px solid ${colors.border}`,
                            borderRadius: layout.borderRadius.md,
                            color: colors.text,
                            fontSize: typography.size.sm,
                            fontFamily: typography.fontBody,
                            outline: 'none',
                            minWidth: 0,
                        }}
                        class="ouvrage-conv-scoped-search"
                    />
                    ${isSearchActive ? html`
                        <!-- Navigation controls -->
                        <button
                            class="ouvrage-search-nav-btn"
                            onClick=${() => navigateMatch(-1)}
                            disabled=${!hasMatches}
                            title="Previous match (Shift+Enter)"
                            aria-label="Previous match"
                        >↑</button>
                        <span style=${{
                            fontSize: typography.size.xs,
                            fontFamily: typography.fontMono,
                            color: searchLoading ? colors.textTertiary : hasMatches ? colors.textSecondary : colors.textTertiary,
                            minWidth: '40px',
                            textAlign: 'center',
                            flexShrink: 0,
                        }}>
                            ${searchLoading ? '…' : hasMatches ? `${currentMatchIndex + 1} of ${matchList.length}` : '0 of 0'}
                        </span>
                        <button
                            class="ouvrage-search-nav-btn"
                            onClick=${() => navigateMatch(1)}
                            disabled=${!hasMatches}
                            title="Next match (Enter)"
                            aria-label="Next match"
                        >↓</button>
                        <button
                            class="ouvrage-search-nav-btn"
                            onClick=${() => handleSearch('')}
                            title="Clear search"
                            aria-label="Clear search"
                            style=${{ borderColor: 'transparent' }}
                        >✕</button>
                    ` : null}
                </div>
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

            <!-- Search results panel -->
            ${isSearchActive ? html`
                <${SearchResultsPanel}
                    results=${searchResults}
                    matchList=${matchList}
                    currentMatchIndex=${currentMatchIndex}
                    searchQuery=${searchQuery}
                    onSelectMatch=${handleSelectMatch}
                />
            ` : null}

            <!-- Body: sidebar + content -->
            <div style=${bodyStyle}>
                <!-- Desktop TOC sidebar (hidden on mobile via CSS) -->
                <${TocSidebar}
                    pinnedHeadings=${pinnedHeadings}
                    messages=${tocMessages}
                    activeId=${activeId}
                    onScrollToHeading=${scrollToHeading}
                    onScrollToMsg=${scrollToMsg}
                    matchIds=${matchIds}
                    currentMatchId=${currentMatchId}
                />

                <!-- Content area -->
                <div style=${contentStyle}>
                    <!-- Pinned message always at top, always expanded -->
                    ${pinnedMsg ? html`
                        <div id="pinned-msg">
                            <${ConversationMessage}
                                msg=${pinnedMsg}
                                isPinned=${true}
                                isCurrentMatch=${currentMatchId != null && currentMatchId == pinnedMsg.id}
                                isMatch=${matchIds.has(pinnedMsg.id)}
                                isSearchActive=${isSearchActive}
                                searchQuery=${matchIds.has(pinnedMsg.id) ? searchQuery : null}
                                isExpanded=${true}
                                onToggle=${null}
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
                            isCurrentMatch=${currentMatchId != null && currentMatchId == msg.id}
                            isMatch=${matchIds.has(msg.id)}
                            isSearchActive=${isSearchActive}
                            searchQuery=${matchIds.has(msg.id) ? searchQuery : null}
                            isExpanded=${expandedIds.has(msg.id)}
                            onToggle=${() => toggleExpanded(msg.id)}
                        />
                    `)}

                    <!-- Post input -->
                    <${PostInput} conversationId=${id} onPosted=${handlePosted} />
                </div>
            </div>
        </div>
    `;
}
