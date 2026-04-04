import { h } from 'https://esm.sh/preact@10.25.4';
import { useState } from 'https://esm.sh/preact@10.25.4/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, layout, animation } from '../tokens.js';
import { FilterBar } from './FilterBar.js';
import { TaskRow } from './TaskRow.js';

const html = htm.bind(h);

// ---------------------------------------------------------------------------
// Message type metadata for search result badges
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
};

// ---------------------------------------------------------------------------
// SearchResultRow — TaskRow with search snippet overlay
// ---------------------------------------------------------------------------

function SearchResultRow({ task, searchQuery, chainMap, allTasks, conversations, onSelect }) {
    const hit = task._searchHit;
    if (!hit) {
        return html`<${TaskRow} task=${task} chainMap=${chainMap} allTasks=${allTasks}
            conversations=${conversations} onSelect=${onSelect} />`;
    }

    const snippetStyle = {
        fontSize: typography.size.xs,
        color: colors.textSecondary,
        padding: '0 14px 6px 14px',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        marginTop: '-4px',
    };

    const badgeStyle = (meta) => ({
        display: 'inline-block',
        padding: '1px 5px',
        borderRadius: '9px',
        background: meta.bg,
        color: meta.color,
        fontSize: typography.size.xs,
        fontWeight: 500,
        fontFamily: 'var(--font-mono, monospace)',
        lineHeight: '1.4',
        marginRight: '6px',
    });

    const meta = hit.message_type ? MSG_META[hit.message_type] : null;

    // Highlight query terms in snippet
    const snippet = hit.snippet || '';
    let snippetContent = snippet;
    if (snippet && searchQuery) {
        const escaped = searchQuery.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const parts = snippet.split(new RegExp(`(${escaped})`, 'gi'));
        snippetContent = html`${parts.map((part, i) =>
            i % 2 === 1
                ? html`<mark key=${i} style=${{ background: 'rgba(255,220,50,0.35)', color: 'inherit', borderRadius: '2px', padding: '0 1px' }}>${part}</mark>`
                : part
        )}`;
    }

    return html`
        <div>
            <${TaskRow} task=${task} chainMap=${chainMap} allTasks=${allTasks}
                conversations=${conversations} onSelect=${onSelect} />
            ${snippet ? html`
                <div style=${snippetStyle}>
                    ${meta ? html`<span style=${badgeStyle(meta)}>${meta.label}</span>` : null}
                    ${snippetContent}
                </div>
            ` : null}
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Section label style — 11px uppercase muted, 600 weight
// ---------------------------------------------------------------------------

const sectionLabelStyle = {
    fontSize: '11px',
    fontWeight: 600,
    color: colors.textTertiary,
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
    marginBottom: '8px',
};

// ---------------------------------------------------------------------------
// AccentTaskRow — TaskRow wrapped with a left border accent
// ---------------------------------------------------------------------------

function AccentTaskRow({ task, accentColor, chainMap, allTasks, conversations, onSelect }) {
    const wrapStyle = {
        borderLeft: `3px solid ${accentColor}`,
        borderRadius: `0 ${layout.borderRadius.md} ${layout.borderRadius.md} 0`,
        marginBottom: '2px',
    };
    return html`
        <div style=${wrapStyle}>
            <${TaskRow}
                task=${task}
                chainMap=${chainMap}
                allTasks=${allTasks}
                conversations=${conversations}
                onSelect=${onSelect}
            />
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Task categorization constants
// ---------------------------------------------------------------------------

const NEEDS_ATTENTION_STATUSES = new Set(['needs-review', 'failed', 'stopped', 'turns-exhausted']);
const TWELVE_HOURS_MS = 12 * 60 * 60 * 1000;

function getTimestamp(t) {
    const raw = t.last_activity || t.updated_at || t.created_at || '1970-01-01T00:00:00Z';
    return new Date(raw.endsWith('Z') ? raw : raw + 'Z').getTime();
}

function accentColorForTask(task) {
    return task.status === 'needs-review' ? colors.yellow : colors.red;
}

// ---------------------------------------------------------------------------
// TaskList — three-section layout with search
// ---------------------------------------------------------------------------

export function TaskList({ tasks, conversations, chainMap, statusFilter, onStatusFilter, onTaskSelect,
    searchQuery, searchResults, searchLoading, onSearch, projectId }) {

    const [olderExpanded, setOlderExpanded] = useState(false);

    const isSearchActive = !!searchQuery;

    // Categorize tasks into three groups
    const now = Date.now();
    const needsAttention = tasks.filter(t => NEEDS_ATTENTION_STATUSES.has(t.status));
    const recentTasks = tasks
        .filter(t => (now - getTimestamp(t)) <= TWELVE_HOURS_MS)
        .sort((a, b) => getTimestamp(b) - getTimestamp(a));

    const attentionIds = new Set(needsAttention.map(t => t.id));
    const recentIds = new Set(recentTasks.map(t => t.id));
    const olderTasks = tasks
        .filter(t => !attentionIds.has(t.id) && !recentIds.has(t.id))
        .sort((a, b) => getTimestamp(b) - getTimestamp(a));

    // Search results: apply status filter if set
    let displaySearchResults = searchResults || [];
    if (isSearchActive && statusFilter && searchResults) {
        displaySearchResults = searchResults.filter(t => t.status === statusFilter);
    }

    const emptyStyle = {
        padding: '32px 0',
        textAlign: 'center',
        color: colors.textTertiary,
        fontSize: typography.size.sm,
    };

    const loadingStyle = {
        padding: '20px 0',
        textAlign: 'center',
        color: colors.textTertiary,
        fontSize: typography.size.sm,
    };

    const olderToggleStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        cursor: 'pointer',
        padding: '10px 0',
        color: colors.textSecondary,
        fontSize: typography.size.sm,
        userSelect: 'none',
        borderTop: `1px solid ${colors.border}22`,
        marginTop: '4px',
    };

    return html`
        <div style=${{ display: 'flex', flexDirection: 'column' }}>
            <style>${`
                @media (max-width: 640px) {
                    .foreman-task-row { flex-wrap: wrap; }
                    .foreman-task-row-tags { width: 100%; flex-wrap: wrap; margin-top: 2px; }
                }
            `}</style>

            <!-- Search/filter bar always at top -->
            <${FilterBar}
                statusFilter=${statusFilter}
                onStatusFilter=${onStatusFilter}
                searchQuery=${searchQuery}
                onSearch=${onSearch}
            />

            ${isSearchActive ? html`
                <!-- Search results mode -->
                ${searchLoading ? html`
                    <div style=${loadingStyle}>Searching…</div>
                ` : searchResults && displaySearchResults.length === 0 ? html`
                    <div style=${emptyStyle}>No results found for "${searchQuery}"</div>
                ` : searchResults ? displaySearchResults.map(task => html`
                    <${SearchResultRow}
                        key=${task.id}
                        task=${task}
                        searchQuery=${searchQuery}
                        chainMap=${chainMap}
                        allTasks=${tasks}
                        conversations=${conversations}
                        onSelect=${onTaskSelect}
                    />
                `) : null}
            ` : html`
                <!-- Section mode -->

                <!-- Section 1: Needs Attention -->
                ${needsAttention.length > 0 ? html`
                    <div style=${{ marginBottom: '24px' }}>
                        <div style=${sectionLabelStyle}>Needs Attention</div>
                        ${needsAttention.map(task => html`
                            <${AccentTaskRow}
                                key=${task.id}
                                task=${task}
                                accentColor=${accentColorForTask(task)}
                                chainMap=${chainMap}
                                allTasks=${tasks}
                                conversations=${conversations}
                                onSelect=${onTaskSelect}
                            />
                        `)}
                    </div>
                ` : null}

                <!-- Section 2: Recent Activity (past 12h) -->
                ${recentTasks.length > 0 ? html`
                    <div style=${{ marginBottom: '24px' }}>
                        <div style=${sectionLabelStyle}>Recent Activity (past 12h)</div>
                        ${recentTasks.map(task => html`
                            <${TaskRow}
                                key=${task.id}
                                task=${task}
                                chainMap=${chainMap}
                                allTasks=${tasks}
                                conversations=${conversations}
                                onSelect=${onTaskSelect}
                            />
                        `)}
                    </div>
                ` : null}

                <!-- Section 3: Older Tasks (collapsed) -->
                ${olderTasks.length > 0 ? html`
                    <div style=${{ marginBottom: '24px' }}>
                        <div
                            style=${olderToggleStyle}
                            onClick=${() => setOlderExpanded(e => !e)}
                        >
                            <span style=${{ color: colors.textTertiary, fontSize: '10px' }}>
                                ${olderExpanded ? '▾' : '▸'}
                            </span>
                            <span style=${sectionLabelStyle}>
                                Older Tasks (${olderTasks.length})
                            </span>
                        </div>
                        ${olderExpanded ? olderTasks.map(task => html`
                            <${TaskRow}
                                key=${task.id}
                                task=${task}
                                chainMap=${chainMap}
                                allTasks=${tasks}
                                conversations=${conversations}
                                onSelect=${onTaskSelect}
                            />
                        `) : null}
                    </div>
                ` : null}

                ${needsAttention.length === 0 && recentTasks.length === 0 && olderTasks.length === 0 ? html`
                    <div style=${emptyStyle}>No tasks yet</div>
                ` : null}
            `}
        </div>
    `;
}
