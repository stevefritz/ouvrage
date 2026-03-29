// Foreman Architecture Docs — Interactive tabbed reference
// Preact/htm, no build step. Same CDN imports as the rest of the dashboard.
// Route: #/docs

import { h } from 'https://esm.sh/preact@10.25.4';
import { useState } from 'https://esm.sh/preact@10.25.4/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, layout } from '../tokens.js';

const html = htm.bind(h);

// ---------------------------------------------------------------------------
// Shared style helpers
// ---------------------------------------------------------------------------

const S = {
    card: {
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.lg,
        padding: '16px 20px',
        marginBottom: '12px',
    },
    codeBlock: {
        background: '#0d0e10',
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.md,
        padding: '12px 16px',
        fontFamily: typography.fontMono,
        fontSize: '12px',
        color: '#a8d8a8',
        overflowX: 'auto',
        whiteSpace: 'pre',
        lineHeight: 1.6,
        margin: '8px 0',
    },
    h2: {
        fontFamily: typography.fontBody,
        fontSize: typography.size['2xl'],
        fontWeight: typography.weight.semibold,
        color: colors.text,
        margin: '0 0 20px 0',
        letterSpacing: '-0.02em',
    },
    h3: {
        fontFamily: typography.fontBody,
        fontSize: typography.size.lg,
        fontWeight: typography.weight.semibold,
        color: colors.text,
        margin: '0 0 10px 0',
    },
    h4: {
        fontFamily: typography.fontBody,
        fontSize: typography.size.base,
        fontWeight: typography.weight.semibold,
        color: colors.textSecondary,
        margin: '0 0 6px 0',
        textTransform: 'uppercase',
        letterSpacing: '0.06em',
        fontSize: '11px',
    },
    label: {
        display: 'inline-block',
        padding: '2px 8px',
        borderRadius: layout.borderRadius.pill,
        fontSize: '11px',
        fontWeight: typography.weight.semibold,
        fontFamily: typography.fontMono,
        marginRight: '6px',
    },
    tag: (color, bg) => ({
        display: 'inline-block',
        padding: '2px 8px',
        borderRadius: layout.borderRadius.pill,
        fontSize: '11px',
        fontWeight: typography.weight.medium,
        background: bg || 'rgba(124,90,246,0.15)',
        color: color || colors.accent,
        marginRight: '4px',
        marginBottom: '4px',
    }),
    row: {
        display: 'flex',
        gap: '12px',
        flexWrap: 'wrap',
        marginBottom: '12px',
    },
    col: (flex = 1) => ({
        flex,
        minWidth: '180px',
    }),
    fileEntry: {
        display: 'flex',
        gap: '12px',
        padding: '8px 0',
        borderBottom: `1px solid ${colors.borderSubtle}`,
        alignItems: 'flex-start',
    },
    fileName: {
        fontFamily: typography.fontMono,
        fontSize: '12px',
        color: colors.accent,
        flexShrink: 0,
        width: '280px',
    },
    fileDesc: {
        fontFamily: typography.fontBody,
        fontSize: '13px',
        color: colors.textSecondary,
        lineHeight: 1.5,
    },
    arrow: {
        color: colors.textTertiary,
        fontSize: '18px',
        margin: '0 8px',
        flexShrink: 0,
    },
    flowBox: (accent) => ({
        background: accent ? colors.accentBg : colors.surface,
        border: `1px solid ${accent ? colors.accent : colors.border}`,
        borderRadius: layout.borderRadius.md,
        padding: '10px 14px',
        fontSize: '13px',
        color: accent ? colors.accent : colors.text,
        fontWeight: accent ? typography.weight.medium : typography.weight.normal,
        fontFamily: typography.fontBody,
        textAlign: 'center',
        minWidth: '100px',
    }),
    tableHeader: {
        background: colors.surfaceActive,
        borderBottom: `1px solid ${colors.border}`,
        padding: '8px 12px',
        fontSize: '11px',
        fontWeight: typography.weight.semibold,
        color: colors.textTertiary,
        textTransform: 'uppercase',
        letterSpacing: '0.06em',
    },
    tableCell: {
        padding: '8px 12px',
        borderBottom: `1px solid ${colors.borderSubtle}`,
        fontSize: '13px',
        color: colors.text,
        verticalAlign: 'top',
    },
    tableCellMono: {
        padding: '8px 12px',
        borderBottom: `1px solid ${colors.borderSubtle}`,
        fontFamily: typography.fontMono,
        fontSize: '12px',
        color: colors.accent,
        verticalAlign: 'top',
    },
};

// ---------------------------------------------------------------------------
// Tab 1: System Architecture
// ---------------------------------------------------------------------------

function TabSystemArchitecture() {
    return html`
        <div>
            <h2 style=${S.h2}>System Architecture</h2>
            <p style=${{ color: colors.textSecondary, marginBottom: '24px', lineHeight: 1.6 }}>
                Switchboard (branded "Foreman" in the UI) is a raw ASGI task orchestration platform.
                It dispatches Claude Code workers to isolated git worktrees, manages gates, and surfaces
                everything through a CDN-loaded Preact dashboard.
            </p>

            <!-- Top-level data flow -->
            <div style=${{ ...S.card, marginBottom: '24px' }}>
                <h3 style=${S.h3}>Request Data Flow</h3>
                <div style=${{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: '8px', padding: '12px 0' }}>
                    ${['MCP Client', 'app.py (ASGI)', 'StreamableHTTP\nSessionManager', 'server.call_tool()', 'dispatch.py\n_dispatch_tool()', 'TOOL_HANDLERS[name]', 'handlers/*.py', 'db.*', 'JSON result']
                        .map((step, i, arr) => html`
                            <div style=${S.flowBox(i === 0 || i === arr.length - 1)}>${step}</div>
                            ${i < arr.length - 1 ? html`<span style=${S.arrow}>→</span>` : null}
                        `)}
                </div>
            </div>

            <!-- Subsystem grid -->
            <h3 style=${S.h3}>Subsystems</h3>
            <div style=${S.row}>
                ${[
                    { name: 'Server Layer', color: colors.accent, items: ['app.py — ASGI entry, route matching', 'tools.py — 70+ MCP tool schemas', 'dispatch.py — TOOL_HANDLERS dict', 'handlers/ — domain handlers'] },
                    { name: 'Dispatch Engine', color: colors.blue, items: ['engine.py — task lifecycle', 'gates.py — test + review gates', 'sdk_session.py — CC SDK bridge', 'queue.py — concurrency FIFO', 'recovery.py — crash recovery'] },
                    { name: 'Git Layer', color: colors.green, items: ['worktree.py — bare clone + worktrees', 'operations.py — push/PR/merge', 'files.py — file utilities'] },
                    { name: 'Auth Layer', color: colors.yellow, items: ['middleware.py — JWT + session check', 'oauth.py — RS256 OAuth server', 'sessions.py — cookie auth, Argon2id'] },
                    { name: 'Data Layer', color: '#f09f56', items: ['db/connection.py — singleton aiosqlite', 'db/schema.py — 21 tables', 'db/tasks.py, conversations.py…', 'embeddings/ — semantic search'] },
                    { name: 'Dashboard (SPA)', color: colors.textSecondary, items: ['foreman.html + foreman-app.js', 'router.js — hash routing', 'views/ + components/', 'dashboard/api.py — REST endpoints'] },
                ].map(sub => html`
                    <div style=${{ ...S.col(), background: colors.surface, border: `1px solid ${colors.border}`, borderRadius: layout.borderRadius.lg, padding: '14px 16px', minWidth: '220px' }}>
                        <div style=${{ fontWeight: typography.weight.semibold, color: sub.color, marginBottom: '10px', fontSize: '14px' }}>${sub.name}</div>
                        ${sub.items.map(item => html`
                            <div style=${{ fontFamily: typography.fontMono, fontSize: '11px', color: colors.textSecondary, lineHeight: 1.7 }}>${item}</div>
                        `)}
                    </div>
                `)}
            </div>

            <!-- Key design decisions -->
            <div style=${S.card}>
                <h3 style=${S.h3}>Key Design Decisions</h3>
                ${[
                    ['Raw ASGI — no framework', 'Manual path matching in app.py. No FastAPI, Flask, Django. Routes are if/elif chains.'],
                    ['SQLite + aiosqlite', 'Single-file database, singleton connection, WAL mode. No ORM. Fernet-encrypted credentials.'],
                    ['CDN-loaded Preact dashboard', 'No build step, no node_modules. htm + Preact from esm.sh. Hash-based routing.'],
                    ['Localhost bypass for workers', '127.0.0.1/::1 skip all auth. CC workers access /mcp/worker without tokens.'],
                    ['Isolated git worktrees', 'Each task gets a bare-clone worktree. Credential helper written inline. Workers commit/push their own branch.'],
                ].map(([title, desc]) => html`
                    <div style=${{ padding: '10px 0', borderBottom: `1px solid ${colors.borderSubtle}` }}>
                        <span style=${{ fontWeight: typography.weight.semibold, color: colors.text, fontSize: '13px' }}>${title}</span>
                        <span style=${{ color: colors.textSecondary, fontSize: '13px', marginLeft: '12px' }}>${desc}</span>
                    </div>
                `)}
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Tab 2: Task Lifecycle
// ---------------------------------------------------------------------------

function TabTaskLifecycle() {
    const [expanded, setExpanded] = useState(null);

    const steps = [
        {
            name: 'dispatch_task()',
            file: 'dispatch/engine.py:434',
            status: 'entry',
            detail: 'Validates task ID, resolves config inheritance (task → project → defaults), creates DB record. Checks held flag, depends_on chain, concurrency limit. Then proceeds to worktree setup.',
        },
        {
            name: 'Held check',
            file: 'dispatch/engine.py',
            status: 'gate',
            detail: 'If task.held=1, status is set to "ready" but not dispatched. Stephen must manually release via resume_task or release_worktree.',
        },
        {
            name: 'depends_on check',
            file: 'dispatch/engine.py',
            status: 'gate',
            detail: 'If depends_on task is not in gate_passed/completed, this task is deferred. It will be auto-dispatched when its dependency passes its gate.',
        },
        {
            name: 'Concurrency check',
            file: 'dispatch/queue.py',
            status: 'gate',
            detail: 'FIFO queue drain: if max concurrent workers reached, task is queued. Queue is drained when a running task finishes.',
        },
        {
            name: 'setup_worktree()',
            file: 'git/worktree.py:60',
            status: 'action',
            detail: 'If new project: git clone --bare with PAT auth, then strips PAT from remote.origin.url. Per task: git worktree add, writes credential helper bash script, configures refspecs.',
        },
        {
            name: 'Build prompt',
            file: 'dispatch/sdk_session.py',
            status: 'action',
            detail: 'Constructs the CC session prompt: task goal, checklist, CLAUDE.md content, any prior failure output (retry feedback), review feedback. Full context.',
        },
        {
            name: '_run_sdk_session()',
            file: 'dispatch/sdk_session.py',
            status: 'action',
            detail: 'Launches Claude Code via claude_agent_sdk. Streams output, tracks tokens/cost. anyio monkey-patch (line 53-75) is a CRITICAL safety measure to prevent CC from killing the server process.',
        },
        {
            name: 'CC Worker runs',
            file: 'worktree (isolated)',
            status: 'external',
            detail: 'CC worker has full access to the worktree. Should commit and push to its branch. Can call /mcp/worker (localhost bypass). May post questions to switchboard.',
        },
        {
            name: 'Completion',
            file: 'dispatch/engine.py',
            status: 'action',
            detail: 'SDK session ends. Status transitions to completed/failed/needs-review/turns-exhausted depending on exit. Gate pipeline begins if auto_test=1.',
        },
        {
            name: 'Test gate',
            file: 'dispatch/gates.py:242',
            status: 'gate',
            detail: 'Runs project.test_command in the worktree. Streams output to DB. Pass → continue. Fail → retry up to max_test_retries with failure output injected. Max retries hit → needs-review.',
        },
        {
            name: 'Review gate',
            file: 'dispatch/gates.py:345',
            status: 'gate',
            detail: 'Dispatches a review subtask (auto_review model, usually opus). Reviewer gets diff + task spec. APPROVED → continue. CHANGES REQUESTED → retry with feedback. Max retries → needs-review.',
        },
        {
            name: 'Gate pass → PR',
            file: 'dispatch/gates.py + git/operations.py',
            status: 'action',
            detail: 'auto_merge: attempts rebase onto base branch, then merges. auto_pr: creates GitHub PR via gh CLI (never call gh yourself — auto_pr handles it). Records PR URL in task.',
        },
        {
            name: 'Release worktree',
            file: 'git/worktree.py',
            status: 'action',
            detail: 'If auto_release_worktree=1, the worktree is removed after gate pass. Frees disk space. The branch remains on the remote.',
        },
        {
            name: 'Dispatch dependents',
            file: 'dispatch/gates.py:_check_and_dispatch_dependents',
            status: 'action',
            detail: 'After gate pass, any tasks with depends_on pointing to this task are automatically dispatched (if not held).',
        },
    ];

    const statusColors = {
        entry: { bg: colors.accentBg, color: colors.accent },
        gate: { bg: colors.yellowBg, color: colors.yellow },
        action: { bg: colors.blueBg, color: colors.blue },
        external: { bg: colors.greenBg, color: colors.green },
    };

    return html`
        <div>
            <h2 style=${S.h2}>Task Lifecycle</h2>
            <div style=${{ display: 'flex', gap: '12px', marginBottom: '20px', flexWrap: 'wrap' }}>
                ${Object.entries({ entry: 'Entry point', gate: 'Decision gate', action: 'Action step', external: 'External (CC worker)' }).map(([k, v]) => html`
                    <span style=${{ ...S.tag(statusColors[k].color, statusColors[k].bg) }}>${v}</span>
                `)}
            </div>

            <div style=${{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                ${steps.map((step, i) => html`
                    <div
                        key=${i}
                        style=${{
                            background: expanded === i ? colors.surfaceActive : colors.surface,
                            border: `1px solid ${expanded === i ? colors.accent : colors.border}`,
                            borderRadius: layout.borderRadius.md,
                            cursor: 'pointer',
                            transition: 'all 0.15s ease',
                        }}
                        onClick=${() => setExpanded(expanded === i ? null : i)}
                    >
                        <div style=${{ display: 'flex', alignItems: 'center', gap: '12px', padding: '10px 14px' }}>
                            <span style=${{
                                width: '22px',
                                height: '22px',
                                borderRadius: '50%',
                                background: statusColors[step.status].bg,
                                color: statusColors[step.status].color,
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                                fontSize: '11px',
                                fontWeight: typography.weight.bold,
                                flexShrink: 0,
                            }}>${i + 1}</span>
                            <span style=${{ fontWeight: typography.weight.semibold, color: colors.text, fontSize: '13px', flex: 1 }}>${step.name}</span>
                            <span style=${{ fontFamily: typography.fontMono, fontSize: '11px', color: colors.textTertiary }}>${step.file}</span>
                            <span style=${{ color: colors.textTertiary, fontSize: '12px' }}>${expanded === i ? '▲' : '▼'}</span>
                        </div>
                        ${expanded === i ? html`
                            <div style=${{ padding: '0 14px 14px 48px', color: colors.textSecondary, fontSize: '13px', lineHeight: 1.6 }}>
                                ${step.detail}
                            </div>
                        ` : null}
                    </div>
                `)}
            </div>

            <!-- State machine -->
            <div style=${{ ...S.card, marginTop: '24px' }}>
                <h3 style=${S.h3}>Task State Machine</h3>
                <div style=${{ display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }}>
                    ${['ready', '→ working', '→ completed', '→ failed', '→ needs-review', '→ turns-exhausted', '→ rate-limited', '→ cancelled', '→ merged'].map(s => html`
                        <span style=${{
                            fontFamily: typography.fontMono,
                            fontSize: '12px',
                            color: s.startsWith('→') ? colors.textTertiary : colors.accent,
                            padding: s.startsWith('→') ? '0' : '3px 10px',
                            background: s.startsWith('→') ? 'none' : colors.accentBg,
                            borderRadius: layout.borderRadius.pill,
                        }}>${s}</span>
                    `)}
                </div>
                <div style=${{ marginTop: '12px', color: colors.textSecondary, fontSize: '13px' }}>
                    Recovery transitions: <span style=${{ fontFamily: typography.fontMono, color: colors.yellow }}>working → ready</span> (orphan detected),
                    <span style=${{ fontFamily: typography.fontMono, color: colors.yellow }}> rate-limited → retry</span> (after cooldown),
                    <span style=${{ fontFamily: typography.fontMono, color: colors.blue }}> needs-review → reopened</span> (Stephen approves)
                </div>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Tab 3: Gate Pipeline
// ---------------------------------------------------------------------------

function TabGatePipeline() {
    return html`
        <div>
            <h2 style=${S.h2}>Gate Pipeline</h2>
            <p style=${{ color: colors.textSecondary, marginBottom: '24px', lineHeight: 1.6 }}>
                After a CC worker session ends, the gate pipeline runs automatically if <code style=${{ fontFamily: typography.fontMono, color: colors.accent }}>auto_test=1</code>.
                Two stages: test gate then review gate. Each stage has retry logic.
            </p>

            <!-- Flow diagram -->
            <div style=${{ ...S.card, marginBottom: '24px' }}>
                <h3 style=${{ ...S.h3, marginBottom: '16px' }}>Pipeline Flow</h3>
                <div style=${{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    ${[
                        { label: 'CC session completes', color: colors.blue },
                        { label: '↓ if auto_test=1', color: colors.textTertiary, small: true },
                        { label: 'Test Gate: run test_command in worktree', color: colors.yellow },
                        { label: '↙ PASS                        FAIL ↘', color: colors.textTertiary, small: true },
                        { label: 'Continue to review gate      Retry (inject failure output) → max_test_retries → needs-review', color: colors.textSecondary, small: true },
                        { label: '↓ if auto_review=1', color: colors.textTertiary, small: true },
                        { label: 'Review Gate: dispatch subtask with review prompt + diff', color: colors.yellow },
                        { label: '↙ APPROVED                    CHANGES REQUESTED ↘', color: colors.textTertiary, small: true },
                        { label: 'Gate passed                  Retry (inject review feedback) → max_review_retries → needs-review', color: colors.textSecondary, small: true },
                        { label: '↓ gate_passed_at recorded', color: colors.textTertiary, small: true },
                        { label: 'auto_merge + auto_pr + auto_release_worktree + dispatch dependents', color: colors.green },
                    ].map(({ label, color, small }) => html`
                        <div style=${{ color, fontFamily: small ? typography.fontBody : typography.fontBody, fontSize: small ? '12px' : '13px', padding: small ? '0 8px' : '8px 14px', background: small ? 'none' : colors.surfaceActive, borderRadius: small ? 0 : layout.borderRadius.sm }}>${label}</div>
                    `)}
                </div>
            </div>

            <!-- Test gate detail -->
            <div style=${S.card}>
                <h3 style=${S.h3}>Test Gate — <span style=${{ fontFamily: typography.fontMono, fontSize: '13px' }}>gates.py:242 _run_test_gate()</span></h3>
                ${[
                    ['Command', 'project.test_command, run inside the worktree directory'],
                    ['Output streaming', 'Captured line-by-line, written to task.last_test_output'],
                    ['Pass condition', 'Exit code 0'],
                    ['Fail → retry', 'Failure output injected as feedback into next CC session prompt'],
                    ['Max retries', 'max_test_retries (default 3, project/task override). Hit → needs-review'],
                    ['Retry count tracked', 'gate_retries field on task record'],
                ].map(([k, v]) => html`
                    <div style=${{ display: 'flex', gap: '12px', padding: '8px 0', borderBottom: `1px solid ${colors.borderSubtle}` }}>
                        <span style=${{ fontWeight: typography.weight.semibold, color: colors.textSecondary, fontSize: '13px', width: '160px', flexShrink: 0 }}>${k}</span>
                        <span style=${{ color: colors.text, fontSize: '13px', lineHeight: 1.5 }}>${v}</span>
                    </div>
                `)}
            </div>

            <!-- Review gate detail -->
            <div style=${{ ...S.card, marginTop: '12px' }}>
                <h3 style=${S.h3}>Review Gate — <span style=${{ fontFamily: typography.fontMono, fontSize: '13px' }}>gates.py:345 _dispatch_review()</span></h3>
                ${[
                    ['Model', 'review_model (default: opus, inheritable)'],
                    ['Prompt contents', 'Task spec + goal, full git diff, success criteria, review instructions from constants.py'],
                    ['Pass condition', 'CC session output contains "APPROVED"'],
                    ['Fail condition', '"CHANGES REQUESTED" + feedback text'],
                    ['Feedback injection', 'Review feedback injected into next CC session prompt for the original task'],
                    ['Max retries', 'max_review_retries (default 2). Hit → needs-review'],
                    ['Subtask', 'Review runs as a child task (parent_task_id set). Not dispatched to a worktree.'],
                ].map(([k, v]) => html`
                    <div style=${{ display: 'flex', gap: '12px', padding: '8px 0', borderBottom: `1px solid ${colors.borderSubtle}` }}>
                        <span style=${{ fontWeight: typography.weight.semibold, color: colors.textSecondary, fontSize: '13px', width: '160px', flexShrink: 0 }}>${k}</span>
                        <span style=${{ color: colors.text, fontSize: '13px', lineHeight: 1.5 }}>${v}</span>
                    </div>
                `)}
            </div>

            <!-- Post-gate actions -->
            <div style=${{ ...S.card, marginTop: '12px' }}>
                <h3 style=${S.h3}>Post-Gate Actions — <span style=${{ fontFamily: typography.fontMono, fontSize: '13px' }}>gates.py:_check_and_dispatch_dependents()</span></h3>
                ${[
                    ['auto_merge=1', 'Rebase task branch onto base branch, then merge. git/operations.py.'],
                    ['auto_pr=1', 'Creates GitHub PR via configured credentials. Records PR URL on task. NEVER call gh CLI yourself.'],
                    ['auto_release_worktree=1', 'Removes the worktree directory after gate pass. Branch stays on remote.'],
                    ['Dependent dispatch', 'Any task with depends_on=this_task_id is auto-dispatched (if not held).'],
                ].map(([k, v]) => html`
                    <div style=${{ display: 'flex', gap: '12px', padding: '8px 0', borderBottom: `1px solid ${colors.borderSubtle}` }}>
                        <span style=${{ fontFamily: typography.fontMono, color: colors.accent, fontSize: '12px', width: '200px', flexShrink: 0 }}>${k}</span>
                        <span style=${{ color: colors.text, fontSize: '13px', lineHeight: 1.5 }}>${v}</span>
                    </div>
                `)}
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Tab 4: Config Inheritance
// ---------------------------------------------------------------------------

function TabConfigInheritance() {
    const fields = [
        { name: 'model', default: '"sonnet"', desc: 'Claude model slug for task CC sessions' },
        { name: 'max_turns', default: '200', desc: 'Max CC turns before turns-exhausted' },
        { name: 'max_wall_clock', default: '60 (min)', desc: 'Max wall-clock minutes for a session' },
        { name: 'auto_test', default: '1', desc: 'Run test gate after session completes' },
        { name: 'auto_review', default: '1', desc: 'Run review gate after test gate passes' },
        { name: 'auto_pr', default: '0', desc: 'Create GitHub PR after gate pass' },
        { name: 'auto_merge', default: '0', desc: 'Rebase + merge after gate pass' },
        { name: 'auto_release_worktree', default: '1', desc: 'Remove worktree after gate pass' },
        { name: 'review_model', default: '"opus"', desc: 'Claude model for review gate sessions' },
        { name: 'max_test_retries', default: '3', desc: 'Max test gate retry attempts' },
        { name: 'max_review_retries', default: '2', desc: 'Max review gate retry attempts' },
    ];

    return html`
        <div>
            <h2 style=${S.h2}>Config Inheritance</h2>
            <p style=${{ color: colors.textSecondary, marginBottom: '24px', lineHeight: 1.6 }}>
                Three-tier inheritance: task → component → project → system defaults.
                The resolution function is <code style=${{ fontFamily: typography.fontMono, color: colors.accent }}>_resolve_limit(task_val, project_val, global_default)</code>
                in <code style=${{ fontFamily: typography.fontMono, color: colors.accent }}>dispatch/engine.py:71</code>.
            </p>

            <!-- Tier diagram -->
            <div style=${{ ...S.card, marginBottom: '24px' }}>
                <h3 style=${S.h3}>Resolution Order (first non-None wins)</h3>
                <div style=${{ display: 'flex', gap: '12px', alignItems: 'center', padding: '12px 0', flexWrap: 'wrap' }}>
                    ${[
                        { label: 'Task override', sub: 'tasks table column', color: colors.accent, bg: colors.accentBg },
                        { label: '→', sub: '', color: colors.textTertiary, bg: 'transparent' },
                        { label: 'Component config', sub: 'components.config JSON', color: colors.blue, bg: colors.blueBg },
                        { label: '→', sub: '', color: colors.textTertiary, bg: 'transparent' },
                        { label: 'Project config', sub: 'projects table column', color: colors.green, bg: colors.greenBg },
                        { label: '→', sub: '', color: colors.textTertiary, bg: 'transparent' },
                        { label: 'System default', sub: 'constants.py / hardcoded', color: colors.textSecondary, bg: colors.surfaceActive },
                    ].map(({ label, sub, color, bg }) => html`
                        <div style=${{ textAlign: 'center' }}>
                            <div style=${{ background: bg, border: bg !== 'transparent' ? `1px solid ${color}30` : 'none', borderRadius: layout.borderRadius.md, padding: '8px 14px', color, fontSize: '13px', fontWeight: typography.weight.semibold }}>${label}</div>
                            ${sub ? html`<div style=${{ fontSize: '11px', color: colors.textTertiary, marginTop: '4px' }}>${sub}</div>` : null}
                        </div>
                    `)}
                </div>
            </div>

            <!-- Fields table -->
            <div style=${{ ...S.card }}>
                <h3 style=${{ ...S.h3, marginBottom: '0' }}>Inheritable Fields</h3>
                <table style=${{ width: '100%', borderCollapse: 'collapse', marginTop: '12px' }}>
                    <thead>
                        <tr>
                            <th style=${{ ...S.tableHeader, textAlign: 'left' }}>Field</th>
                            <th style=${{ ...S.tableHeader, textAlign: 'left' }}>System Default</th>
                            <th style=${{ ...S.tableHeader, textAlign: 'left' }}>Description</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${fields.map(f => html`
                            <tr>
                                <td style=${S.tableCellMono}>${f.name}</td>
                                <td style=${{ ...S.tableCell, fontFamily: typography.fontMono, fontSize: '12px', color: colors.yellow }}>${f.default}</td>
                                <td style=${S.tableCell}>${f.desc}</td>
                            </tr>
                        `)}
                    </tbody>
                </table>
            </div>

            <!-- Special notes -->
            <div style=${{ ...S.card, marginTop: '12px', borderColor: colors.yellow + '40', background: colors.yellowBg }}>
                <h3 style=${{ ...S.h3, color: colors.yellow }}>Important Notes</h3>
                <ul style=${{ color: colors.textSecondary, fontSize: '13px', lineHeight: 1.8, paddingLeft: '20px', margin: 0 }}>
                    <li>Components have <strong style=${{ color: colors.text }}>zero config at creation</strong> — all fields null. They inherit from project.</li>
                    <li>Tasks are <strong style=${{ color: colors.text }}>held by default</strong> (held=1) unless explicitly released or dispatched with held=0.</li>
                    <li>auto_pr=1 means Switchboard calls gh CLI — <strong style=${{ color: colors.red }}>never call gh CLI yourself</strong>.</li>
                    <li>review_model defaults to "opus" regardless of the task model — reviews need the smarter model.</li>
                </ul>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Tab 5: Auth & Security
// ---------------------------------------------------------------------------

function TabAuthSecurity() {
    return html`
        <div>
            <h2 style=${S.h2}>Auth & Security</h2>
            <p style=${{ color: colors.textSecondary, marginBottom: '24px', lineHeight: 1.6 }}>
                Two-layer auth, always active. Plus localhost bypass for CC workers.
                Both layers are checked in <code style=${{ fontFamily: typography.fontMono, color: colors.accent }}>auth/middleware.py</code>.
            </p>

            <!-- Three columns -->
            <div style=${S.row}>
                ${[
                    {
                        title: 'Layer 1: Session Auth',
                        color: colors.blue,
                        subtitle: 'Dashboard (Foreman UI)',
                        file: 'auth/sessions.py',
                        items: [
                            'Cookie: switchboard_session',
                            'TTL: 7 days, 24h inactivity timeout',
                            'Passwords: Argon2id hashing',
                            'Rate limiting: 5 fails → 15min lockout',
                            'Protected paths: /foreman/*, /dashboard/api/*',
                            'Login: POST /auth/login',
                            'Logout: POST /auth/logout',
                        ],
                    },
                    {
                        title: 'Layer 2: Bearer JWT',
                        color: colors.accent,
                        subtitle: 'MCP Client (Claude.ai)',
                        file: 'auth/middleware.py + oauth.py',
                        items: [
                            'Bearer token in Authorization header',
                            'RS256 JWTs, 1-hour access token TTL',
                            'jti for revocation support',
                            '30-day refresh tokens with rotation',
                            'PKCE S256 support',
                            'OAuth 2.0 server built-in (authlib)',
                            'Protected path: /mcp',
                        ],
                    },
                    {
                        title: 'Localhost Bypass',
                        color: colors.green,
                        subtitle: 'CC Workers',
                        file: 'auth/middleware.py',
                        items: [
                            'Source: 127.0.0.1 or ::1',
                            'Skips ALL auth validation',
                            'Worker path: /mcp/worker',
                            'No token required',
                            'user_id set from X-Worker-User header',
                            'is_worker=True in request context',
                            'DO NOT change without understanding full flow',
                        ],
                    },
                ].map(col => html`
                    <div style=${{ ...S.col(), background: colors.surface, border: `1px solid ${col.color}40`, borderRadius: layout.borderRadius.lg, padding: '16px' }}>
                        <div style=${{ fontWeight: typography.weight.semibold, color: col.color, fontSize: '14px', marginBottom: '4px' }}>${col.title}</div>
                        <div style=${{ fontSize: '12px', color: colors.textTertiary, marginBottom: '4px' }}>${col.subtitle}</div>
                        <div style=${{ fontFamily: typography.fontMono, fontSize: '11px', color: colors.textTertiary, marginBottom: '12px' }}>${col.file}</div>
                        ${col.items.map(item => html`
                            <div style=${{ fontSize: '12px', color: colors.textSecondary, lineHeight: 1.8, paddingLeft: '8px', borderLeft: `2px solid ${col.color}30` }}>
                                ${item}
                            </div>
                        `)}
                    </div>
                `)}
            </div>

            <!-- Request context vars -->
            <div style=${{ ...S.card, marginTop: '12px' }}>
                <h3 style=${S.h3}>Request Context Variables — <span style=${{ fontFamily: typography.fontMono, fontSize: '13px' }}>server/context.py</span></h3>
                <p style=${{ color: colors.textSecondary, fontSize: '13px', marginBottom: '12px' }}>Three asyncio context vars set per-request in app.py, accessible via getters:</p>
                ${[
                    ['user_id', 'get_request_user_id()', 'int or None — authenticated user'],
                    ['is_token_auth', 'get_is_token_auth()', 'bool — True if Bearer JWT was used'],
                    ['is_worker', 'get_is_worker()', 'bool — True if localhost bypass triggered'],
                ].map(([var_, getter, desc]) => html`
                    <div style=${{ display: 'flex', gap: '16px', padding: '8px 0', borderBottom: `1px solid ${colors.borderSubtle}`, alignItems: 'center' }}>
                        <span style=${{ fontFamily: typography.fontMono, color: colors.accent, fontSize: '12px', width: '120px' }}>${var_}</span>
                        <span style=${{ fontFamily: typography.fontMono, color: colors.blue, fontSize: '12px', width: '200px' }}>${getter}</span>
                        <span style=${{ color: colors.textSecondary, fontSize: '13px' }}>${desc}</span>
                    </div>
                `)}
            </div>

            <!-- Unprotected paths -->
            <div style=${{ ...S.card, marginTop: '12px' }}>
                <h3 style=${S.h3}>Unprotected Paths</h3>
                <div style=${{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                    ${['/health', '/.well-known/*', '/oauth/*', '/auth/*', '/foreman/login'].map(p => html`
                        <span style=${S.tag(colors.green, colors.greenBg)}>${p}</span>
                    `)}
                </div>
            </div>

            <!-- Credential storage -->
            <div style=${{ ...S.card, marginTop: '12px' }}>
                <h3 style=${S.h3}>Credential Storage</h3>
                <p style=${{ color: colors.textSecondary, fontSize: '13px', lineHeight: 1.6 }}>
                    API keys (Anthropic, GitHub PAT, Slack bot token) are encrypted with <strong style=${{ color: colors.text }}>Fernet symmetric encryption</strong>
                    before storage in the <code style=${{ fontFamily: typography.fontMono, color: colors.accent }}>user_credentials</code> table.
                    Key: <code style=${{ fontFamily: typography.fontMono, color: colors.accent }}>SWITCHBOARD_MASTER_KEY</code> env var.
                    The GitHub PAT is also stripped from the bare repo's remote.origin.url after clone (worktree.py).
                </p>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Tab 6: Data Model
// ---------------------------------------------------------------------------

function TabDataModel() {
    const tables = [
        { name: 'users', pk: 'id', cols: 'email (unique), password_hash, role, timezone, lockout tracking', usedBy: 'auth, all FK references' },
        { name: 'user_credentials', pk: 'id', cols: 'user_id FK, service (anthropic/github/slack), encrypted_value', usedBy: 'engine.py, worktree.py' },
        { name: 'sessions', pk: 'id', cols: 'user_id FK, token_hash, expires_at, last_active', usedBy: 'auth/sessions.py' },
        { name: 'oauth_clients', pk: 'client_id', cols: 'client_secret, name, redirect_uris, scopes', usedBy: 'auth/oauth.py' },
        { name: 'oauth_authorization_codes', pk: 'code', cols: 'client_id, user_id, scope, expires_at, code_challenge', usedBy: 'auth/oauth.py' },
        { name: 'oauth_tokens', pk: 'jti', cols: 'access_token, refresh_token, user_id, expires_at, revoked_at', usedBy: 'auth/oauth.py, middleware.py' },
        { name: 'projects', pk: 'id (slug)', cols: 'repo_url, test_command, model, auto_*, max_*, env_overrides JSON', usedBy: 'engine.py, worktree.py' },
        { name: 'components', pk: 'id (slug)', cols: 'project_id FK, name, config JSON, paused, punchlist_mode', usedBy: 'engine.py, punchlist' },
        { name: 'tasks', pk: 'id (slug)', cols: 'project_id, component_id, status, phase, branch, worktree_path, gate_status, gate_retries, depends_on, held, auto_*, model, max_turns', usedBy: 'engine.py, gates.py, recovery.py' },
        { name: 'task_checklist', pk: 'id', cols: 'task_id FK, item text, done bool', usedBy: 'handlers/tasks.py' },
        { name: 'conversations', pk: 'id (slug)', cols: 'project_id FK, goal, archived', usedBy: 'handlers/conversations.py' },
        { name: 'messages', pk: 'id', cols: 'conversation_id XOR task_id, author, type, content, pinned, attempt_number', usedBy: 'handlers/conversations.py, engine.py' },
        { name: 'message_chunks', pk: 'id', cols: 'message_id FK, chunk_index, content, embedding BLOB', usedBy: 'embeddings/service.py, db/search.py' },
        { name: 'punchlist', pk: 'id', cols: 'component_id FK, item, status (open/claimed/resolved), claimed_by_task_id', usedBy: 'handlers/punchlist.py' },
        { name: 'instance', pk: '(single row)', cols: 'plan_tier, owner_user_id, feature flags', usedBy: 'app.py, dashboard/api.py' },
        { name: 'push_subscriptions', pk: 'id', cols: 'user_id FK, endpoint, p256dh, auth', usedBy: 'notifications/web_push.py' },
    ];

    return html`
        <div>
            <h2 style=${S.h2}>Data Model</h2>
            <p style=${{ color: colors.textSecondary, marginBottom: '16px', lineHeight: 1.6 }}>
                SQLite, single file, async via aiosqlite. WAL journal mode. Foreign keys enforced.
                Singleton connection in <code style=${{ fontFamily: typography.fontMono, color: colors.accent }}>db/connection.py</code>.
                All timestamps ISO format via <code style=${{ fontFamily: typography.fontMono, color: colors.accent }}>now_iso()</code>.
            </p>

            <!-- Special notes -->
            <div style=${{ ...S.card, marginBottom: '16px', borderColor: colors.blue + '40', background: colors.blueBg }}>
                <strong style=${{ color: colors.blue, fontSize: '13px' }}>Key Design Note:</strong>
                <span style=${{ color: colors.textSecondary, fontSize: '13px', marginLeft: '8px' }}>
                    The <code style=${{ fontFamily: typography.fontMono }}>messages</code> table is dual-purpose:
                    <code style=${{ fontFamily: typography.fontMono }}>conversation_id</code> XOR <code style=${{ fontFamily: typography.fontMono }}>task_id</code> — one is always NULL.
                    Conversations use <code style=${{ fontFamily: typography.fontMono }}>conversation_id</code>; task logs use <code style=${{ fontFamily: typography.fontMono }}>task_id</code>.
                </span>
            </div>

            <div style=${S.card}>
                <table style=${{ width: '100%', borderCollapse: 'collapse' }}>
                    <thead>
                        <tr>
                            <th style=${{ ...S.tableHeader, textAlign: 'left', width: '180px' }}>Table</th>
                            <th style=${{ ...S.tableHeader, textAlign: 'left', width: '80px' }}>PK</th>
                            <th style=${{ ...S.tableHeader, textAlign: 'left' }}>Key Columns</th>
                            <th style=${{ ...S.tableHeader, textAlign: 'left', width: '180px' }}>Used By</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${tables.map(t => html`
                            <tr>
                                <td style=${S.tableCellMono}>${t.name}</td>
                                <td style=${{ ...S.tableCell, fontFamily: typography.fontMono, fontSize: '11px', color: colors.textTertiary }}>${t.pk}</td>
                                <td style=${{ ...S.tableCell, fontSize: '12px', color: colors.textSecondary }}>${t.cols}</td>
                                <td style=${{ ...S.tableCell, fontSize: '12px', color: colors.textTertiary }}>${t.usedBy}</td>
                            </tr>
                        `)}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Tab 7: File Map
// ---------------------------------------------------------------------------

function TabFileMap() {
    const sections = [
        {
            title: 'switchboard/server/',
            color: colors.accent,
            files: [
                ['app.py', 'Raw ASGI entry. Route matching (if/elif). create_app() factory. Mounts StreamableHTTPSessionManager.'],
                ['tools.py', '70+ MCP tool schema definitions. Pure JSON Schema. No logic. ~1095 lines.'],
                ['dispatch.py', 'TOOL_HANDLERS dict mapping tool name → async handler function.'],
                ['context.py', 'Three asyncio context vars: user_id, is_token_auth, is_worker.'],
                ['handlers/conversations.py', 'MCP handlers: create/read/list/search conversations and messages.'],
                ['handlers/projects.py', 'MCP handlers: create/update/get/list projects.'],
                ['handlers/tasks.py', 'MCP handlers: dispatch, resume, retry, cancel, approve, close, checklist ops.'],
                ['handlers/components.py', 'MCP handlers: component CRUD, pause/resume/stop.'],
                ['handlers/punchlist.py', 'MCP handlers: punchlist item lifecycle (add/claim/resolve).'],
                ['handlers/ops.py', 'MCP handlers: board, get_context, get_guide.'],
                ['handlers/tokens.py', 'MCP handlers: API token create/list/revoke.'],
                ['handlers/common.py', 'Shared handler utilities.'],
            ],
        },
        {
            title: 'switchboard/dispatch/',
            color: colors.blue,
            files: [
                ['engine.py', 'Core task lifecycle: dispatch_task (434), resume_task (719), retry_task (767), reopen_task (862), cancel_task (1015), approve_task (1110), close_task (1144).'],
                ['gates.py', 'Gate pipeline: _run_test_gate (242), _dispatch_review (345), _check_and_dispatch_dependents.'],
                ['sdk_session.py', 'CC SDK bridge. Builds prompt. Runs claude_agent_sdk. anyio monkey-patch (53-75) = CRITICAL.'],
                ['queue.py', 'FIFO concurrency queue. Drain on task completion.'],
                ['recovery.py', 'recover_orphaned_tasks (144), check_stalled_tasks (420), mark_working_for_recovery (43).'],
                ['_state.py', 'Shared mutable state: running tasks dict, active clients set.'],
            ],
        },
        {
            title: 'switchboard/db/',
            color: '#f09f56',
            files: [
                ['connection.py', 'Singleton aiosqlite connection. WAL mode. FK enforcement. get_db() context manager.'],
                ['schema.py', 'CREATE TABLE statements, migrations, all 21 tables.'],
                ['tasks.py', 'Task CRUD, status transitions, checklist operations.'],
                ['conversations.py', 'Conversation + message CRUD, cursor-based pagination.'],
                ['projects.py', 'Project CRUD.'],
                ['components.py', 'Component CRUD.'],
                ['punchlist.py', 'Punchlist item lifecycle.'],
                ['users.py', 'User management, credential encryption/decryption (Fernet).'],
                ['search.py', 'Semantic search: embedding-based similarity queries.'],
                ['push.py', 'Web push subscription management.'],
                ['_helpers.py', 'Shared utils: now_iso(), _read_messages(), aggregate queries.'],
            ],
        },
        {
            title: 'switchboard/auth/',
            color: colors.yellow,
            files: [
                ['middleware.py', 'Two-layer auth check. Sets context vars. Localhost bypass logic.'],
                ['oauth.py', 'Built-in OAuth 2.0 server (authlib). RS256 JWTs, PKCE, refresh token rotation.'],
                ['sessions.py', 'Cookie auth. Login/logout. Argon2id passwords. Rate limiting (5 fails → 15min lockout).'],
            ],
        },
        {
            title: 'switchboard/git/',
            color: colors.green,
            files: [
                ['worktree.py', 'Bare clone + per-task worktrees. Credential helper (inline bash script). Strip PAT after clone.'],
                ['operations.py', 'Branch ops, rebase, push, diff, merge, PR creation.'],
                ['files.py', 'File operation utilities.'],
            ],
        },
        {
            title: 'switchboard/notifications/',
            color: colors.textSecondary,
            files: [
                ['slack.py', 'Per-task Slack threads. Rich block format. SLACK_BOT_TOKEN env var.'],
                ['web_push.py', 'VAPID-signed browser push notifications. VAPID_PRIVATE_KEY + VAPID_PUBLIC_KEY.'],
            ],
        },
        {
            title: 'switchboard/embeddings/',
            color: colors.textSecondary,
            files: [
                ['service.py', 'OpenAI text-embedding-3-small embeddings. Cosine similarity search.'],
                ['chunks.py', 'Message chunking for semantic search. Splits long messages.'],
            ],
        },
        {
            title: 'dashboard/',
            color: colors.accent,
            files: [
                ['foreman.html', 'Entry point HTML. Loads DM Sans font. Mounts #foreman-root.'],
                ['foreman-app.js', 'Preact app root. useRouter() → renders view components.'],
                ['foreman-shell.js', 'ForemanShell, ForemanHeader, ForemanPage layout components.'],
                ['router.js', 'Hash-based router. parseRoute(), useRouter(), navigate(), routes helper.'],
                ['tokens.js', 'Design tokens: colors, typography, spacing, layout, animation.'],
                ['api.js', 'REST API client wrapper for /dashboard/api/*.'],
                ['views/LandingView.js', 'Project grid with attention model and health indicators.'],
                ['views/ProjectView.js', 'Project detail: tasks, components, conversations, activity.'],
                ['views/TaskView.js', 'Task detail: status, checklist, logs, chain, files.'],
                ['views/ConversationView.js', 'Conversation thread with pinned spec display.'],
                ['views/TaskCreateView.js', 'Task creation form.'],
                ['views/ProjectCreateView.js', 'Project creation form.'],
                ['components/Settings.js', 'Instance + user settings UI.'],
                ['components/Files.js', 'File browser UI.'],
                ['docs/architecture.jsx', 'This file. Interactive architecture reference.'],
            ],
        },
        {
            title: 'tests/',
            color: colors.textTertiary,
            files: [
                ['conftest.py', 'Test fixtures: tmp_db, db, sample_project, sample_task, sample_conversation, mock_git, mock_sdk.'],
                ['test_unit.py', 'Unit tests: functions in isolation, mocked DB/git.'],
                ['test_integration.py', 'Integration tests: real SQLite, real git, no CC sessions.'],
                ['test_*.py', '876+ tests total, all async (pytest-asyncio, asyncio_mode=auto).'],
            ],
        },
    ];

    return html`
        <div>
            <h2 style=${S.h2}>File Map</h2>
            <p style=${{ color: colors.textSecondary, marginBottom: '24px', lineHeight: 1.6 }}>
                All business logic is in the <code style=${{ fontFamily: typography.fontMono, color: colors.accent }}>switchboard/</code> package.
                No root-level Python shims. Dashboard is CDN-loaded ES modules in <code style=${{ fontFamily: typography.fontMono, color: colors.accent }}>dashboard/</code>.
            </p>

            ${sections.map(section => html`
                <div style=${{ ...S.card, marginBottom: '12px' }}>
                    <h3 style=${{ fontFamily: typography.fontMono, fontSize: '14px', fontWeight: typography.weight.semibold, color: section.color, marginBottom: '12px' }}>${section.title}</h3>
                    ${section.files.map(([name, desc]) => html`
                        <div style=${S.fileEntry}>
                            <span style=${S.fileName}>${name}</span>
                            <span style=${S.fileDesc}>${desc}</span>
                        </div>
                    `)}
                </div>
            `)}
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Tab 8: Recovery
// ---------------------------------------------------------------------------

function TabRecovery() {
    return html`
        <div>
            <h2 style=${S.h2}>Recovery & Resilience</h2>
            <p style=${{ color: colors.textSecondary, marginBottom: '24px', lineHeight: 1.6 }}>
                Three recovery layers protect against crashes, signal kills, stalls, and rate limits.
                All recovery logic lives in <code style=${{ fontFamily: typography.fontMono, color: colors.accent }}>dispatch/recovery.py</code>.
            </p>

            <!-- Three layers -->
            <div style=${S.row}>
                ${[
                    {
                        title: 'Layer 1: Graceful Shutdown',
                        fn: 'mark_working_for_recovery()',
                        line: 'recovery.py:43',
                        color: colors.blue,
                        desc: 'On SIGTERM/SIGINT, marks all currently working tasks with recovery_priority so they can be resumed after restart.',
                        items: ['Called during shutdown signal handler', 'Sets recovery_priority on working tasks', 'Tasks remain in "working" status', 'Restart picks them up via startup recovery'],
                    },
                    {
                        title: 'Layer 2: Startup Recovery',
                        fn: 'recover_orphaned_tasks()',
                        line: 'recovery.py:144',
                        color: colors.yellow,
                        desc: 'On server start, sweeps for orphaned tasks: stuck gates, working tasks with no live PID, silently killed workers.',
                        items: ['Runs once at startup', 'Checks all "working" status tasks', 'Verifies PID is still alive', 'Stuck test gate → retry or needs-review', 'Orphaned working → retry with recovery feedback', 'Recovery count incremented, max_retries respected'],
                    },
                    {
                        title: 'Layer 3: Background Monitor',
                        fn: 'check_stalled_tasks()',
                        line: 'recovery.py:420',
                        color: colors.green,
                        desc: 'Background loop runs every 60s. Detects stalls by checking last_activity timestamp. Handles rate-limit retry timing.',
                        items: ['Asyncio loop, every 60 seconds', 'Stall detection: last_activity > wall_clock limit', 'Stalled → mark needs-review with detail', 'rate-limited: checks retry_after timestamp', 'rate-limited + past retry_after → auto-retry', 'Updates last_activity on active tasks'],
                    },
                ].map(layer => html`
                    <div style=${{ ...S.col(), background: colors.surface, border: `1px solid ${layer.color}40`, borderRadius: layout.borderRadius.lg, padding: '16px', minWidth: '240px' }}>
                        <div style=${{ fontWeight: typography.weight.semibold, color: layer.color, fontSize: '14px', marginBottom: '4px' }}>${layer.title}</div>
                        <div style=${{ fontFamily: typography.fontMono, fontSize: '11px', color: colors.textTertiary, marginBottom: '8px' }}>${layer.fn} — ${layer.line}</div>
                        <div style=${{ color: colors.textSecondary, fontSize: '12px', lineHeight: 1.6, marginBottom: '12px' }}>${layer.desc}</div>
                        ${layer.items.map(item => html`
                            <div style=${{ fontSize: '12px', color: colors.textSecondary, lineHeight: 1.8, paddingLeft: '8px', borderLeft: `2px solid ${layer.color}30` }}>${item}</div>
                        `)}
                    </div>
                `)}
            </div>

            <!-- anyio monkey-patch warning -->
            <div style=${{ ...S.card, marginTop: '16px', borderColor: colors.red + '60', background: colors.redBg }}>
                <h3 style=${{ ...S.h3, color: colors.red }}>CRITICAL: anyio Monkey-Patch</h3>
                <p style=${{ color: colors.textSecondary, fontSize: '13px', lineHeight: 1.6, margin: 0 }}>
                    <code style=${{ fontFamily: typography.fontMono, color: colors.accent }}>dispatch/sdk_session.py:53-75</code> patches anyio to prevent CC from sending signals to the host process.
                    Without this, a CC worker could kill the Switchboard server itself. <strong style=${{ color: colors.red }}>Do not remove or bypass this patch.</strong>
                    It is tested and intentional.
                </p>
            </div>

            <!-- Rate limiting -->
            <div style=${{ ...S.card, marginTop: '12px' }}>
                <h3 style=${S.h3}>Rate Limit Handling</h3>
                ${[
                    ['Detection', 'CC SDK returns rate limit error. engine.py catches it.'],
                    ['Status', 'Task set to "rate-limited". retry_after timestamp stored.'],
                    ['Auto-retry', 'Background monitor checks retry_after every 60s. Auto-dispatches when time passes.'],
                    ['No manual action', 'Rate limit recovery is fully automatic — no human intervention needed.'],
                ].map(([k, v]) => html`
                    <div style=${{ display: 'flex', gap: '12px', padding: '8px 0', borderBottom: `1px solid ${colors.borderSubtle}` }}>
                        <span style=${{ fontWeight: typography.weight.semibold, color: colors.textSecondary, fontSize: '13px', width: '140px', flexShrink: 0 }}>${k}</span>
                        <span style=${{ color: colors.text, fontSize: '13px', lineHeight: 1.5 }}>${v}</span>
                    </div>
                `)}
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Tab 9: MCP Tools
// ---------------------------------------------------------------------------

function TabMcpTools() {
    const groups = [
        {
            name: 'Planning & Context',
            color: colors.blue,
            tools: [
                { name: 'get_context', params: '—', desc: 'Compact snapshot: active projects, running tasks, recent events. Call first in every session.' },
                { name: 'get_guide', params: '—', desc: 'Full tool reference. Only call when you need details beyond get_context.' },
                { name: 'board', params: 'project?', desc: 'Dashboard overview of all active conversations.' },
                { name: 'conversations', params: 'search?, project?', desc: 'List/search conversations.' },
                { name: 'search_conversations', params: 'query', desc: 'Semantic search across conversation messages.' },
                { name: 'search_message_chunks', params: 'query', desc: 'Embedding-based chunk search.' },
            ],
        },
        {
            name: 'Conversations & Messages',
            color: colors.accent,
            tools: [
                { name: 'create_conversation', params: 'id, project, goal', desc: 'Start a new conversation thread.' },
                { name: 'post', params: 'conversation_id, author, content', desc: 'Add a message to a conversation.' },
                { name: 'read', params: 'conversation_id, after?', desc: 'Get messages. Use after cursor to avoid reflooding context.' },
                { name: 'get_pinned', params: 'conversation_id', desc: 'Get the source-of-truth pinned message.' },
                { name: 'pin', params: 'message_id', desc: 'Pin a message (auto-unpins previous).' },
                { name: 'archive', params: 'conversation_id', desc: 'Soft-archive a resolved conversation.' },
            ],
        },
        {
            name: 'Dispatching Tasks',
            color: colors.yellow,
            tools: [
                { name: 'dispatch_task', params: 'task_id, project_id, goal, held?', desc: 'Create and dispatch a task. held=1 by default.' },
                { name: 'resume_task', params: 'task_id', desc: 'Resume a held or ready task.' },
                { name: 'retry_task', params: 'task_id', desc: 'Retry a failed or needs-review task.' },
                { name: 'reopen_task', params: 'task_id', desc: 'Reopen a completed task for more work.' },
                { name: 'cancel_task', params: 'task_id', desc: 'Cancel a task (halts if running).' },
                { name: 'approve_task', params: 'task_id', desc: 'Approve a needs-review task (triggers gate pass logic).' },
                { name: 'close_task', params: 'task_id', desc: 'Close a completed task (cleanup).' },
                { name: 'move_task', params: 'task_id, project_id', desc: 'Move a task to a different project.' },
            ],
        },
        {
            name: 'Monitoring',
            color: colors.green,
            tools: [
                { name: 'get_task_status', params: 'task_id', desc: 'Current status, phase, checklist progress, cost, PR status.' },
                { name: 'read_task_messages', params: 'task_id, after?', desc: 'Read task log messages (specs, results, handoffs).' },
                { name: 'post_task_message', params: 'task_id, author, content, type?', desc: 'Post a message to a task log.' },
                { name: 'get_session_log', params: 'task_id, attempt?', desc: 'Full CC session transcript for an attempt.' },
                { name: 'get_dispatch_log', params: 'task_id', desc: 'Dispatch log: worktree setup, gate runs, results.' },
                { name: 'list_attempts', params: 'task_id', desc: 'List all dispatch attempts for a task.' },
                { name: 'get_pipeline', params: 'project_id', desc: 'Full pipeline view: tasks, gates, dependencies.' },
                { name: 'list_tasks', params: 'project_id?, status?', desc: 'List tasks with filtering.' },
                { name: 'list_task_files', params: 'task_id', desc: 'List files attached to a task.' },
                { name: 'get_task_file', params: 'task_id, filename', desc: 'Read a specific task file (screenshots, artifacts).' },
            ],
        },
        {
            name: 'Projects & Components',
            color: '#f09f56',
            tools: [
                { name: 'create_project', params: 'id, repo_url, …', desc: 'Register a new project.' },
                { name: 'update_project', params: 'id, …', desc: 'Update project config (model, auto_*, test_command, etc).' },
                { name: 'get_project', params: 'id', desc: 'Get project details including config.' },
                { name: 'list_projects', params: '—', desc: 'List all registered projects.' },
                { name: 'create_component', params: 'id, project_id, name', desc: 'Create a component (zero config — inherits from project).' },
                { name: 'update_component', params: 'id, …', desc: 'Update component config or metadata.' },
                { name: 'pause_component', params: 'id', desc: 'Pause all task dispatch for a component.' },
                { name: 'stop_component', params: 'id', desc: 'Stop all running tasks in a component.' },
                { name: 'list_punchlist', params: 'component_id', desc: 'List punchlist items for a component.' },
                { name: 'add_punchlist_item', params: 'component_id, item', desc: 'Add a punchlist item.' },
                { name: 'claim_punchlist_item', params: 'item_id, task_id', desc: 'Claim a punchlist item for a task.' },
                { name: 'resolve_punchlist_item', params: 'item_id', desc: 'Mark a punchlist item resolved.' },
            ],
        },
        {
            name: 'Control & Admin',
            color: colors.red,
            tools: [
                { name: 'update_task', params: 'task_id, …', desc: 'Update task fields (goal, model, auto_*, held, depends_on).' },
                { name: 'update_task_phase', params: 'task_id, phase, detail?', desc: 'Update the task phase label (for CC worker progress reporting).' },
                { name: 'update_task_checklist', params: 'item_id, done', desc: 'Mark checklist item done/undone.' },
                { name: 'add_checklist_item', params: 'task_id, item', desc: 'Add a checklist item to a task.' },
                { name: 'bulk_update_tasks', params: 'updates[]', desc: 'Bulk update multiple tasks.' },
                { name: 'release_worktree', params: 'task_id', desc: 'Manually release a task worktree.' },
                { name: 'add_task_file', params: 'task_id, filename, content', desc: 'Attach a file artifact to a task.' },
                { name: 'create_api_token', params: 'name', desc: 'Create a named API token.' },
                { name: 'list_api_tokens', params: '—', desc: 'List API tokens.' },
                { name: 'revoke_api_token', params: 'token_id', desc: 'Revoke an API token.' },
            ],
        },
    ];

    return html`
        <div>
            <h2 style=${S.h2}>MCP Tools Reference</h2>
            <p style=${{ color: colors.textSecondary, marginBottom: '24px', lineHeight: 1.6 }}>
                70+ tools defined in <code style=${{ fontFamily: typography.fontMono, color: colors.accent }}>server/tools.py</code>.
                Grouped by workflow below. Always start with <strong style=${{ color: colors.text }}>get_context</strong>.
            </p>

            ${groups.map(group => html`
                <div style=${{ ...S.card, marginBottom: '12px' }}>
                    <h3 style=${{ ...S.h3, color: group.color }}>${group.name}</h3>
                    <table style=${{ width: '100%', borderCollapse: 'collapse' }}>
                        <thead>
                            <tr>
                                <th style=${{ ...S.tableHeader, textAlign: 'left', width: '180px' }}>Tool</th>
                                <th style=${{ ...S.tableHeader, textAlign: 'left', width: '200px' }}>Key Params</th>
                                <th style=${{ ...S.tableHeader, textAlign: 'left' }}>What it does</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${group.tools.map(t => html`
                                <tr>
                                    <td style=${{ ...S.tableCellMono, color: group.color }}>${t.name}</td>
                                    <td style=${{ ...S.tableCell, fontFamily: typography.fontMono, fontSize: '11px', color: colors.textTertiary }}>${t.params}</td>
                                    <td style=${S.tableCell}>${t.desc}</td>
                                </tr>
                            `)}
                        </tbody>
                    </table>
                </div>
            `)}
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Tab 10: Dashboard
// ---------------------------------------------------------------------------

function TabDashboard() {
    return html`
        <div>
            <h2 style=${S.h2}>Dashboard (Foreman SPA)</h2>
            <p style=${{ color: colors.textSecondary, marginBottom: '24px', lineHeight: 1.6 }}>
                "Foreman" is the dashboard brand name. Single-page app with no build step.
                CDN-loaded Preact + htm. Hash-based routing. REST API at <code style=${{ fontFamily: typography.fontMono, color: colors.accent }}>/dashboard/api/*</code>.
            </p>

            <!-- Tech stack -->
            <div style=${{ ...S.card, marginBottom: '16px' }}>
                <h3 style=${S.h3}>Tech Stack</h3>
                <div style=${{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                    ${[
                        ['Preact 10.25.4', colors.accent],
                        ['htm 3.1.1', colors.blue],
                        ['CDN: esm.sh', colors.green],
                        ['No build step', colors.yellow],
                        ['No node_modules', colors.yellow],
                        ['Hash routing', colors.textSecondary],
                        ['DM Sans font', colors.textSecondary],
                        ['JetBrains Mono', colors.textSecondary],
                    ].map(([label, color]) => html`
                        <span style=${S.tag(color)}>${label}</span>
                    `)}
                </div>
            </div>

            <!-- Design tokens -->
            <div style=${{ ...S.card, marginBottom: '16px' }}>
                <h3 style=${S.h3}>Design Tokens — <span style=${{ fontFamily: typography.fontMono, fontSize: '13px' }}>dashboard/tokens.js</span></h3>
                <div style=${S.row}>
                    <div>
                        <div style=${S.h4}>Colors</div>
                        ${[
                            ['bg', '#101114', '#101114'],
                            ['surface', '#18191d', '#18191d'],
                            ['border', '#2a2c32', '#2a2c32'],
                            ['text', '#e8e9ea', '#e8e9ea'],
                            ['accent', '#7c5af6', '#7c5af6'],
                            ['green', '#3dd68c', '#3dd68c'],
                            ['yellow', '#f5a623', '#f5a623'],
                            ['red', '#f25c5c', '#f25c5c'],
                            ['blue', '#4da3ff', '#4da3ff'],
                        ].map(([name, hex, color]) => html`
                            <div style=${{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
                                <div style=${{ width: '14px', height: '14px', background: hex, borderRadius: '3px', border: '1px solid rgba(255,255,255,0.1)', flexShrink: 0 }}></div>
                                <span style=${{ fontFamily: typography.fontMono, fontSize: '12px', color }}>${name}</span>
                                <span style=${{ fontFamily: typography.fontMono, fontSize: '11px', color: colors.textTertiary }}>${hex}</span>
                            </div>
                        `)}
                    </div>
                    <div>
                        <div style=${S.h4}>Typography</div>
                        <div style=${{ fontFamily: "'DM Sans', sans-serif", fontSize: '13px', color: colors.text, marginBottom: '8px' }}>DM Sans — body text</div>
                        <div style=${{ fontFamily: "'JetBrains Mono', monospace", fontSize: '12px', color: colors.accent, marginBottom: '16px' }}>JetBrains Mono — code</div>
                        <div style=${S.h4}>Sizes (px)</div>
                        ${['xs:12', 'sm:13', 'base:14', 'lg:16', 'xl:18', '2xl:22', '3xl:28'].map(s => html`
                            <div style=${{ fontFamily: typography.fontMono, fontSize: '11px', color: colors.textSecondary, lineHeight: 1.8 }}>${s}</div>
                        `)}
                    </div>
                </div>
            </div>

            <!-- Routes -->
            <div style=${{ ...S.card, marginBottom: '16px' }}>
                <h3 style=${S.h3}>Hash Routes — <span style=${{ fontFamily: typography.fontMono, fontSize: '13px' }}>dashboard/router.js</span></h3>
                <table style=${{ width: '100%', borderCollapse: 'collapse' }}>
                    <thead>
                        <tr>
                            <th style=${{ ...S.tableHeader, textAlign: 'left', width: '220px' }}>Route</th>
                            <th style=${{ ...S.tableHeader, textAlign: 'left', width: '180px' }}>View</th>
                            <th style=${{ ...S.tableHeader, textAlign: 'left' }}>Component</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${[
                            ['#/', 'landing', 'views/LandingView.js'],
                            ['#/project/new', 'project-new', 'views/ProjectCreateView.js'],
                            ['#/project/:id', 'project', 'views/ProjectView.js'],
                            ['#/task/new', 'task-new', 'views/TaskCreateView.js'],
                            ['#/task/:id', 'task', 'views/TaskView.js'],
                            ['#/conversation/:id', 'conversation', 'views/ConversationView.js'],
                            ['#/files', 'files', 'components/Files.js'],
                            ['#/settings', 'settings', 'components/Settings.js'],
                            ['#/docs', 'docs', 'docs/architecture.jsx (this file)'],
                        ].map(([route, view, component]) => html`
                            <tr>
                                <td style=${{ ...S.tableCellMono, color: colors.accent }}>${route}</td>
                                <td style=${{ ...S.tableCell, fontFamily: typography.fontMono, fontSize: '12px', color: colors.textTertiary }}>${view}</td>
                                <td style=${{ ...S.tableCell, fontFamily: typography.fontMono, fontSize: '12px', color: colors.textSecondary }}>${component}</td>
                            </tr>
                        `)}
                    </tbody>
                </table>
            </div>

            <!-- Component tree -->
            <div style=${S.card}>
                <h3 style=${S.h3}>Component Tree</h3>
                <div style=${S.codeBlock}>${`foreman.html
└── foreman-app.js (ForemanApp)
    ├── router.js (useRouter)
    └── foreman-shell.js (ForemanShell)
        ├── ForemanHeader
        │   ├── Brand link (#/)
        │   ├── Files link (#/files)
        │   ├── Settings link (#/settings)
        │   └── Docs link (#/docs)
        └── <main> (view slot)
            ├── LandingView — project grid
            ├── ProjectView — project detail + tasks + components
            ├── TaskView — task detail + checklist + logs
            ├── ConversationView — message thread
            ├── TaskCreateView — create task form
            ├── ProjectCreateView — create project form
            ├── Settings — instance + user settings
            ├── Files — file browser
            └── ArchitectureDocs — this component`}</div>

                <div style=${{ marginTop: '16px' }}>
                    <h4 style=${S.h4}>Visual Check System</h4>
                    <p style=${{ color: colors.textSecondary, fontSize: '13px', lineHeight: 1.6, margin: '0 0 8px 0' }}>
                        Dashboard tasks must include Playwright screenshots. The visual check script renders pages with mock data.
                    </p>
                    <div style=${S.codeBlock}>${`python3 scripts/visual-check.py settings
python3 scripts/visual-check.py landing
python3 scripts/visual-check.py docs`}</div>
                    <p style=${{ color: colors.textSecondary, fontSize: '12px', marginTop: '8px' }}>
                        Config: <code style=${{ fontFamily: typography.fontMono }}>scripts/visual-config.json</code> —
                        Fixtures: <code style=${{ fontFamily: typography.fontMono }}>fixtures/visual/*.json</code>
                    </p>
                </div>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Tab definitions
// ---------------------------------------------------------------------------

const TABS = [
    { id: 'system',     label: 'System Architecture', component: TabSystemArchitecture },
    { id: 'lifecycle',  label: 'Task Lifecycle',       component: TabTaskLifecycle },
    { id: 'gates',      label: 'Gate Pipeline',        component: TabGatePipeline },
    { id: 'config',     label: 'Config Inheritance',   component: TabConfigInheritance },
    { id: 'auth',       label: 'Auth & Security',      component: TabAuthSecurity },
    { id: 'data',       label: 'Data Model',            component: TabDataModel },
    { id: 'files',      label: 'File Map',              component: TabFileMap },
    { id: 'recovery',   label: 'Recovery',              component: TabRecovery },
    { id: 'tools',      label: 'MCP Tools',             component: TabMcpTools },
    { id: 'dashboard',  label: 'Dashboard',             component: TabDashboard },
];

// ---------------------------------------------------------------------------
// Root component
// ---------------------------------------------------------------------------

export function ArchitectureDocs() {
    const [activeTab, setActiveTab] = useState('system');
    const tab = TABS.find(t => t.id === activeTab) || TABS[0];
    const TabContent = tab.component;

    return html`
        <div style=${{ fontFamily: typography.fontBody, color: colors.text }}>
            <!-- Tab bar -->
            <div style=${{
                display: 'flex',
                gap: '2px',
                overflowX: 'auto',
                borderBottom: `1px solid ${colors.border}`,
                marginBottom: '28px',
                paddingBottom: '0',
                scrollbarWidth: 'none',
                msOverflowStyle: 'none',
            }}>
                ${TABS.map(t => html`
                    <button
                        key=${t.id}
                        onClick=${() => setActiveTab(t.id)}
                        style=${{
                            background: 'none',
                            border: 'none',
                            borderBottom: `2px solid ${t.id === activeTab ? colors.accent : 'transparent'}`,
                            color: t.id === activeTab ? colors.text : colors.textTertiary,
                            padding: '10px 14px',
                            cursor: 'pointer',
                            fontSize: '13px',
                            fontWeight: t.id === activeTab ? typography.weight.semibold : typography.weight.normal,
                            fontFamily: typography.fontBody,
                            whiteSpace: 'nowrap',
                            transition: 'color 0.15s ease',
                        }}
                    >${t.label}</button>
                `)}
            </div>

            <!-- Tab content -->
            <${TabContent} />
        </div>
    `;
}

export default ArchitectureDocs;
