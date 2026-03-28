// ProjectCreateView — Register a new git repo as a Switchboard project
// All config fields are required (no silent fallthrough to system defaults).

import { h } from 'https://esm.sh/preact@10.25.4';
import { useState, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, layout } from '../tokens.js';
import { navigate } from '../router.js';
import { api } from '../api.js';

const html = htm.bind(h);

// ---------------------------------------------------------------------------
// HelpIcon — hover tooltip using pure CSS
// ---------------------------------------------------------------------------

function HelpIcon({ text }) {
    const wrapStyle = {
        position: 'relative',
        display: 'inline-flex',
        alignItems: 'center',
        marginLeft: '5px',
        cursor: 'default',
        verticalAlign: 'middle',
    };

    return html`
        <span style=${wrapStyle} class="help-icon-wrap">
            <span class="help-icon-trigger" style=${{
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                width: '14px',
                height: '14px',
                borderRadius: '50%',
                border: `1px solid ${colors.textTertiary}`,
                color: colors.textTertiary,
                fontSize: '10px',
                lineHeight: 1,
                fontFamily: typography.fontBody,
                flexShrink: 0,
            }}>?</span>
            <span class="help-icon-tooltip" style=${{
                position: 'absolute',
                bottom: 'calc(100% + 6px)',
                left: '50%',
                transform: 'translateX(-50%)',
                background: colors.surfaceActive,
                border: `1px solid ${colors.border}`,
                borderRadius: layout.borderRadius.md,
                padding: '7px 10px',
                fontSize: typography.size.xs,
                color: colors.textSecondary,
                whiteSpace: 'pre-wrap',
                maxWidth: '260px',
                width: 'max-content',
                lineHeight: 1.5,
                zIndex: 100,
                pointerEvents: 'none',
                display: 'none',
            }}>${text}</span>
        </span>
    `;
}

// ---------------------------------------------------------------------------
// Toggle — on/off boolean toggle
// ---------------------------------------------------------------------------

function Toggle({ value, onChange }) {
    const trackStyle = {
        display: 'inline-flex',
        alignItems: 'center',
        width: '36px',
        height: '20px',
        borderRadius: '10px',
        background: value ? colors.green : colors.surfaceActive,
        border: `1px solid ${value ? colors.green : colors.border}`,
        cursor: 'pointer',
        transition: 'background 150ms, border-color 150ms',
        flexShrink: 0,
        position: 'relative',
    };

    const thumbStyle = {
        position: 'absolute',
        left: value ? '18px' : '2px',
        width: '16px',
        height: '16px',
        borderRadius: '50%',
        background: value ? '#fff' : colors.textTertiary,
        transition: 'left 150ms, background 150ms',
    };

    return html`
        <button
            type="button"
            role="switch"
            aria-checked=${value}
            onClick=${() => onChange(!value)}
            style=${trackStyle}
        >
            <span style=${thumbStyle} />
        </button>
    `;
}

// ---------------------------------------------------------------------------
// FieldLabel — label + help icon
// ---------------------------------------------------------------------------

function FieldLabel({ label, helpText, required = false }) {
    const style = {
        display: 'flex',
        alignItems: 'center',
        marginBottom: '6px',
        fontSize: typography.size.sm,
        fontWeight: typography.weight.medium,
        color: colors.textSecondary,
    };

    return html`
        <label style=${style}>
            ${label}${required ? html`<span style=${{ color: colors.red, marginLeft: '2px' }}>*</span>` : null}
            ${helpText ? html`<${HelpIcon} text=${helpText} />` : null}
        </label>
    `;
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

function validate(state) {
    const errors = [];
    const fieldErrors = {};

    if (!state.id.trim()) {
        errors.push('Project ID is required');
        fieldErrors.id = true;
    } else if (!/^[a-z0-9][a-z0-9-]*$/.test(state.id.trim())) {
        errors.push('Project ID must start with alphanumeric and contain only lowercase letters, numbers, and hyphens');
        fieldErrors.id = true;
    }

    if (!state.repo.trim()) {
        errors.push('Repository URL is required');
        fieldErrors.repo = true;
    } else if (!state.repo.trim().startsWith('https://') && !state.repo.trim().startsWith('git@')) {
        errors.push('Repository URL must start with "https://" or "git@"');
        fieldErrors.repo = true;
    }

    if (!state.defaultBranch.trim()) {
        errors.push('Default Branch is required');
        fieldErrors.defaultBranch = true;
    }

    if (!state.maxTurns || isNaN(parseInt(state.maxTurns)) || parseInt(state.maxTurns) <= 0) {
        errors.push('Max Turns must be a positive integer');
        fieldErrors.maxTurns = true;
    }

    if (!state.maxWallClock || isNaN(parseInt(state.maxWallClock)) || parseInt(state.maxWallClock) <= 0) {
        errors.push('Max Wall Clock must be a positive integer');
        fieldErrors.maxWallClock = true;
    }

    if (state.autoPr && state.autoMerge) {
        errors.push('Auto PR and Auto Merge cannot both be enabled');
        fieldErrors.autoPr = true;
        fieldErrors.autoMerge = true;
    }

    return { errors, fieldErrors };
}

// ---------------------------------------------------------------------------
// ProjectCreateView
// ---------------------------------------------------------------------------

export function ProjectCreateView() {
    // Required fields
    const [id, setId] = useState('');
    const [repo, setRepo] = useState('');
    const [defaultBranch, setDefaultBranch] = useState('main');
    const [model, setModel] = useState('claude-sonnet-4-6');
    const [reviewModel, setReviewModel] = useState('claude-opus-4-6');
    const [autoTest, setAutoTest] = useState(true);
    const [autoReview, setAutoReview] = useState(true);
    const [autoPr, setAutoPr] = useState(false);
    const [autoMerge, setAutoMerge] = useState(false);
    const [maxTurns, setMaxTurns] = useState('200');
    const [maxWallClock, setMaxWallClock] = useState('60');

    // Optional fields
    const [optionalOpen, setOptionalOpen] = useState(false);
    const [testCommand, setTestCommand] = useState('');
    const [setupCommand, setSetupCommand] = useState('');
    const [teardownCommand, setTeardownCommand] = useState('');
    const [ignorePatterns, setIgnorePatterns] = useState('');
    const [envOverrides, setEnvOverrides] = useState([]);

    // Form state
    const [submitting, setSubmitting] = useState(false);
    const [validationErrors, setValidationErrors] = useState([]);
    const [fieldErrors, setFieldErrors] = useState({});
    const [serverError, setServerError] = useState(null);

    // Mutual exclusion handlers
    const handleAutoPr = (v) => {
        setAutoPr(v);
        if (v) setAutoMerge(false);
    };

    const handleAutoMerge = (v) => {
        setAutoMerge(v);
        if (v) setAutoPr(false);
    };

    // Env overrides
    const addEnvRow = () => setEnvOverrides(prev => [...prev, { key: '', value: '' }]);
    const removeEnvRow = (i) => setEnvOverrides(prev => prev.filter((_, idx) => idx !== i));
    const updateEnvKey = (i, k) => setEnvOverrides(prev => prev.map((r, idx) => idx === i ? { ...r, key: k } : r));
    const updateEnvVal = (i, v) => setEnvOverrides(prev => prev.map((r, idx) => idx === i ? { ...r, value: v } : r));

    const handleSubmit = async (e) => {
        e.preventDefault();
        setServerError(null);

        const state = { id, repo, defaultBranch, model, reviewModel, autoTest, autoReview, autoPr, autoMerge, maxTurns, maxWallClock };
        const { errors, fieldErrors: fe } = validate(state);

        if (errors.length > 0) {
            setValidationErrors(errors);
            setFieldErrors(fe);
            return;
        }

        setValidationErrors([]);
        setFieldErrors({});
        setSubmitting(true);

        try {
            const payload = {
                id: id.trim(),
                repo: repo.trim(),
                default_branch: defaultBranch.trim(),
                model,
                review_model: reviewModel,
                auto_test: autoTest,
                auto_review: autoReview,
                auto_pr: autoPr,
                auto_merge: autoMerge,
                max_turns: parseInt(maxTurns),
                max_wall_clock: parseInt(maxWallClock),
            };

            if (testCommand.trim()) payload.test_command = testCommand.trim();
            if (setupCommand.trim()) payload.setup_command = setupCommand.trim();
            if (teardownCommand.trim()) payload.teardown_command = teardownCommand.trim();
            if (ignorePatterns.trim()) {
                payload.review_ignore_patterns = ignorePatterns.split(',').map(p => p.trim()).filter(Boolean);
            }
            const validEnv = envOverrides.filter(r => r.key.trim());
            if (validEnv.length > 0) {
                payload.env_overrides = Object.fromEntries(validEnv.map(r => [r.key.trim(), r.value]));
            }

            await api.createProject(payload);
            navigate(`/project/${payload.id}`);
        } catch (err) {
            setServerError(err.message || 'Failed to create project');
            setSubmitting(false);
        }
    };

    // ---------------------------------------------------------------------------
    // Styles
    // ---------------------------------------------------------------------------

    const pageStyle = {
        maxWidth: '720px',
        margin: '0 auto',
        display: 'flex',
        flexDirection: 'column',
        gap: '28px',
        paddingBottom: '48px',
    };

    const headerStyle = {
        paddingBottom: '20px',
        borderBottom: `1px solid ${colors.border}`,
    };

    const titleStyle = {
        fontFamily: typography.fontBody,
        fontSize: typography.size['2xl'],
        fontWeight: typography.weight.semibold,
        color: colors.text,
        margin: '0 0 6px',
        letterSpacing: '-0.02em',
    };

    const subtitleStyle = {
        fontSize: typography.size.sm,
        color: colors.textTertiary,
        margin: 0,
    };

    const sectionStyle = {
        display: 'flex',
        flexDirection: 'column',
        gap: '16px',
    };

    const rowStyle = {
        display: 'grid',
        gridTemplateColumns: '1fr 1fr',
        gap: '16px',
    };

    const fieldStyle = {
        display: 'flex',
        flexDirection: 'column',
    };

    const inputBase = {
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.md,
        color: colors.text,
        fontSize: typography.size.sm,
        padding: '8px 10px',
        outline: 'none',
        width: '100%',
        boxSizing: 'border-box',
        transition: 'border-color 150ms',
    };

    const inputError = {
        ...inputBase,
        borderColor: colors.red,
    };

    const inputMono = {
        ...inputBase,
        fontFamily: typography.fontMono,
        fontSize: typography.size.sm,
    };

    const inputMonoError = {
        ...inputMono,
        borderColor: colors.red,
    };

    const selectStyle = {
        ...inputBase,
        cursor: 'pointer',
        appearance: 'none',
        backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath fill='%238a93a2' d='M6 8L0 0h12z'/%3E%3C/svg%3E")`,
        backgroundRepeat: 'no-repeat',
        backgroundPosition: 'right 10px center',
        paddingRight: '28px',
    };

    const cardStyle = {
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.lg,
        padding: '16px 20px',
        display: 'flex',
        flexDirection: 'column',
        gap: '12px',
    };

    const toggleRowStyle = {
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'space-between',
        gap: '16px',
    };

    const toggleLabelColStyle = {
        display: 'flex',
        flexDirection: 'column',
        gap: '2px',
        flex: 1,
    };

    const toggleLabelStyle = {
        display: 'flex',
        alignItems: 'center',
        fontSize: typography.size.sm,
        fontWeight: typography.weight.medium,
        color: colors.textSecondary,
    };

    const mutualExclusiveStyle = {
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        fontStyle: 'italic',
    };

    const errorBannerStyle = {
        background: colors.redBg,
        border: `1px solid ${colors.red}44`,
        borderRadius: layout.borderRadius.md,
        padding: '12px 16px',
        color: colors.red,
        fontSize: typography.size.sm,
    };

    const footerStyle = {
        display: 'flex',
        justifyContent: 'flex-end',
        gap: '10px',
        paddingTop: '8px',
        borderTop: `1px solid ${colors.border}`,
    };

    const cancelBtnStyle = {
        padding: '8px 20px',
        borderRadius: layout.borderRadius.md,
        background: 'transparent',
        border: `1px solid ${colors.border}`,
        color: colors.textSecondary,
        fontSize: typography.size.sm,
        cursor: 'pointer',
        fontFamily: typography.fontBody,
    };

    const submitBtnStyle = {
        padding: '8px 20px',
        borderRadius: layout.borderRadius.md,
        background: colors.accent,
        border: `1px solid ${colors.accent}`,
        color: '#fff',
        fontSize: typography.size.sm,
        fontWeight: typography.weight.medium,
        cursor: submitting ? 'not-allowed' : 'pointer',
        opacity: submitting ? 0.7 : 1,
        fontFamily: typography.fontBody,
    };

    const optionalHeaderStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        cursor: 'pointer',
        userSelect: 'none',
        padding: '10px 0',
        borderTop: `1px solid ${colors.border}`,
        color: colors.textTertiary,
        fontSize: typography.size.sm,
        fontWeight: typography.weight.medium,
        letterSpacing: '0.06em',
        background: 'none',
        border: 'none',
        width: '100%',
        textAlign: 'left',
    };

    const sectionLabelStyle = {
        fontFamily: typography.fontBody,
        fontSize: typography.size.xs,
        fontWeight: typography.weight.semibold,
        color: colors.textTertiary,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        marginBottom: '4px',
    };

    const numberInputStyle = (hasErr) => ({
        ...(hasErr ? inputError : inputBase),
        width: '100%',
        boxSizing: 'border-box',
    });

    const allErrors = [...validationErrors, ...(serverError ? [serverError] : [])];

    return html`
        <style>${`
            .help-icon-wrap:hover .help-icon-tooltip { display: block !important; }
            .help-icon-trigger { user-select: none; }
            input:focus, select:focus, textarea:focus {
                border-color: ${colors.accent} !important;
                box-shadow: 0 0 0 2px ${colors.accent}22;
            }
            button[type="button"]:focus { outline: none; }
            .foreman-input-mono::placeholder { color: ${colors.textTertiary}; font-family: ${typography.fontMono}; }
            .foreman-input::placeholder { color: ${colors.textTertiary}; }
        `}</style>

        <div style=${pageStyle}>
            <div style=${headerStyle}>
                <h1 style=${titleStyle}>New Project</h1>
                <p style=${subtitleStyle}>Register a git repo. All config fields are required.</p>
            </div>

            ${allErrors.length > 0 ? html`
                <div style=${errorBannerStyle}>
                    ${allErrors.length === 1 ? html`<span>${allErrors[0]}</span>` : html`
                        <ul style=${{ margin: '0', paddingLeft: '18px' }}>
                            ${allErrors.map((e, i) => html`<li key=${i}>${e}</li>`)}
                        </ul>
                    `}
                </div>
            ` : null}

            <form onSubmit=${handleSubmit} style=${sectionStyle} novalidate>

                <!-- Row 1: Project ID + Repo URL -->
                <div style=${rowStyle}>
                    <div style=${fieldStyle}>
                        <${FieldLabel} label="Project ID" required=${true} helpText="Short unique slug used in task IDs and URLs. Lowercase, hyphens only." />
                        <input
                            class="foreman-input-mono"
                            type="text"
                            value=${id}
                            onInput=${e => setId(e.target.value)}
                            style=${fieldErrors.id ? inputMonoError : inputMono}
                            placeholder="my-project"
                            spellcheck="false"
                            autocomplete="off"
                        />
                    </div>
                    <div style=${fieldStyle}>
                        <${FieldLabel} label="Repository URL" required=${true} helpText="HTTPS clone URL. SSH URLs are auto-converted on the server." />
                        <input
                            class="foreman-input-mono"
                            type="text"
                            value=${repo}
                            onInput=${e => setRepo(e.target.value)}
                            style=${fieldErrors.repo ? inputMonoError : inputMono}
                            placeholder="https://github.com/org/repo.git"
                            spellcheck="false"
                            autocomplete="off"
                        />
                    </div>
                </div>

                <!-- Row 2: Default Branch (full width) -->
                <div style=${fieldStyle}>
                    <${FieldLabel} label="Default Branch" required=${true} helpText="The main branch tasks fork from and merge back into. Also the inherited default for task base_branch and branch_target." />
                    <input
                        class="foreman-input-mono"
                        type="text"
                        value=${defaultBranch}
                        onInput=${e => setDefaultBranch(e.target.value)}
                        style=${fieldErrors.defaultBranch ? inputMonoError : inputMono}
                        spellcheck="false"
                        autocomplete="off"
                    />
                </div>

                <!-- Row 3: Model + Review Model -->
                <div style=${rowStyle}>
                    <div style=${fieldStyle}>
                        <${FieldLabel} label="Model" required=${true} helpText="Default Claude model for worker sessions. Tasks can override." />
                        <select
                            value=${model}
                            onChange=${e => setModel(e.target.value)}
                            style=${selectStyle}
                        >
                            <option value="claude-sonnet-4-6">Sonnet — faster, cheaper</option>
                            <option value="claude-opus-4-6">Opus — more capable</option>
                        </select>
                    </div>
                    <div style=${fieldStyle}>
                        <${FieldLabel} label="Review Model" required=${true} helpText="Default model for automated code reviews after tests pass." />
                        <select
                            value=${reviewModel}
                            onChange=${e => setReviewModel(e.target.value)}
                            style=${selectStyle}
                        >
                            <option value="claude-sonnet-4-6">Sonnet — faster, cheaper</option>
                            <option value="claude-opus-4-6">Opus — recommended</option>
                        </select>
                    </div>
                </div>

                <!-- Gate Defaults card -->
                <div>
                    <div style=${sectionLabelStyle}>Gate Defaults</div>
                    <div style=${cardStyle}>
                        <div style=${toggleRowStyle}>
                            <div style=${toggleLabelColStyle}>
                                <span style=${toggleLabelStyle}>
                                    Auto Test
                                    <${HelpIcon} text="Run test command after CC completes. If tests fail, CC is retried." />
                                </span>
                            </div>
                            <${Toggle} value=${autoTest} onChange=${setAutoTest} />
                        </div>
                        <div style=${toggleRowStyle}>
                            <div style=${toggleLabelColStyle}>
                                <span style=${toggleLabelStyle}>
                                    Auto Review
                                    <${HelpIcon} text="Dispatch reviewer after tests pass." />
                                </span>
                            </div>
                            <${Toggle} value=${autoReview} onChange=${setAutoReview} />
                        </div>
                        <div style=${toggleRowStyle}>
                            <div style=${toggleLabelColStyle}>
                                <span style=${toggleLabelStyle}>
                                    Auto PR
                                    <${HelpIcon} text="Create GitHub PR when all gates pass. Mutually exclusive with Auto Merge." />
                                </span>
                                <span style=${mutualExclusiveStyle}>Mutually exclusive with Auto Merge</span>
                            </div>
                            <${Toggle} value=${autoPr} onChange=${handleAutoPr} />
                        </div>
                        <div style=${toggleRowStyle}>
                            <div style=${toggleLabelColStyle}>
                                <span style=${toggleLabelStyle}>
                                    Auto Merge
                                    <${HelpIcon} text="Merge branch when all gates pass. Mutually exclusive with Auto PR." />
                                </span>
                                <span style=${mutualExclusiveStyle}>Mutually exclusive with Auto PR</span>
                            </div>
                            <${Toggle} value=${autoMerge} onChange=${handleAutoMerge} />
                        </div>
                    </div>
                </div>

                <!-- Row 4: Max Turns + Max Wall Clock -->
                <div style=${rowStyle}>
                    <div style=${fieldStyle}>
                        <${FieldLabel} label="Max Turns" required=${true} helpText="Maximum conversation turns per task. Higher = more autonomy and cost." />
                        <input
                            type="number"
                            min="1"
                            value=${maxTurns}
                            onInput=${e => setMaxTurns(e.target.value)}
                            style=${numberInputStyle(fieldErrors.maxTurns)}
                        />
                    </div>
                    <div style=${fieldStyle}>
                        <${FieldLabel} label="Max Wall Clock (minutes)" required=${true} helpText="Maximum time in minutes per task. Task pauses when exceeded." />
                        <input
                            type="number"
                            min="1"
                            value=${maxWallClock}
                            onInput=${e => setMaxWallClock(e.target.value)}
                            style=${numberInputStyle(fieldErrors.maxWallClock)}
                        />
                    </div>
                </div>

                <!-- Optional section (collapsible) -->
                <div>
                    <button
                        type="button"
                        onClick=${() => setOptionalOpen(o => !o)}
                        style=${{
                            display: 'flex',
                            alignItems: 'center',
                            gap: '6px',
                            cursor: 'pointer',
                            userSelect: 'none',
                            padding: '10px 0',
                            borderTop: `1px solid ${colors.border}`,
                            borderBottom: optionalOpen ? `1px solid ${colors.border}` : 'none',
                            borderLeft: 'none',
                            borderRight: 'none',
                            color: colors.textTertiary,
                            fontSize: typography.size.sm,
                            fontWeight: typography.weight.semibold,
                            letterSpacing: '0.08em',
                            background: 'none',
                            width: '100%',
                            textAlign: 'left',
                        }}
                    >
                        <span>${optionalOpen ? '▾' : '▸'}</span>
                        <span>OPTIONAL</span>
                    </button>

                    ${optionalOpen ? html`
                        <div style=${{ ...sectionStyle, marginTop: '16px' }}>
                            <div style=${fieldStyle}>
                                <${FieldLabel} label="Test Command" helpText="Shell command for the test suite. Example: 'pytest -v', 'npm test'. Leave blank if no automated tests." />
                                <input
                                    class="foreman-input-mono"
                                    type="text"
                                    value=${testCommand}
                                    onInput=${e => setTestCommand(e.target.value)}
                                    style=${inputMono}
                                    placeholder="pytest -v"
                                    spellcheck="false"
                                />
                            </div>
                            <div style=${fieldStyle}>
                                <${FieldLabel} label="Setup Command" helpText="Runs in each new worktree after creation. Example: 'npm install', 'pip install -r requirements.txt'." />
                                <input
                                    class="foreman-input-mono"
                                    type="text"
                                    value=${setupCommand}
                                    onInput=${e => setSetupCommand(e.target.value)}
                                    style=${inputMono}
                                    placeholder="npm install"
                                    spellcheck="false"
                                />
                            </div>
                            <div style=${fieldStyle}>
                                <${FieldLabel} label="Review Ignore Patterns" helpText="File glob patterns to exclude from reviewer diffs. Comma-separated. Example: '*.lock, vendor/, node_modules/'" />
                                <input
                                    class="foreman-input"
                                    type="text"
                                    value=${ignorePatterns}
                                    onInput=${e => setIgnorePatterns(e.target.value)}
                                    style=${inputBase}
                                    placeholder="*.lock, vendor/, node_modules/"
                                    spellcheck="false"
                                />
                            </div>
                            <div style=${fieldStyle}>
                                <${FieldLabel} label="Env Overrides" helpText="Environment variables appended to .env.testing in each worktree." />
                                <div style=${{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                                    ${envOverrides.map((row, i) => html`
                                        <div key=${i} style=${{ display: 'grid', gridTemplateColumns: '1fr 1fr auto', gap: '8px', alignItems: 'center' }}>
                                            <input
                                                type="text"
                                                value=${row.key}
                                                onInput=${e => updateEnvKey(i, e.target.value)}
                                                style=${inputMono}
                                                placeholder="KEY"
                                                spellcheck="false"
                                                autocomplete="off"
                                            />
                                            <input
                                                type="text"
                                                value=${row.value}
                                                onInput=${e => updateEnvVal(i, e.target.value)}
                                                style=${inputMono}
                                                placeholder="value"
                                                spellcheck="false"
                                                autocomplete="off"
                                            />
                                            <button
                                                type="button"
                                                onClick=${() => removeEnvRow(i)}
                                                style=${{
                                                    background: 'none',
                                                    border: `1px solid ${colors.border}`,
                                                    borderRadius: layout.borderRadius.sm,
                                                    color: colors.textTertiary,
                                                    cursor: 'pointer',
                                                    padding: '4px 8px',
                                                    fontSize: typography.size.sm,
                                                    lineHeight: 1,
                                                }}
                                                title="Remove"
                                            >×</button>
                                        </div>
                                    `)}
                                    <button
                                        type="button"
                                        onClick=${addEnvRow}
                                        style=${{
                                            alignSelf: 'flex-start',
                                            background: 'none',
                                            border: `1px solid ${colors.border}`,
                                            borderRadius: layout.borderRadius.md,
                                            color: colors.textSecondary,
                                            cursor: 'pointer',
                                            padding: '5px 12px',
                                            fontSize: typography.size.sm,
                                            fontFamily: typography.fontBody,
                                        }}
                                    >+ Add</button>
                                </div>
                            </div>
                            <div style=${fieldStyle}>
                                <${FieldLabel} label="Teardown Command" helpText="Runs when a worktree is cleaned up. Rarely needed." />
                                <input
                                    class="foreman-input-mono"
                                    type="text"
                                    value=${teardownCommand}
                                    onInput=${e => setTeardownCommand(e.target.value)}
                                    style=${inputMono}
                                    placeholder="make clean"
                                    spellcheck="false"
                                />
                            </div>
                        </div>
                    ` : null}
                </div>

                <!-- Footer -->
                <div style=${footerStyle}>
                    <button
                        type="button"
                        onClick=${() => navigate('/')}
                        style=${cancelBtnStyle}
                    >Cancel</button>
                    <button
                        type="submit"
                        disabled=${submitting}
                        style=${submitBtnStyle}
                    >${submitting ? 'Creating…' : 'Create Project'}</button>
                </div>
            </form>
        </div>
    `;
}
