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

    // Debounced search
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
                const data = await api.search({ q, project_id: projectId });
                const results = (data.results || []).filter(r => r.conversation_id);
                setSearchResults(results);
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
                            const conv = conversations.find(c => c.id === r.conversation_id) || {
                                id: r.conversation_id,
                                goal: r.title || r.conversation_id,
                                has_pinned: false,
                                pinned_title: null,
                                message_count: null,
                                updated_at: null,
                            };
                            return html`<${ConversationCard} key=${conv.id} conv=${conv} projectId=${projectId} />`;
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
