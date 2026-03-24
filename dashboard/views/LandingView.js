// Foreman Landing View — Project card grid with health indicators
// Smart attention model: failed + needs-review + stalled + repeated failures + blocked chains

import { h } from 'https://esm.sh/preact@10.25.4';
import { useState, useEffect, useCallback } from 'https://esm.sh/preact@10.25.4/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, layout } from '../tokens.js';
import { routes } from '../router.js';
import { api } from '../api.js';

const html = htm.bind(h);

const STALL_THRESHOLD_MS = 30 * 60 * 1000; // 30 minutes
const POLL_INTERVAL_MS = 30_000;

// ---------------------------------------------------------------------------
// Attention model
// ---------------------------------------------------------------------------

/**
 * Compute attention count for a project's tasks.
 * Counts distinct tasks that need human attention:
 *   - failed or needs-review (status-based)
 *   - CC questions: pending_questions > 0 (deferred — API field not yet exposed; will count when available)
 *   - stalled: working with no activity > 30min
 *   - repeated failures: gate_retries >= 2
 *   - blocked chains: depends_on a failed task, not yet cancelled/completed
 */
function computeAttention(projectTasks) {
    const now = Date.now();
    const seen = new Set();

    // Build set of failed task IDs for blocked-chain detection
    const failedIds = new Set(
        projectTasks.filter(t => t.status === 'failed').map(t => t.id)
    );

    for (const t of projectTasks) {
        if (seen.has(t.id)) continue;

        // Status-based: failed, needs-review
        if (t.status === 'failed' || t.status === 'needs-review') {
            seen.add(t.id);
            continue;
        }

        // CC questions: worker is waiting for human input
        // NOTE: requires API to expose pending_questions on task rows (not yet implemented)
        if ((t.pending_questions || 0) > 0) {
            seen.add(t.id);
            continue;
        }

        // Stalled: working but no activity for >30min
        if (t.status === 'working' && t.last_activity) {
            const lastAct = new Date(t.last_activity).getTime();
            if (now - lastAct > STALL_THRESHOLD_MS) {
                seen.add(t.id);
                continue;
            }
        }

        // Repeated failures: gate has been retried >= 2 times
        if ((t.gate_retries || 0) >= 2) {
            seen.add(t.id);
            continue;
        }

        // Blocked chains: depends_on a failed task, still active
        if (t.depends_on && failedIds.has(t.depends_on) &&
            !['cancelled', 'completed'].includes(t.status)) {
            seen.add(t.id);
        }
    }

    return seen.size;
}

/**
 * Compute completion ratio for a project's tasks.
 * Ratio = completed / (completed + failed + working + needs-review + rate-limited + turns-exhausted)
 * Excludes ready (not started) and cancelled (abandoned).
 */
function computeCompletion(projectTasks) {
    const inFlight = projectTasks.filter(t =>
        ['completed', 'failed', 'working', 'needs-review', 'rate-limited', 'turns-exhausted'].includes(t.status)
    );
    if (inFlight.length === 0) return null;
    const completed = inFlight.filter(t => t.status === 'completed').length;
    return { completed, total: inFlight.length };
}

// ---------------------------------------------------------------------------
// Project card
// ---------------------------------------------------------------------------

function ProjectCard({ project, tasks }) {
    const projectTasks = tasks.filter(t => t.project_id === project.id);

    const runningCount = projectTasks.filter(t => t.status === 'working').length;
    const attentionCount = computeAttention(projectTasks);
    const completion = computeCompletion(projectTasks);
    const hasAttention = attentionCount > 0;

    // Repo shortname: strip common owner prefix if present (e.g. "org/repo" → "repo")
    const repoShort = (project.repo || project.id).split('/').pop();

    const cardStyle = {
        background: colors.surface,
        border: `1px solid ${hasAttention ? colors.yellow : colors.border}`,
        borderRadius: layout.borderRadius.lg,
        padding: '16px 20px',
        cursor: 'pointer',
        transition: `border-color 200ms, background 200ms, box-shadow 200ms`,
        position: 'relative',
        display: 'flex',
        flexDirection: 'column',
        gap: '10px',
        textDecoration: 'none',
        color: 'inherit',
        ...(hasAttention ? { boxShadow: `0 0 0 1px ${colors.yellow}22` } : {}),
    };

    const titleRowStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
    };

    const dotStyle = {
        width: '8px',
        height: '8px',
        borderRadius: '50%',
        flexShrink: 0,
        backgroundColor: hasAttention ? colors.yellow : (runningCount > 0 ? colors.green : colors.textTertiary),
    };

    const nameStyle = {
        fontFamily: typography.fontBody,
        fontSize: typography.size.md,
        fontWeight: typography.weight.semibold,
        color: colors.text,
        flex: 1,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        letterSpacing: '-0.01em',
    };

    const repoStyle = {
        fontFamily: typography.fontMono,
        fontSize: typography.size.sm,
        color: colors.textTertiary,
    };

    const statsRowStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '12px',
        fontSize: typography.size.sm,
    };

    const runningStyle = {
        color: runningCount > 0 ? colors.green : colors.textTertiary,
        fontWeight: runningCount > 0 ? typography.weight.medium : typography.weight.normal,
        whiteSpace: 'nowrap',
    };

    return html`
        <a
            href=${routes.project(project.id)}
            style=${cardStyle}
            class="foreman-project-card"
        >
            <div style=${titleRowStyle}>
                <span
                    style=${dotStyle}
                    class=${hasAttention ? 'foreman-status-dot-pulse' : ''}
                />
                <span style=${nameStyle}>${project.id}</span>
                ${hasAttention ? html`
                    <span class="foreman-attention-badge">${attentionCount}</span>
                ` : null}
            </div>

            <div style=${repoStyle}>${repoShort}</div>

            <div style=${statsRowStyle}>
                <span style=${runningStyle}>
                    ${runningCount > 0 ? `${runningCount} running` : 'idle'}
                </span>
                ${completion ? html`
                    <span style=${{ color: colors.textTertiary }}>·</span>
                    <${CompletionBar} completed=${completion.completed} total=${completion.total} />
                ` : null}
            </div>
        </a>
    `;
}

// ---------------------------------------------------------------------------
// Completion bar
// ---------------------------------------------------------------------------

function CompletionBar({ completed, total }) {
    const pct = total > 0 ? Math.round((completed / total) * 100) : 0;

    const containerStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        flex: 1,
        minWidth: 0,
    };

    const trackStyle = {
        flex: 1,
        height: '3px',
        background: colors.border,
        borderRadius: '2px',
        overflow: 'hidden',
        minWidth: '40px',
    };

    const fillStyle = {
        height: '100%',
        width: `${pct}%`,
        background: pct === 100 ? colors.blue : colors.accent,
        borderRadius: '2px',
        transition: 'width 400ms ease',
    };

    const labelStyle = {
        fontFamily: typography.fontMono,
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        whiteSpace: 'nowrap',
        flexShrink: 0,
    };

    return html`
        <div style=${containerStyle}>
            <div style=${trackStyle}>
                <div style=${fillStyle} />
            </div>
            <span style=${labelStyle}>${completed}/${total}</span>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

function EmptyState() {
    const style = {
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '80px 24px',
        gap: '12px',
        textAlign: 'center',
    };

    return html`
        <div style=${style}>
            <div style=${{ fontSize: '32px', marginBottom: '4px' }}>◇</div>
            <div style=${{ fontSize: typography.size.md, fontWeight: typography.weight.medium, color: colors.text }}>
                No projects yet
            </div>
            <div style=${{ fontSize: typography.size.sm, color: colors.textSecondary, maxWidth: '320px', lineHeight: 1.6 }}>
                Register a project to start dispatching tasks.
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Loading skeleton
// ---------------------------------------------------------------------------

function CardSkeleton() {
    const cardStyle = {
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.lg,
        padding: '16px 20px',
        display: 'flex',
        flexDirection: 'column',
        gap: '10px',
    };

    const lineStyle = (w, h = '12px') => ({
        height: h,
        width: w,
        background: colors.surfaceActive,
        borderRadius: '4px',
    });

    return html`
        <div style=${cardStyle} class="foreman-skeleton">
            <div style=${{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <div style=${{ width: '8px', height: '8px', borderRadius: '50%', background: colors.surfaceActive }} />
                <div style=${lineStyle('60%', '14px')} />
            </div>
            <div style=${lineStyle('30%')} />
            <div style=${lineStyle('45%')} />
        </div>
    `;
}

// ---------------------------------------------------------------------------
// LandingView
// ---------------------------------------------------------------------------

export function LandingView() {
    const [projects, setProjects] = useState(null);
    const [tasks, setTasks] = useState([]);
    const [error, setError] = useState(null);
    const [lastRefresh, setLastRefresh] = useState(null);

    const load = useCallback(async () => {
        try {
            const [projectList, taskList] = await Promise.all([
                api.getProjects(),
                api.getTasks(),
            ]);
            setProjects(projectList);
            setTasks(taskList);
            setLastRefresh(new Date());
            setError(null);
        } catch (e) {
            setError(e.message || 'Failed to load');
        }
    }, []);

    // Initial load
    useEffect(() => {
        load();
    }, [load]);

    // Polling
    useEffect(() => {
        const timer = setInterval(load, POLL_INTERVAL_MS);
        return () => clearInterval(timer);
    }, [load]);

    const pageStyle = {
        display: 'flex',
        flexDirection: 'column',
        gap: '24px',
    };

    const headerStyle = {
        display: 'flex',
        alignItems: 'baseline',
        justifyContent: 'space-between',
        paddingBottom: '16px',
        borderBottom: `1px solid ${colors.border}`,
    };

    const titleStyle = {
        fontFamily: typography.fontBody,
        fontSize: typography.size['2xl'],
        fontWeight: typography.weight.semibold,
        color: colors.text,
        margin: 0,
        letterSpacing: '-0.02em',
    };

    const subtitleStyle = {
        fontSize: typography.size.sm,
        color: colors.textTertiary,
    };

    const gridStyle = {
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
        gap: '12px',
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

    // Render skeletons during initial load
    const isLoading = projects === null && error === null;

    return html`
        <div style=${pageStyle}>
            <div style=${headerStyle}>
                <h1 style=${titleStyle}>Projects</h1>
                ${lastRefresh ? html`
                    <span style=${subtitleStyle}>
                        ${projects ? `${projects.length} project${projects.length !== 1 ? 's' : ''}` : ''}
                    </span>
                ` : null}
            </div>

            ${error ? html`
                <div style=${errorStyle}>
                    <span>Failed to load: ${error}</span>
                    <button style=${retryBtnStyle} onClick=${load}>Retry</button>
                </div>
            ` : null}

            ${isLoading ? html`
                <div style=${gridStyle}>
                    ${[1, 2, 3].map(i => html`<${CardSkeleton} key=${i} />`)}
                </div>
            ` : projects && projects.length === 0 ? html`
                <${EmptyState} />
            ` : projects ? html`
                <div style=${gridStyle}>
                    ${[...projects].sort((a, b) => {
                        const latestActivity = (proj) => {
                            const projTasks = tasks.filter(t => t.project_id === proj.id);
                            if (projTasks.length === 0) return '';
                            return projTasks.reduce((max, t) => (t.last_activity || t.updated_at || '') > max ? (t.last_activity || t.updated_at || '') : max, '');
                        };
                        return latestActivity(b).localeCompare(latestActivity(a));
                    }).map(p => html`
                        <${ProjectCard}
                            key=${p.id}
                            project=${p}
                            tasks=${tasks}
                        />
                    `)}
                </div>
            ` : null}
        </div>
    `;
}
