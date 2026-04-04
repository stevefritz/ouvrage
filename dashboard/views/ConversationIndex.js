// Conversation Index View
// Rendered when the Conversations tab is active on a project.
// Route: #/project/:id/conversations
// Shows pinned conversations (amber left border) and all others,
// with a debounced semantic search bar.

import { h } from 'https://esm.sh/preact@10.25.4';
import { useState, useEffect, useCallback, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, layout, animation } from '../tokens.js';
import { routes } from '../router.js';
import { api } from '../api.js';
import { relativeTime } from '../components/utils.js';

const html = htm.bind(h);

// ---------------------------------------------------------------------------
// Message type metadata (subset — matches ConversationView MSG_META)
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// MiniTypeBadge — inline type badge for search result cards
// ---------------------------------------------------------------------------

function MiniTypeBadge({ type }) {
    const meta = getMsgMeta(type);
    return html`
        <span style=${{
            display: 'inline-block',
            padding: '1px 5px',
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

// ---------------------------------------------------------------------------
// SnippetLine — renders snippet text with <mark> around matched query terms
// ---------------------------------------------------------------------------

function SnippetLine({ text, query }) {
    if (!text) return null;
    if (!query) return html`<span>${text}</span>`;
    const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const parts = text.split(new RegExp(`(${escaped})`, 'gi'));
    return html`<span>${parts.map((part, i) =>
        i % 2 === 1
            ? html`<mark key=${i} style=${{ background: 'rgba(255,220,50,0.35)', color: 'inherit', borderRadius: '2px', padding: '0 1px' }}>${part}</mark>`
            : part
    )}</span>`;
}

// ---------------------------------------------------------------------------
// ConversationCard — single conversation row/card
// ---------------------------------------------------------------------------

function ConversationCard({ conv, projectId }) {
    const href = projectId
        ? routes.projectConversation(projectId, conv.id)
        : routes.conversation(conv.id);

    const cardStyle = {
        display: 'block',
        padding: '12px 14px',
        borderRadius: layout.borderRadius.md,
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        textDecoration: 'none',
        color: colors.text,
        transition: `background ${animation.durationFast}`,
        ...(conv.has_pinned ? { borderLeft: `3px solid ${colors.accent}` } : {}),
    };

    const titleStyle = {
        fontSize: typography.size.sm,
        fontWeight: typography.weight.medium,
        color: colors.text,
        marginBottom: conv.has_pinned && conv.pinned_title ? '4px' : '0',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
    };

    const pinnedPreviewStyle = {
        fontSize: typography.size.xs,
        color: colors.textSecondary,
        fontStyle: 'italic',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
    };

    const metaStyle = {
        display: 'flex',
        alignItems: 'baseline',
        justifyContent: 'space-between',
        gap: '8px',
        marginTop: '6px',
    };

    const metaTextStyle = {
        fontFamily: typography.fontMono,
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        flexShrink: 0,
    };

    const title = conv.goal || conv.id;
    const msgCount = conv.message_count ?? 0;
    const lastAt = conv.last_message_at || conv.updated_at;

    return html`
        <a href=${href} style=${cardStyle} class="foreman-conv-card">
            <div style=${{ display: 'flex', alignItems: 'flex-start', gap: '8px' }}>
                <div style=${{ flex: 1, minWidth: 0 }}>
                    <div style=${titleStyle}>${title}</div>
                    ${conv.has_pinned && conv.pinned_title ? html`
                        <div style=${pinnedPreviewStyle}>📌 ${conv.pinned_title}</div>
                    ` : null}
                </div>
            </div>
            <div style=${metaStyle}>
                <span style=${{ ...metaTextStyle, color: colors.textTertiary }}>
                    ${msgCount} ${msgCount === 1 ? 'message' : 'messages'}
                </span>
                <span style=${metaTextStyle}>${relativeTime(lastAt)}</span>
            </div>
        </a>
    `;
}

// ---------------------------------------------------------------------------
// SearchResultCard — ConversationCard + snippet line + type badge
// ---------------------------------------------------------------------------

function SearchResultCard({ result, conv, projectId, searchQuery }) {
    const href = projectId
        ? routes.projectConversation(projectId, conv.id)
        : routes.conversation(conv.id);

    const cardStyle = {
        display: 'block',
        padding: '12px 14px',
        borderRadius: layout.borderRadius.md,
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        textDecoration: 'none',
        color: colors.text,
        transition: `background ${animation.durationFast}`,
        ...(conv.has_pinned ? { borderLeft: `3px solid ${colors.accent}` } : {}),
    };

    const titleStyle = {
        fontSize: typography.size.sm,
        fontWeight: typography.weight.medium,
        color: colors.text,
        marginBottom: '4px',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
    };

    const snippetStyle = {
        fontSize: typography.size.xs,
        color: colors.textSecondary,
        marginTop: '2px',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
    };

    const metaStyle = {
        display: 'flex',
        alignItems: 'baseline',
        justifyContent: 'space-between',
        gap: '8px',
        marginTop: '6px',
    };

    const metaTextStyle = {
        fontFamily: typography.fontMono,
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        flexShrink: 0,
    };

    const title = conv.goal || conv.id;
    const msgCount = conv.message_count ?? 0;
    const lastAt = conv.last_message_at || conv.updated_at;

    return html`
        <a href=${href} style=${cardStyle} class="foreman-conv-card foreman-conv-search-result">
            <div style=${{ display: 'flex', alignItems: 'flex-start', gap: '8px' }}>
                <div style=${{ flex: 1, minWidth: 0 }}>
                    <div style=${titleStyle}>${title}</div>
                    ${result.snippet ? html`
                        <div style=${snippetStyle}>
                            ${result.message_type ? html`<${MiniTypeBadge} type=${result.message_type} /> ` : null}
                            <${SnippetLine} text=${result.snippet} query=${searchQuery} />
                        </div>
                    ` : null}
                </div>
            </div>
            <div style=${metaStyle}>
                <span style=${{ ...metaTextStyle, color: colors.textTertiary }}>
                    ${msgCount} ${msgCount === 1 ? 'message' : 'messages'}
                </span>
                <span style=${metaTextStyle}>${relativeTime(lastAt)}</span>
            </div>
        </a>
    `;
}

// ---------------------------------------------------------------------------
// Section header
// ---------------------------------------------------------------------------

function SectionHeader({ label, count, accent }) {
    return html`
        <div style=${{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            marginBottom: '8px',
            marginTop: '4px',
        }}>
            <span style=${{
                fontSize: typography.size.xs,
                fontWeight: typography.weight.semibold,
                letterSpacing: '0.08em',
                textTransform: 'uppercase',
                color: accent ? colors.accent : colors.textTertiary,
            }}>${label}</span>
            ${count != null ? html`
                <span style=${{
                    fontSize: typography.size.xs,
                    color: colors.textTertiary,
                    fontFamily: typography.fontMono,
                }}>(${count})</span>
            ` : null}
            <div style=${{ flex: 1, height: '1px', background: colors.border }} />
        </div>
    `;
}

// ---------------------------------------------------------------------------
// ConversationIndex — main view
// ---------------------------------------------------------------------------

export function ConversationIndex({ projectId }) {
    const [conversations, setConversations] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    // Search state
    const [searchQuery, setSearchQuery] = useState('');
    const [searchResults, setSearchResults] = useState(null);
    const [searchLoading, setSearchLoading] = useState(false);
    const searchTimer = useRef(null);

    // Load conversations
    useEffect(() => {
        if (!projectId) return;
        setLoading(true);
        api.getConversations({ project: projectId })
            .then(data => {
                setConversations(Array.isArray(data) ? data : []);
                setLoading(false);
            })
            .catch(err => {
                setError(err.message || 'Failed to load conversations');
                setLoading(false);
            });
    }, [projectId]);

    // Debounced search — dual approach:
    // 1) Server-side LIKE filter on conversation titles (fast, partial match)
    // 2) Vector search for message-content hits within conversations
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
                // Run both in parallel: title LIKE search + vector content search
                const [titleMatches, vecData] = await Promise.all([
                    api.getConversations({ project: projectId, search: q }).catch(() => []),
                    api.search({ q, project_id: projectId, limit: 20 }).catch(() => ({ results: [] })),
                ]);

                // Build results from title matches (synthetic result objects for SearchResultCard)
                const seen = new Set();
                const merged = [];
                for (const conv of (Array.isArray(titleMatches) ? titleMatches : [])) {
                    seen.add(conv.id);
                    merged.push({
                        conversation_id: conv.id,
                        title: conv.goal || conv.id,
                        snippet: null,
                        relevance_score: 2.0, // title matches rank highest
                        message_type: null,
                        author: null,
                        created_at: conv.updated_at,
                        _conv: conv,
                    });
                }

                // Add vector search hits that reference conversations
                const vecResults = (vecData.results || [])
                    .filter(r => r.conversation_id && !seen.has(r.conversation_id));
                for (const r of vecResults) {
                    if (seen.has(r.conversation_id)) continue;
                    seen.add(r.conversation_id);
                    merged.push(r);
                }

                // Sort by relevance (title matches first via high score, then by vec score)
                merged.sort((a, b) => (b.relevance_score ?? 0) - (a.relevance_score ?? 0));
                setSearchResults(merged);
            } catch (_) {
                setSearchResults([]);
            } finally {
                setSearchLoading(false);
            }
        }, 300);
    }, [projectId]);

    // Group conversations
    const pinnedConvs = conversations.filter(c => c.has_pinned);
    const unpinnedConvs = conversations.filter(c => !c.has_pinned);

    // Styles
    const containerStyle = {
        display: 'flex',
        flexDirection: 'column',
        gap: '0',
    };

    const headerRowStyle = {
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        marginBottom: '16px',
        gap: '12px',
    };

    const searchStyle = {
        flex: 1,
        padding: '7px 12px',
        borderRadius: layout.borderRadius.md,
        border: `1px solid ${colors.border}`,
        background: colors.input,
        color: colors.text,
        fontSize: typography.size.sm,
        fontFamily: typography.fontBody,
        outline: 'none',
    };

    const newBtnStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '6px',
        padding: '7px 14px',
        borderRadius: layout.borderRadius.md,
        background: colors.accentBg,
        border: `1px solid ${colors.accent}44`,
        color: colors.accent,
        fontSize: typography.size.sm,
        fontWeight: typography.weight.medium,
        cursor: 'pointer',
        textDecoration: 'none',
        whiteSpace: 'nowrap',
        flexShrink: 0,
    };

    const listStyle = {
        display: 'flex',
        flexDirection: 'column',
        gap: '4px',
        marginBottom: '20px',
    };

    const emptyStyle = {
        padding: '40px 0',
        textAlign: 'center',
        color: colors.textTertiary,
        fontSize: typography.size.sm,
    };

    if (loading) {
        return html`
            <div style=${emptyStyle}>Loading conversations…</div>
        `;
    }

    if (error) {
        return html`
            <div style=${{ ...emptyStyle, color: colors.red }}>Error: ${error}</div>
        `;
    }

    // Search results mode
    const showSearch = searchQuery.trim().length > 0;

    return html`
        <div style=${containerStyle}>
            <!-- Header: search + new button -->
            <div style=${headerRowStyle}>
                <input
                    type="search"
                    placeholder="Search conversations…"
                    value=${searchQuery}
                    onInput=${e => handleSearch(e.target.value)}
                    style=${searchStyle}
                    class="foreman-conv-search"
                />
                <a
                    href="#/conversation/new?project=${encodeURIComponent(projectId)}"
                    style=${newBtnStyle}
                    class="foreman-new-conv-btn"
                >+ New conversation</a>
            </div>

            ${showSearch ? html`
                <!-- Search results -->
                ${searchLoading ? html`
                    <div style=${emptyStyle}>Searching…</div>
                ` : searchResults && searchResults.length === 0 ? html`
                    <div style=${emptyStyle}>No results for "${searchQuery}"</div>
                ` : searchResults ? html`
                    <div style=${listStyle}>
                        ${searchResults.map(r => {
                            const conv = r._conv || conversations.find(c => c.id === r.conversation_id) || {
                                id: r.conversation_id,
                                goal: r.title || r.conversation_id,
                                has_pinned: false,
                                pinned_title: null,
                                message_count: null,
                                updated_at: null,
                            };
                            return html`<${SearchResultCard} key=${r.conversation_id + '-' + (r.message_id || '')} result=${r} conv=${conv} projectId=${projectId} searchQuery=${searchQuery} />`;
                        })}
                    </div>
                ` : null}
            ` : html`
                <!-- Pinned group -->
                ${pinnedConvs.length > 0 ? html`
                    <div>
                        <${SectionHeader} label="Has pinned message" count=${pinnedConvs.length} accent=${true} />
                        <div style=${listStyle}>
                            ${pinnedConvs.map(conv => html`
                                <${ConversationCard} key=${conv.id} conv=${conv} projectId=${projectId} />
                            `)}
                        </div>
                    </div>
                ` : null}

                <!-- All conversations group -->
                ${unpinnedConvs.length > 0 ? html`
                    <div>
                        <${SectionHeader} label="All conversations" count=${unpinnedConvs.length} accent=${false} />
                        <div style=${listStyle}>
                            ${unpinnedConvs.map(conv => html`
                                <${ConversationCard} key=${conv.id} conv=${conv} projectId=${projectId} />
                            `)}
                        </div>
                    </div>
                ` : pinnedConvs.length === 0 ? html`
                    <div style=${emptyStyle}>No conversations yet</div>
                ` : null}
            `}
        </div>
    `;
}
