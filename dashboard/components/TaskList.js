import { h } from 'https://esm.sh/preact@10.25.4';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, layout, animation } from '../tokens.js';
import { navigate } from '../router.js';
import { FilterBar } from './FilterBar.js';
import { TaskRow } from './TaskRow.js';

const html = htm.bind(h);

// ---------------------------------------------------------------------------
// SearchResultRow — semantic search result card
// ---------------------------------------------------------------------------

function SearchResultRow({ result }) {
    const handleClick = () => {
        if (result.task_id) {
            navigate(`/task/${result.task_id}`);
        } else if (result.conversation_id) {
            navigate(`/conversations/${result.conversation_id}`);
        }
    };

    const rowStyle = {
        display: 'flex',
        flexDirection: 'column',
        padding: '10px 12px',
        borderRadius: layout.borderRadius.md,
        border: `1px solid ${colors.border}`,
        marginBottom: '6px',
        background: colors.surface,
        cursor: 'pointer',
        transition: `background ${animation.durationFast}`,
    };

    const typeColors = {
        task: colors.accent,
        task_message: colors.blue,
        conversation_message: colors.green,
        chunk: colors.yellow,
    };
    const typeColor = typeColors[result.type] || colors.textTertiary;

    const typeBadgeStyle = {
        display: 'inline-block',
        fontSize: '10px',
        fontWeight: typography.weight.semibold,
        color: typeColor,
        background: `${typeColor}20`,
        borderRadius: layout.borderRadius.sm,
        padding: '1px 6px',
        textTransform: 'uppercase',
        letterSpacing: '0.05em',
        marginRight: '8px',
    };

    const titleStyle = {
        fontSize: typography.size.sm,
        fontWeight: typography.weight.medium,
        color: colors.text,
        marginBottom: '3px',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
    };

    const snippetStyle = {
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        display: '-webkit-box',
        WebkitLineClamp: 2,
        WebkitBoxOrient: 'vertical',
    };

    const metaStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '6px',
        marginBottom: '4px',
    };

    const contextStyle = {
        fontSize: '11px',
        color: colors.textTertiary,
    };

    const typeLabel = result.type === 'task' ? 'task'
        : result.type === 'task_message' ? 'message'
        : result.type === 'conversation_message' ? 'conv message'
        : 'excerpt';

    return html`
        <div
            style=${rowStyle}
            onClick=${handleClick}
            onMouseEnter=${e => e.currentTarget.style.background = colors.surfaceHover}
            onMouseLeave=${e => e.currentTarget.style.background = colors.surface}
        >
            <div style=${metaStyle}>
                <span style=${typeBadgeStyle}>${typeLabel}</span>
                ${result.task_id ? html`<span style=${contextStyle}>${result.task_id}</span>` : null}
            </div>
            ${result.title ? html`<div style=${titleStyle}>${result.title}</div>` : null}
            ${result.snippet ? html`<div style=${snippetStyle}>${result.snippet}</div>` : null}
        </div>
    `;
}

// ---------------------------------------------------------------------------
// TaskList — task list section (header, filter bar, task rows)
// ---------------------------------------------------------------------------

export function TaskList({ tasks, conversations, chainMap, statusFilter, onStatusFilter, onTaskSelect,
    searchQuery, searchResults, searchLoading, onSearch, projectId }) {

    // Filter normal task list (used when no search active)
    let filtered = tasks;
    if (statusFilter) filtered = filtered.filter(t => t.status === statusFilter);

    // Sort by last_activity descending — flat list, no grouping
    const ts = (t) => {
        const raw = t.last_activity || t.updated_at || t.created_at || '1970-01-01T00:00:00Z';
        return new Date(raw.endsWith('Z') ? raw : raw + 'Z').getTime();
    };
    filtered = [...filtered].sort((a, b) => ts(b) - ts(a));

    const isSearchActive = !!searchQuery;

    // When search is active, filter search results by status dropdown too
    let displayTasks = isSearchActive ? (searchResults || []) : filtered;
    if (isSearchActive && statusFilter && searchResults) {
        displayTasks = searchResults.filter(t => t.status === statusFilter);
    }

    const sectionStyle = {
        display: 'flex',
        flexDirection: 'column',
    };

    const sectionHeaderStyle = {
        fontSize: typography.size.sm,
        fontWeight: typography.weight.semibold,
        color: colors.textSecondary,
        letterSpacing: '0.06em',
        textTransform: 'uppercase',
        marginBottom: '8px',
    };

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

    const headerLabel = searchLoading
        ? 'Tasks · …'
        : isSearchActive && searchResults
            ? `Tasks · ${displayTasks.length}`
            : `Tasks · ${filtered.length}`;

    return html`
        <div style=${sectionStyle}>
            <style>${`
                @media (max-width: 640px) {
                    .foreman-task-row { flex-wrap: wrap; }
                    .foreman-task-row-tags { width: 100%; flex-wrap: wrap; margin-top: 2px; }
                }
            `}</style>
            <div style=${sectionHeaderStyle}>${headerLabel}</div>

            <${FilterBar}
                statusFilter=${statusFilter}
                onStatusFilter=${onStatusFilter}
                searchQuery=${searchQuery}
                onSearch=${onSearch}
            />

            ${isSearchActive ? html`
                ${searchLoading ? html`
                    <div style=${loadingStyle}>Searching…</div>
                ` : searchResults && displayTasks.length === 0 ? html`
                    <div style=${emptyStyle}>No results found for "${searchQuery}"</div>
                ` : searchResults ? displayTasks.map(task => html`
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
                ${filtered.length === 0 ? html`
                    <div style=${emptyStyle}>No tasks match the current filters</div>
                ` : filtered.map(task => html`
                    <${TaskRow}
                        key=${task.id}
                        task=${task}
                        chainMap=${chainMap}
                        allTasks=${tasks}
                        conversations=${conversations}
                        onSelect=${onTaskSelect}
                    />
                `)}
            `}
        </div>
    `;
}
