// Foreman Project View
// Layout: Tab bar → Tasks / Conversations / Files / Settings tabs
// Spec: foreman-design conversation, messages [6-9]

import { h } from 'https://esm.sh/preact@10.25.4';
import { useState, useEffect, useCallback } from 'https://esm.sh/preact@10.25.4/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, layout, animation } from '../tokens.js';
import { routes, navigate } from '../router.js';
import { api } from '../api.js';
import { relativeTime } from '../components/utils.js';
import { TaskView } from './TaskView.js';
import { styles as fkStyles, FormField, FormRow, Toggle } from '../components/FormKit.js';
import { ProjectHeader } from '../components/ProjectHeader.js';
import { TaskList } from '../components/TaskList.js';
import { buildChainMap } from '../components/TaskRow.js';

const html = htm.bind(h);

const POLL_INTERVAL_MS = 15_000;

// ---------------------------------------------------------------------------
// Conversations section (used in Conversations tab)
// ---------------------------------------------------------------------------

function ConversationsSection({ conversations }) {
    const projectConvs = conversations;
    if (projectConvs.length === 0) return html`
        <div style=${{
            padding: '60px 0',
            textAlign: 'center',
            color: colors.textTertiary,
            fontSize: typography.size.sm,
        }}>No conversations yet</div>
    `;

    const listStyle = {
        display: 'flex',
        flexDirection: 'column',
        gap: '4px',
    };

    const rowStyle = {
        display: 'flex',
        alignItems: 'baseline',
        justifyContent: 'space-between',
        gap: '12px',
        padding: '8px 12px',
        borderRadius: layout.borderRadius.md,
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        textDecoration: 'none',
        color: colors.text,
        transition: `background ${animation.durationFast}`,
    };

    return html`
        <div style=${listStyle}>
            ${projectConvs.map(conv => html`
                <a key=${conv.id}
                   href=${routes.conversation(conv.id)}
                   style=${rowStyle}
                   class="foreman-conv-row"
                >
                    <span style=${{
                        flex: 1,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        fontSize: typography.size.sm,
                    }}>
                        ${conv.goal || conv.id}
                    </span>
                    <span style=${{
                        fontFamily: typography.fontMono,
                        fontSize: typography.size.xs,
                        color: colors.textTertiary,
                        flexShrink: 0,
                    }}>${relativeTime(conv.last_activity || conv.updated_at)}</span>
                </a>
            `)}
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Task Panel — slide-out triage panel
// ---------------------------------------------------------------------------

function TaskPanel({ taskId, onClose }) {
    const [isMobile, setIsMobile] = useState(() => window.innerWidth < 640);

    useEffect(() => {
        const check = () => setIsMobile(window.innerWidth < 640);
        window.addEventListener('resize', check);
        return () => window.removeEventListener('resize', check);
    }, []);

    useEffect(() => {
        if (!taskId) return;
        const onKey = (e) => { if (e.key === 'Escape') onClose(); };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [taskId, onClose]);

    if (!taskId) return null;

    const panelStyle = isMobile ? {
        position: 'fixed',
        left: 0, right: 0, bottom: 0,
        height: '65vh',
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: `${layout.borderRadius.lg} ${layout.borderRadius.lg} 0 0`,
        boxShadow: '0 -8px 40px rgba(0,0,0,0.5)',
        zIndex: 500,
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
        animation: `foreman-slide-up ${animation.durationNormal} ${animation.easing}`,
    } : {
        position: 'fixed',
        top: 0, right: 0, bottom: 0,
        width: 'clamp(420px, 33vw, 560px)',
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderLeft: `1px solid ${colors.border}`,
        boxShadow: '-8px 0 40px rgba(0,0,0,0.4)',
        zIndex: 500,
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
        animation: `foreman-slide-right ${animation.durationNormal} ${animation.easing}`,
    };

    const headerStyle = {
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '12px 16px',
        borderBottom: `1px solid ${colors.border}`,
        flexShrink: 0,
    };

    const closeBtnStyle = {
        background: 'none', border: 'none',
        color: colors.textTertiary, cursor: 'pointer',
        fontSize: '20px', lineHeight: 1,
        padding: '2px 6px',
        borderRadius: layout.borderRadius.sm,
    };

    const backdropStyle = {
        position: 'fixed', inset: 0,
        background: 'rgba(0,0,0,0.4)', zIndex: 499,
    };

    return html`
        <div>
            <style>${`
                @keyframes foreman-slide-right {
                    from { transform: translateX(100%); opacity: 0; }
                    to   { transform: translateX(0);    opacity: 1; }
                }
                @keyframes foreman-slide-up {
                    from { transform: translateY(100%); opacity: 0; }
                    to   { transform: translateY(0);    opacity: 1; }
                }
            `}</style>
            <div style=${backdropStyle} onClick=${onClose} />
            <div style=${panelStyle}>
                <div style=${headerStyle}>
                    <span style=${{ flex: 1 }} />
                    <button style=${closeBtnStyle} onClick=${onClose} title="Close (Esc)">×</button>
                </div>
                <div style=${{ flex: 1, overflowY: 'auto', padding: '16px' }}>
                    <${TaskView} id=${taskId} mode="compact" onClose=${onClose} />
                </div>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// RepoUrlField — read-only repo URL display with copy button
// ---------------------------------------------------------------------------

function RepoUrlField({ repo }) {
    const [copied, setCopied] = useState(false);

    if (!repo) return null;

    const handleCopy = async () => {
        try {
            await navigator.clipboard.writeText(repo);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
        } catch (_) {}
    };

    return html`
        <${FormField} label="Repository">
            <div style=${{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span style=${{
                    flex: 1,
                    fontFamily: typography.fontMono,
                    fontSize: typography.size.sm,
                    color: colors.textSecondary,
                    background: colors.bg,
                    border: `1px solid ${colors.border}`,
                    borderRadius: layout.borderRadius.sm,
                    padding: '6px 10px',
                    wordBreak: 'break-all',
                    lineHeight: '1.4',
                    display: 'block',
                }}>${repo}</span>
                <button
                    type="button"
                    onClick=${handleCopy}
                    title="Copy repository URL"
                    style=${{
                        flexShrink: 0,
                        padding: '6px 10px',
                        borderRadius: layout.borderRadius.sm,
                        background: copied ? colors.greenBg : colors.surfaceHover,
                        border: `1px solid ${copied ? colors.green + '44' : colors.border}`,
                        color: copied ? colors.green : colors.textTertiary,
                        cursor: 'pointer',
                        fontSize: typography.size.xs,
                        fontFamily: typography.fontBody,
                        transition: 'color 0.15s, background 0.15s, border-color 0.15s',
                        whiteSpace: 'nowrap',
                    }}
                >${copied ? 'Copied!' : 'Copy'}</button>
            </div>
        </${FormField}>
    `;
}

// ---------------------------------------------------------------------------
// DangerZone — delete project confirmation
// ---------------------------------------------------------------------------

function DangerZone({ project, projectId }) {
    const [confirmText, setConfirmText] = useState('');
    const [deleting, setDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState(null);

    const canDelete = confirmText === projectId && !deleting;

    const handleDelete = async () => {
        if (!canDelete) return;
        setDeleting(true);
        setDeleteError(null);
        try {
            await api.deleteProject(projectId);
            navigate('/');
        } catch (e) {
            setDeleteError(e.message || 'Delete failed');
            setDeleting(false);
        }
    };

    return html`
        <div style=${{
            marginTop: '40px',
            padding: '20px',
            border: `1px solid ${colors.red}44`,
            borderRadius: layout.borderRadius.md,
        }}>
            <div style=${{
                fontSize: '11px',
                fontWeight: 600,
                color: colors.red,
                letterSpacing: '0.08em',
                textTransform: 'uppercase',
                marginBottom: '12px',
            }}>Danger Zone</div>
            <p style=${{
                fontSize: typography.size.sm,
                color: colors.textSecondary,
                margin: '0 0 16px 0',
                lineHeight: 1.5,
            }}>
                Permanently delete this project and all associated tasks, checklist items, and messages.
                This action cannot be undone.
            </p>
            <div style=${{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                <input
                    type="text"
                    value=${confirmText}
                    onInput=${e => setConfirmText(e.target.value)}
                    placeholder=${`Type "${projectId}" to confirm`}
                    style=${{
                        ...fkStyles.input,
                        flex: 1,
                        minWidth: '200px',
                        borderColor: confirmText && confirmText !== projectId ? colors.red + '88' : undefined,
                    }}
                />
                <button
                    onClick=${handleDelete}
                    disabled=${!canDelete}
                    style=${{
                        padding: '7px 16px',
                        borderRadius: layout.borderRadius.sm,
                        background: canDelete ? colors.red : colors.redBg,
                        border: `1px solid ${colors.red}${canDelete ? '' : '44'}`,
                        color: canDelete ? '#fff' : colors.red + '88',
                        cursor: canDelete ? 'pointer' : 'not-allowed',
                        fontSize: typography.size.sm,
                        fontFamily: typography.fontBody,
                        fontWeight: typography.weight.medium,
                        whiteSpace: 'nowrap',
                        flexShrink: 0,
                        transition: 'background 0.15s, color 0.15s',
                    }}
                >${deleting ? 'Deleting…' : 'Delete Project'}</button>
            </div>
            ${deleteError ? html`
                <div style=${{ marginTop: '10px', fontSize: typography.size.xs, color: colors.red }}>
                    ${deleteError}
                </div>
            ` : null}
        </div>
    `;
}

// ---------------------------------------------------------------------------
// SettingsTab — inline project config form + danger zone
// ---------------------------------------------------------------------------

function SettingsTab({ project, projectId, onSaved }) {
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState(null);
    const [saveSuccess, setSaveSuccess] = useState(false);

    const [defaultBranch, setDefaultBranch] = useState(project.default_branch || 'main');
    const [model, setModel] = useState(project.model || '');
    const [reviewModel, setReviewModel] = useState(project.review_model || '');
    const [setupCommand, setSetupCommand] = useState(project.setup_command || '');
    const [testCommand, setTestCommand] = useState(project.test_command || '');
    const [teardownCommand, setTeardownCommand] = useState(project.teardown_command || '');
    const [maxTurns, setMaxTurns] = useState(project.max_turns != null ? String(project.max_turns) : '');
    const [maxWallClock, setMaxWallClock] = useState(project.max_wall_clock != null ? String(project.max_wall_clock) : '');
    const [autoTest, setAutoTest] = useState(project.auto_test != null ? Boolean(project.auto_test) : true);
    const [autoReview, setAutoReview] = useState(project.auto_review != null ? Boolean(project.auto_review) : true);
    const [autoPr, setAutoPr] = useState(project.auto_pr != null ? Boolean(project.auto_pr) : false);
    const [autoMerge, setAutoMerge] = useState(project.auto_merge != null ? Boolean(project.auto_merge) : false);
    const [reviewIgnorePatterns, setReviewIgnorePatterns] = useState(
        Array.isArray(project.review_ignore_patterns)
            ? project.review_ignore_patterns.join('\n')
            : (project.review_ignore_patterns || '')
    );
    const [envOverrides, setEnvOverrides] = useState(
        project.env_overrides && typeof project.env_overrides === 'object'
            ? JSON.stringify(project.env_overrides, null, 2)
            : (project.env_overrides || '')
    );
    const [envError, setEnvError] = useState(null);
    const [githubPatOverride, setGithubPatOverride] = useState(null);

    const handleSave = async () => {
        setEnvError(null);
        let parsedEnv = undefined;
        if (envOverrides.trim()) {
            try {
                parsedEnv = JSON.parse(envOverrides);
            } catch (_) {
                setEnvError('Invalid JSON in env overrides');
                return;
            }
        }

        setSaving(true);
        setError(null);
        setSaveSuccess(false);
        try {
            const fields = {
                default_branch: defaultBranch.trim() || 'main',
                model: model || null,
                review_model: reviewModel || null,
                setup_command: setupCommand.trim() || null,
                test_command: testCommand.trim() || null,
                teardown_command: teardownCommand.trim() || null,
                max_turns: maxTurns.trim() ? parseInt(maxTurns, 10) : null,
                max_wall_clock: maxWallClock.trim() ? parseInt(maxWallClock, 10) : null,
                auto_test: autoTest,
                auto_review: autoReview,
                auto_pr: autoPr,
                auto_merge: autoMerge,
                review_ignore_patterns: reviewIgnorePatterns.trim()
                    ? reviewIgnorePatterns.split('\n').map(s => s.trim()).filter(Boolean)
                    : null,
                env_overrides: parsedEnv !== undefined ? parsedEnv : (envOverrides.trim() ? undefined : null),
            };
            if (githubPatOverride !== null) {
                fields.github_pat_override = githubPatOverride || null;
            }
            Object.keys(fields).forEach(k => fields[k] === undefined && delete fields[k]);

            await api.updateProject(project.id, fields);
            onSaved();
            setSaveSuccess(true);
            setTimeout(() => setSaveSuccess(false), 3000);
        } catch (e) {
            setError(e.message || 'Save failed');
        } finally {
            setSaving(false);
        }
    };

    const sectionLabelStyle = {
        fontSize: '11px',
        fontWeight: typography.weight.medium,
        color: colors.textTertiary,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        marginBottom: '10px',
        marginTop: '4px',
        paddingBottom: '6px',
        borderBottom: `1px solid ${colors.border}33`,
    };

    const inheritHintStyle = {
        fontSize: '10px',
        color: colors.textTertiary,
        fontStyle: 'italic',
        marginTop: '3px',
    };

    const toggleRowStyle = {
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '8px 0',
        borderBottom: `1px solid ${colors.border}22`,
    };

    const toggleLabelStyle = {
        fontSize: typography.size.sm,
        color: colors.text,
        flex: 1,
    };

    const toggleSubStyle = {
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        marginTop: '2px',
    };

    return html`
        <div style=${{ maxWidth: '680px', display: 'flex', flexDirection: 'column', gap: '28px' }}>

            ${error ? html`
                <div style=${{
                    padding: '10px 14px',
                    background: colors.redBg,
                    border: `1px solid ${colors.red}44`,
                    borderRadius: layout.borderRadius.md,
                    color: colors.red,
                    fontSize: typography.size.sm,
                }}>${error}</div>
            ` : null}

            ${saveSuccess ? html`
                <div style=${{
                    padding: '10px 14px',
                    background: colors.greenBg,
                    border: `1px solid ${colors.green}44`,
                    borderRadius: layout.borderRadius.md,
                    color: colors.green,
                    fontSize: typography.size.sm,
                }}>Settings saved</div>
            ` : null}

            <!-- Git section -->
            <div>
                <div style=${sectionLabelStyle}>Git</div>
                <${RepoUrlField} repo=${project.repo} />
                <${FormField} label="Default Branch">
                    <input
                        type="text"
                        value=${defaultBranch}
                        onInput=${e => setDefaultBranch(e.target.value)}
                        style=${fkStyles.input}
                        placeholder="main"
                    />
                    <div style=${inheritHintStyle}>Inherits to tasks as merge target</div>
                </${FormField}>
                <${FormField} label="GitHub PAT (project-specific)">
                    <input
                        type="password"
                        value=${githubPatOverride ?? ''}
                        onInput=${e => setGithubPatOverride(e.target.value)}
                        style=${fkStyles.input}
                        placeholder="ghp_… (leave blank to use instance PAT)"
                        autoComplete="new-password"
                    />
                    <div style=${{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '4px' }}>
                        ${(() => {
                            const patIsSet = githubPatOverride !== null ? Boolean(githubPatOverride) : Boolean(project.github_pat_override);
                            return html`
                                <span style=${{
                                    fontSize: '11px',
                                    color: patIsSet ? colors.accent : colors.textTertiary,
                                    fontStyle: patIsSet ? 'normal' : 'italic',
                                    flex: 1,
                                }}>
                                    ${patIsSet ? 'Using project PAT' : 'Using instance PAT (default)'}
                                </span>
                                ${patIsSet ? html`
                                    <button
                                        type="button"
                                        onClick=${() => setGithubPatOverride('')}
                                        style=${{
                                            background: 'none', border: 'none',
                                            color: colors.textTertiary, cursor: 'pointer',
                                            fontSize: '11px', padding: '0',
                                            textDecoration: 'underline',
                                        }}
                                    >Clear</button>
                                ` : null}
                            `;
                        })()}
                    </div>
                </${FormField}>
            </div>

            <!-- Models section -->
            <div>
                <div style=${sectionLabelStyle}>Models</div>
                <${FormRow}>
                    <${FormField} label="Worker Model">
                        <select
                            value=${model}
                            onChange=${e => setModel(e.target.value)}
                            style=${fkStyles.select}
                        >
                            <option value="">System default</option>
                            <option value="sonnet">sonnet</option>
                            <option value="opus">opus</option>
                        </select>
                        <div style=${inheritHintStyle}>Inherits to tasks</div>
                    </${FormField}>
                    <${FormField} label="Review Model">
                        <select
                            value=${reviewModel}
                            onChange=${e => setReviewModel(e.target.value)}
                            style=${fkStyles.select}
                        >
                            <option value="">System default (opus)</option>
                            <option value="sonnet">sonnet</option>
                            <option value="opus">opus</option>
                        </select>
                        <div style=${inheritHintStyle}>Inherits to tasks</div>
                    </${FormField}>
                </${FormRow}>
            </div>

            <!-- Commands section -->
            <div>
                <div style=${sectionLabelStyle}>Commands</div>
                <${FormField} label="Setup Command">
                    <textarea
                        value=${setupCommand}
                        onInput=${e => setSetupCommand(e.target.value)}
                        style=${{ ...fkStyles.input, resize: 'vertical', minHeight: '60px', fontFamily: typography.fontMono, fontSize: typography.size.xs }}
                        placeholder="e.g. npm install"
                        rows="2"
                    />
                    <div style=${inheritHintStyle}>Run after worktree creation — inherits to tasks</div>
                </${FormField}>
                <${FormField} label="Test Command">
                    <textarea
                        value=${testCommand}
                        onInput=${e => setTestCommand(e.target.value)}
                        style=${{ ...fkStyles.input, resize: 'vertical', minHeight: '60px', fontFamily: typography.fontMono, fontSize: typography.size.xs }}
                        placeholder="e.g. pytest tests/"
                        rows="2"
                    />
                    <div style=${inheritHintStyle}>Used by test gate — inherits to tasks</div>
                </${FormField}>
                <${FormField} label="Teardown Command">
                    <textarea
                        value=${teardownCommand}
                        onInput=${e => setTeardownCommand(e.target.value)}
                        style=${{ ...fkStyles.input, resize: 'vertical', minHeight: '60px', fontFamily: typography.fontMono, fontSize: typography.size.xs }}
                        placeholder="e.g. docker compose down"
                        rows="2"
                    />
                    <div style=${inheritHintStyle}>Run on worktree cleanup</div>
                </${FormField}>
            </div>

            <!-- Limits section -->
            <div>
                <div style=${sectionLabelStyle}>Limits</div>
                <${FormRow}>
                    <${FormField} label="Max Turns">
                        <input
                            type="number"
                            value=${maxTurns}
                            onInput=${e => setMaxTurns(e.target.value)}
                            style=${fkStyles.input}
                            placeholder="System default"
                            min="1"
                        />
                        <div style=${inheritHintStyle}>Inherits to tasks</div>
                    </${FormField}>
                    <${FormField} label="Max Wall Clock (minutes)">
                        <input
                            type="number"
                            value=${maxWallClock}
                            onInput=${e => setMaxWallClock(e.target.value)}
                            style=${fkStyles.input}
                            placeholder="System default"
                            min="1"
                        />
                        <div style=${inheritHintStyle}>Inherits to tasks</div>
                    </${FormField}>
                </${FormRow}>
            </div>

            <!-- Automation section -->
            <div>
                <div style=${sectionLabelStyle}>Automation</div>

                <div style=${toggleRowStyle}>
                    <div style=${{ flex: 1 }}>
                        <div style=${toggleLabelStyle}>Auto Test</div>
                        <div style=${toggleSubStyle}>Run test gate after each session — inherits to tasks</div>
                    </div>
                    <${Toggle} checked=${autoTest} onChange=${() => setAutoTest(v => !v)} />
                </div>

                <div style=${toggleRowStyle}>
                    <div style=${{ flex: 1 }}>
                        <div style=${toggleLabelStyle}>Auto Review</div>
                        <div style=${toggleSubStyle}>Run Opus self-review gate after test pass — inherits to tasks</div>
                    </div>
                    <${Toggle} checked=${autoReview} onChange=${() => setAutoReview(v => !v)} />
                </div>

                <div style=${toggleRowStyle}>
                    <div style=${{ flex: 1 }}>
                        <div style=${toggleLabelStyle}>Auto PR</div>
                        <div style=${toggleSubStyle}>Create PR when chain tail passes all gates — inherits to tasks. Mutually exclusive with Auto Merge.</div>
                    </div>
                    <${Toggle}
                        checked=${autoPr}
                        onChange=${() => { setAutoPr(v => !v); if (!autoPr) setAutoMerge(false); }}
                    />
                </div>

                <div style=${toggleRowStyle}>
                    <div style=${{ flex: 1 }}>
                        <div style=${toggleLabelStyle}>Auto Merge</div>
                        <div style=${toggleSubStyle}>Merge branch on gate pass — inherits to tasks. Mutually exclusive with Auto PR.</div>
                    </div>
                    <${Toggle}
                        checked=${autoMerge}
                        onChange=${() => { setAutoMerge(v => !v); if (!autoMerge) setAutoPr(false); }}
                    />
                </div>
            </div>

            <!-- Advanced section -->
            <div>
                <div style=${sectionLabelStyle}>Advanced</div>

                <${FormField} label="Review Ignore Patterns">
                    <textarea
                        value=${reviewIgnorePatterns}
                        onInput=${e => setReviewIgnorePatterns(e.target.value)}
                        style=${{ ...fkStyles.input, resize: 'vertical', minHeight: '72px', fontFamily: typography.fontMono, fontSize: typography.size.xs }}
                        placeholder=${"*.lock\nvendor/"}
                        rows="3"
                    />
                    <div style=${inheritHintStyle}>One glob pattern per line — excludes files from reviewer diffs</div>
                </${FormField}>

                <${FormField} label="Env Overrides">
                    <textarea
                        value=${envOverrides}
                        onInput=${e => { setEnvOverrides(e.target.value); setEnvError(null); }}
                        style=${{ ...fkStyles.input, resize: 'vertical', minHeight: '100px', fontFamily: typography.fontMono, fontSize: typography.size.xs }}
                        placeholder='{"NODE_ENV": "test"}'
                        rows="4"
                    />
                    ${envError ? html`
                        <div style=${{ fontSize: typography.size.xs, color: colors.red, marginTop: '4px' }}>${envError}</div>
                    ` : html`
                        <div style=${inheritHintStyle}>JSON key-value pairs written to .env.testing in worktree</div>
                    `}
                </${FormField}>
            </div>

            <!-- Save button -->
            <div style=${{ display: 'flex', gap: '8px' }}>
                <button
                    onClick=${handleSave}
                    disabled=${saving}
                    style=${{
                        ...fkStyles.buttonPrimary,
                        padding: '8px 20px',
                        fontSize: typography.size.sm,
                        opacity: saving ? 0.6 : 1,
                        cursor: saving ? 'not-allowed' : 'pointer',
                    }}
                >${saving ? 'Saving…' : 'Save Changes'}</button>
            </div>

            <!-- Danger Zone -->
            <${DangerZone} project=${project} projectId=${projectId} />
        </div>
    `;
}

// ---------------------------------------------------------------------------
// TabBar — tab navigation for project sub-routes
// ---------------------------------------------------------------------------

function TabBar({ projectId, activeTab, conversationCount }) {
    const tabs = [
        { id: 'tasks', label: 'Tasks' },
        { id: 'conversations', label: 'Conversations', badge: conversationCount || null },
        { id: 'files', label: 'Files' },
        { id: 'settings', label: 'Settings' },
    ];

    const tabStyle = (isActive) => ({
        display: 'inline-flex',
        alignItems: 'center',
        gap: '6px',
        padding: '10px 16px',
        fontSize: typography.size.sm,
        fontWeight: isActive ? typography.weight.semibold : typography.weight.normal,
        color: isActive ? colors.text : colors.textTertiary,
        textDecoration: 'none',
        borderBottom: isActive ? `2px solid ${colors.accent}` : '2px solid transparent',
        transition: `color ${animation.durationFast}, border-color ${animation.durationFast}`,
        whiteSpace: 'nowrap',
    });

    const badgeStyle = {
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        minWidth: '18px',
        height: '16px',
        padding: '0 5px',
        borderRadius: '999px',
        background: colors.accentBg,
        border: `1px solid ${colors.accent}44`,
        color: colors.accent,
        fontSize: '10px',
        fontWeight: typography.weight.semibold,
        lineHeight: 1,
    };

    return html`
        <div style=${{
            display: 'flex',
            borderBottom: `1px solid ${colors.border}`,
            marginBottom: '24px',
            overflowX: 'auto',
        }}>
            ${tabs.map(tab => html`
                <a
                    key=${tab.id}
                    href=${routes.projectTab(projectId, tab.id)}
                    style=${tabStyle(activeTab === tab.id)}
                >
                    ${tab.label}
                    ${tab.badge ? html`<span style=${badgeStyle}>${tab.badge}</span>` : null}
                </a>
            `)}
        </div>
    `;
}

// ---------------------------------------------------------------------------
// ProjectView — root component
// ---------------------------------------------------------------------------

export function ProjectView({ id, tab }) {
    const activeTab = tab || 'tasks';

    const [project, setProject] = useState(null);
    const [tasks, setTasks] = useState([]);
    const [conversations, setConversations] = useState([]);
    const [error, setError] = useState(null);
    const [loading, setLoading] = useState(true);
    const [selectedTaskId, setSelectedTaskId] = useState(null);
    const [saveToast, setSaveToast] = useState(false);

    const [statusFilter, setStatusFilter] = useState('');

    const _searchStorageKey = `foreman_search_${id}`;
    const [searchQuery, setSearchQuery] = useState(() => {
        try { return localStorage.getItem(_searchStorageKey) || ''; } catch (_) { return ''; }
    });
    const [searchResults, setSearchResults] = useState(null);
    const [searchLoading, setSearchLoading] = useState(false);

    const chainMap = buildChainMap(tasks);

    const load = useCallback(async () => {
        try {
            const [proj, taskList, convList] = await Promise.all([
                api.getProject(id),
                api.getTasks({ project_id: id }),
                api.getConversations({ project: id }).catch(() => []),
            ]);
            setProject(proj);
            setTasks(taskList);
            setConversations(convList);
            setError(null);
            setLoading(false);
        } catch (e) {
            setError(e.message || 'Failed to load project');
            setLoading(false);
        }
    }, [id]);

    useEffect(() => {
        setLoading(true);
        load();
    }, [load]);

    useEffect(() => {
        const timer = setInterval(load, POLL_INTERVAL_MS);
        return () => clearInterval(timer);
    }, [load]);

    const handleSearch = useCallback(async (query) => {
        setSearchQuery(query);
        try { localStorage.setItem(_searchStorageKey, query); } catch (_) {}
        if (!query) {
            try { localStorage.removeItem(_searchStorageKey); } catch (_) {}
            setSearchResults(null);
            setSearchLoading(false);
            return;
        }
        setSearchLoading(true);
        try {
            const result = await api.search({ q: query, project_id: id, limit: 20 });
            setSearchResults(result.results || []);
        } catch (e) {
            setSearchResults([]);
        } finally {
            setSearchLoading(false);
        }
    }, [id, _searchStorageKey]);

    useEffect(() => {
        if (searchQuery) {
            handleSearch(searchQuery);
        }
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    const pageStyle = {
        display: 'flex',
        flexDirection: 'column',
        gap: '0',
        maxWidth: '1100px',
        margin: '0 auto',
    };

    const backLinkStyle = {
        display: 'inline-flex',
        alignItems: 'center',
        gap: '5px',
        fontSize: typography.size.sm,
        color: colors.textTertiary,
        textDecoration: 'none',
        marginBottom: '16px',
        transition: `color ${animation.durationFast}`,
    };

    const errorStyle = {
        padding: '24px',
        borderRadius: layout.borderRadius.md,
        background: colors.redBg,
        border: `1px solid ${colors.red}44`,
        color: colors.red,
        fontSize: typography.size.sm,
    };

    const loadingStyle = {
        padding: '60px 0',
        textAlign: 'center',
        color: colors.textTertiary,
        fontSize: typography.size.sm,
    };

    if (loading) {
        return html`
            <div style=${pageStyle}>
                <a href=${routes.landing()} style=${backLinkStyle} class="foreman-back-link">← Projects</a>
                <div style=${loadingStyle}>Loading…</div>
            </div>
        `;
    }

    if (error) {
        return html`
            <div style=${pageStyle}>
                <a href=${routes.landing()} style=${backLinkStyle} class="foreman-back-link">← Projects</a>
                <div style=${{
                    ...errorStyle,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    gap: '16px',
                }}>
                    <span>Error: ${error}</span>
                    <button onClick=${load} style=${{
                        padding: '4px 12px',
                        borderRadius: layout.borderRadius.sm,
                        background: `${colors.red}22`,
                        border: `1px solid ${colors.red}44`,
                        color: colors.red,
                        fontSize: typography.size.sm,
                        cursor: 'pointer',
                        flexShrink: 0,
                    }}>Retry</button>
                </div>
            </div>
        `;
    }

    // Render tab content
    let tabContent;
    if (activeTab === 'tasks') {
        tabContent = html`
            <${TaskList}
                tasks=${tasks}
                conversations=${conversations}
                chainMap=${chainMap}
                statusFilter=${statusFilter}
                onStatusFilter=${setStatusFilter}
                onTaskSelect=${setSelectedTaskId}
                searchQuery=${searchQuery}
                searchResults=${searchResults}
                searchLoading=${searchLoading}
                onSearch=${handleSearch}
                projectId=${id}
            />
        `;
    } else if (activeTab === 'conversations') {
        tabContent = html`<${ConversationsSection} conversations=${conversations} />`;
    } else if (activeTab === 'files') {
        tabContent = html`
            <div style=${{
                padding: '60px 0',
                textAlign: 'center',
                color: colors.textTertiary,
                fontSize: typography.size.sm,
            }}>Files — coming soon</div>
        `;
    } else if (activeTab === 'settings') {
        tabContent = html`
            <${SettingsTab}
                project=${project}
                projectId=${id}
                onSaved=${async () => {
                    await load();
                    setSaveToast(true);
                    setTimeout(() => setSaveToast(false), 3000);
                }}
            />
        `;
    }

    return html`
        <div style=${pageStyle}>
            <!-- Back navigation -->
            <a href=${routes.landing()} style=${backLinkStyle} class="foreman-back-link">← Projects</a>

            <!-- Project header -->
            <div style=${{ marginBottom: '0', paddingBottom: '0' }}>
                <${ProjectHeader} project=${project} id=${id} />
            </div>

            <!-- Tab bar -->
            <${TabBar}
                projectId=${id}
                activeTab=${activeTab}
                conversationCount=${conversations.length}
            />

            <!-- Tab content -->
            ${tabContent}
        </div>

        <!-- Task Panel slide-out -->
        <${TaskPanel}
            taskId=${selectedTaskId}
            onClose=${() => setSelectedTaskId(null)}
        />

        <!-- Save success toast -->
        ${saveToast ? html`
            <div style=${{
                position: 'fixed',
                bottom: '24px',
                left: '50%',
                transform: 'translateX(-50%)',
                background: colors.green,
                color: '#fff',
                padding: '8px 20px',
                borderRadius: layout.borderRadius.md,
                fontSize: typography.size.sm,
                fontFamily: typography.fontBody,
                fontWeight: typography.weight.medium,
                zIndex: 700,
                boxShadow: '0 4px 20px rgba(0,0,0,0.4)',
                pointerEvents: 'none',
            }}>Project settings saved</div>
        ` : null}
    `;
}
