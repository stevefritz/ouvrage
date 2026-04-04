import { h } from 'https://esm.sh/preact@10.25.4';
import { useState } from 'https://esm.sh/preact@10.25.4/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, layout } from '../tokens.js';
import { FilterBar } from './FilterBar.js';
import { TaskRow } from './TaskRow.js';

const html = htm.bind(h);

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
                    <${TaskRow}
                        key=${task.id}
                        task=${task}
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
