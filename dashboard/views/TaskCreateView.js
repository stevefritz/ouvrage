// TaskCreateView — Create a new task (held by default)
// Route: #/task/new[?project=:id]
// Reads sessionStorage['foreman-punchlist-scaffold'] on mount for pre-fill.

import { h } from 'https://esm.sh/preact@10.25.4';
import { useState, useEffect, useRef, useCallback } from 'https://esm.sh/preact@10.25.4/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, layout, animation } from '../tokens.js';
import { routes, navigate } from '../router.js';
import { api } from '../api.js';

const html = htm.bind(h);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function slugify(text) {
    return text
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '')
        .slice(0, 60);
}

// ---------------------------------------------------------------------------
// Reusable sub-components
// ---------------------------------------------------------------------------

function FieldLabel({ label, tooltip }) {
    const [showTip, setShowTip] = useState(false);
    const tipRef = useRef(null);

    return html`
        <div style=${{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px' }}>
            <label style=${{
                fontSize: typography.size.sm,
                fontWeight: typography.weight.medium,
                color: colors.textSecondary,
            }}>${label}</label>
            ${tooltip ? html`
                <span
                    style=${{
                        display: 'inline-flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        width: '16px',
                        height: '16px',
                        borderRadius: '50%',
                        border: `1px solid ${colors.border}`,
                        fontSize: '10px',
                        color: colors.textTertiary,
                        cursor: 'pointer',
                        position: 'relative',
                    }}
                    onMouseEnter=${() => setShowTip(true)}
                    onMouseLeave=${() => setShowTip(false)}
                >?
                    ${showTip ? html`
                        <div ref=${tipRef} style=${{
                            position: 'absolute',
                            bottom: '120%',
                            left: '50%',
                            transform: 'translateX(-50%)',
                            background: colors.surface,
                            border: `1px solid ${colors.border}`,
                            borderRadius: layout.borderRadius.md,
                            padding: '8px 10px',
                            fontSize: typography.size.xs,
                            color: colors.textSecondary,
                            whiteSpace: 'normal',
                            width: '220px',
                            zIndex: 100,
                            pointerEvents: 'none',
                            lineHeight: typography.lineHeight.relaxed,
                        }}>${tooltip}</div>
                    ` : null}
                </span>
            ` : null}
        </div>
    `;
}

function TriState({ value, onChange, label, tooltip, inheritHint }) {
    const btnBase = {
        padding: '3px 10px',
        fontSize: typography.size.xs,
        border: `1px solid ${colors.border}`,
        cursor: 'pointer',
        fontFamily: typography.fontBody,
        transition: `background ${animation.durationFast}`,
    };

    const makeBtn = (val, text) => {
        const active = value === val;
        return html`
            <button
                type="button"
                onClick=${() => onChange(val)}
                style=${{
                    ...btnBase,
                    background: active ? colors.blue : colors.surface,
                    color: active ? '#fff' : colors.textSecondary,
                    borderColor: active ? colors.blue : colors.border,
                    fontWeight: active ? typography.weight.medium : typography.weight.normal,
                    borderRadius: val === 'inherit'
                        ? `${layout.borderRadius.sm} 0 0 ${layout.borderRadius.sm}`
                        : val === 'off'
                            ? `0 ${layout.borderRadius.sm} ${layout.borderRadius.sm} 0`
                            : '0',
                }}
            >${text}</button>
        `;
    };

    return html`
        <div style=${{ marginBottom: '16px' }}>
            <${FieldLabel} label=${label} tooltip=${tooltip} />
            <div style=${{ display: 'flex', alignItems: 'center', gap: '0' }}>
                <div style=${{ display: 'flex' }}>
                    ${makeBtn('inherit', 'inherit')}
                    ${makeBtn('on', 'on')}
                    ${makeBtn('off', 'off')}
                </div>
                ${value === 'inherit' && inheritHint ? html`
                    <span style=${{
                        marginLeft: '10px',
                        fontSize: typography.size.xs,
                        color: colors.textTertiary,
                        fontStyle: 'italic',
                    }}>${inheritHint}</span>
                ` : null}
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const inputStyle = {
    width: '100%',
    padding: '8px 10px',
    background: colors.surface,
    border: `1px solid ${colors.border}`,
    borderRadius: layout.borderRadius.md,
    color: colors.text,
    fontSize: typography.size.sm,
    fontFamily: typography.fontBody,
    outline: 'none',
    boxSizing: 'border-box',
    transition: `border-color ${animation.durationFast}`,
};

const inputErrorStyle = {
    ...inputStyle,
    borderColor: colors.red,
};

const monoInputStyle = {
    ...inputStyle,
    fontFamily: typography.fontMono,
    fontSize: typography.size.sm,
};

const cardStyle = {
    background: colors.surface,
    border: `1px solid ${colors.border}`,
    borderRadius: layout.borderRadius.lg,
    padding: '24px',
};

const sectionTitleStyle = {
    fontSize: typography.size.xs,
    fontWeight: typography.weight.semibold,
    color: colors.textTertiary,
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
    marginBottom: '16px',
};

const fieldStyle = {
    marginBottom: '16px',
};

// ---------------------------------------------------------------------------
// TaskCreateView
// ---------------------------------------------------------------------------

export function TaskCreateView({ project: initialProject }) {
    // ---- Projects data ----
    const [projects, setProjects] = useState([]);
    const [projectData, setProjectData] = useState(null);

    // ---- Core fields ----
    const [selectedProject, setSelectedProject] = useState(initialProject || '');
    const [taskId, setTaskId] = useState('');
    const [goal, setGoal] = useState('');
    const [spec, setSpec] = useState('');
    const [checklist, setChecklist] = useState(['', '']);

    // ---- Config fields ----
    const [configOpen, setConfigOpen] = useState(false);
    const [model, setModel] = useState('inherit');
    const [reviewModel, setReviewModel] = useState('inherit');
    const [autoTest, setAutoTest] = useState('inherit');
    const [autoReview, setAutoReview] = useState('inherit');
    const [autoPr, setAutoPr] = useState('inherit');
    const [autoMerge, setAutoMerge] = useState('inherit');
    const [maxTurns, setMaxTurns] = useState('');
    const [maxWallClock, setMaxWallClock] = useState('');
    const [maxTestRetries, setMaxTestRetries] = useState('');
    const [maxReviewRetries, setMaxReviewRetries] = useState('');
    const [dependsOn, setDependsOn] = useState('');
    const [dependsOnCandidates, setDependsOnCandidates] = useState([]);
    const [dependsOnSearch, setDependsOnSearch] = useState('');
    const [dependsOnOpen, setDependsOnOpen] = useState(false);
    const dependsOnRef = useRef(null);
    const [baseBranch, setBaseBranch] = useState('');
    const [tags, setTags] = useState('');
    const [escalationCriteria, setEscalationCriteria] = useState('');
    const [held, setHeld] = useState(true);

    // ---- Form state ----
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState(null);
    const [fieldErrors, setFieldErrors] = useState({});

    // ---- Load projects on mount ----
    useEffect(() => {
        api.getProjects().then(data => {
            setProjects(data || []);
        }).catch(() => {});
    }, []);

    // ---- When project changes, fetch project data ----
    useEffect(() => {
        if (!selectedProject) {
            setProjectData(null);
            return;
        }
        api.getProject(selectedProject).then(data => {
            setProjectData(data);
        }).catch(() => setProjectData(null));
    }, [selectedProject]);

    // ---- Fetch depends-on candidates when project changes ----
    useEffect(() => {
        if (!selectedProject) {
            setDependsOnCandidates([]);
            return;
        }
        fetch(`/dashboard/api/tasks/depends-on-candidates?project_id=${encodeURIComponent(selectedProject)}`)
            .then(r => r.ok ? r.json() : [])
            .then(data => setDependsOnCandidates(data || []))
            .catch(() => setDependsOnCandidates([]));
    }, [selectedProject]);

    // ---- Close depends-on dropdown on outside click ----
    useEffect(() => {
        const handler = (e) => {
            if (dependsOnRef.current && !dependsOnRef.current.contains(e.target)) {
                setDependsOnOpen(false);
            }
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, []);

    // ---- Auto-suggest task ID from goal on blur ----
    const handleGoalBlur = useCallback(() => {
        if (!taskId.trim() && goal.trim()) {
            const projectPrefix = selectedProject ? selectedProject + '/' : '';
            setTaskId(projectPrefix + slugify(goal.trim()));
        }
    }, [taskId, goal, selectedProject]);

    // ---- Auto-prefix task ID when project changes (only if it looks like it needs it) ----
    useEffect(() => {
        if (selectedProject && taskId && !taskId.startsWith(selectedProject + '/')) {
            // Only update if the task id has no slash (bare slug)
            if (!taskId.includes('/')) {
                setTaskId(selectedProject + '/' + taskId);
            }
        }
    }, [selectedProject]); // eslint-disable-line react-hooks/exhaustive-deps

    // ---- Auto PR / Auto Merge mutual exclusion ----
    const handleAutoPrChange = (val) => {
        setAutoPr(val);
        if (val === 'on') setAutoMerge('inherit');
    };

    const handleAutoMergeChange = (val) => {
        setAutoMerge(val);
        if (val === 'on') setAutoPr('inherit');
    };

    // ---- Depends_on selection ----
    const handleDependsOnSelect = (candidate) => {
        setDependsOn(candidate.id);
        setDependsOnSearch('');
        setDependsOnOpen(false);
        setHeld(false);
    };
    const handleDependsOnClear = () => {
        setDependsOn('');
        setDependsOnSearch('');
    };
    const filteredCandidates = dependsOnCandidates.filter(c => {
        if (!dependsOnSearch) return true;
        const q = dependsOnSearch.toLowerCase();
        return c.id.toLowerCase().includes(q) || (c.goal || '').toLowerCase().includes(q);
    });

    // ---- Checklist helpers ----
    const addChecklistItem = () => setChecklist(cl => [...cl, '']);
    const removeChecklistItem = (i) => setChecklist(cl => cl.filter((_, idx) => idx !== i));
    const updateChecklistItem = (i, val) => setChecklist(cl => cl.map((c, idx) => idx === i ? val : c));

    // ---- Submit ----
    const handleSubmit = async (e) => {
        e.preventDefault();
        setError(null);
        setFieldErrors({});

        const errors = {};
        if (!selectedProject.trim()) errors.project = 'Required';
        if (!taskId.trim()) errors.taskId = 'Required';
        if (!goal.trim()) errors.goal = 'Required';
        if (Object.keys(errors).length > 0) {
            setFieldErrors(errors);
            return;
        }

        setSubmitting(true);

        const payload = {
            project_id: selectedProject,
            id: taskId.trim(),
            goal: goal.trim(),
            spec: spec.trim() || undefined,
            checklist: checklist.filter(c => c.trim()),
            held,
            model: model !== 'inherit' ? model : undefined,
            review_model: reviewModel !== 'inherit' ? reviewModel : undefined,
            auto_test: autoTest !== 'inherit' ? (autoTest === 'on') : undefined,
            auto_review: autoReview !== 'inherit' ? (autoReview === 'on') : undefined,
            auto_pr: autoPr !== 'inherit' ? (autoPr === 'on') : undefined,
            auto_merge: autoMerge !== 'inherit' ? (autoMerge === 'on') : undefined,
            max_turns: maxTurns ? parseInt(maxTurns, 10) : undefined,
            max_wall_clock: maxWallClock ? parseInt(maxWallClock, 10) : undefined,
            max_test_retries: maxTestRetries ? parseInt(maxTestRetries, 10) : undefined,
            max_review_retries: maxReviewRetries ? parseInt(maxReviewRetries, 10) : undefined,
            depends_on: dependsOn.trim() || undefined,
            base_branch: baseBranch.trim() || undefined,
            tags: tags ? tags.split(',').map(t => t.trim()).filter(Boolean) : undefined,
            escalation_criteria: escalationCriteria.trim() || undefined,
        };

        // Remove undefined keys
        Object.keys(payload).forEach(k => payload[k] === undefined && delete payload[k]);

        try {
            await api.createTask(payload);
            navigate(`/task/${encodeURIComponent(payload.id)}`);
        } catch (err) {
            setError(err.message || 'Failed to create task');
            setSubmitting(false);
        }
    };

    // ---- Inherited hints from project config ----
    const hint = (field, fallback) => {
        if (!projectData) return fallback ? `inherit (${fallback})` : 'inherit';
        const val = projectData[field];
        if (val === null || val === undefined) return fallback ? `inherit (${fallback})` : 'inherit';
        return `inheriting: ${val}`;
    };

    const triHint = (field, fallback) => {
        if (!projectData) return fallback;
        const val = projectData[field];
        if (val === null || val === undefined) return fallback;
        return `inheriting: ${val ? 'on' : 'off'}`;
    };

    // ---- Render ----
    const pageStyle = {
        display: 'flex',
        flexDirection: 'column',
        gap: '24px',
        maxWidth: '720px',
        margin: '0 auto',
    };

    return html`
        <div style=${pageStyle}>
            <!-- Back link -->
            <a
                href=${selectedProject ? routes.project(selectedProject) : routes.landing()}
                style=${{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: '5px',
                    fontSize: typography.size.sm,
                    color: colors.textTertiary,
                    textDecoration: 'none',
                    marginBottom: '-8px',
                }}
            >← ${selectedProject ? selectedProject : 'Projects'}</a>

            <!-- Header -->
            <div>
                <h1 style=${{
                    fontSize: typography.size['2xl'],
                    fontWeight: typography.weight.semibold,
                    color: colors.text,
                    margin: '0 0 6px',
                    letterSpacing: '-0.02em',
                }}>New Task</h1>
                <p style=${{
                    fontSize: typography.size.sm,
                    color: colors.textTertiary,
                    margin: 0,
                }}>Creates held by default. Review the spec, then Approve to dispatch.</p>
            </div>

            <!-- Error banner -->
            ${error ? html`
                <div style=${{
                    padding: '12px 16px',
                    borderRadius: layout.borderRadius.md,
                    background: colors.redBg,
                    border: `1px solid ${colors.red}44`,
                    color: colors.red,
                    fontSize: typography.size.sm,
                }}>${error}</div>
            ` : null}

            <form onSubmit=${handleSubmit} style=${{ display: 'flex', flexDirection: 'column', gap: '24px' }}>

                <!-- Core fields card -->
                <div style=${cardStyle}>
                    <div style=${sectionTitleStyle}>Core Fields</div>

                    <!-- Project -->
                    <div style=${fieldStyle}>
                        <${FieldLabel} label="Project *" tooltip="The project this task belongs to. Determines which git repo, test commands, and inherited config are used." />
                        <select
                            value=${selectedProject}
                            onChange=${e => setSelectedProject(e.target.value)}
                            style=${{
                                ...inputStyle,
                                ...(fieldErrors.project ? { borderColor: colors.red } : {}),
                            }}
                        >
                            <option value="">— select project —</option>
                            ${projects.map(p => html`
                                <option key=${p.id} value=${p.id}>${p.id}</option>
                            `)}
                        </select>
                        ${fieldErrors.project ? html`<div style=${{ color: colors.red, fontSize: typography.size.xs, marginTop: '4px' }}>${fieldErrors.project}</div>` : null}
                    </div>

                    <!-- Goal -->
                    <div style=${fieldStyle}>
                        <${FieldLabel} label="Goal *" tooltip="One-line description of what this task should accomplish. The worker uses this as their primary directive." />
                        <input
                            type="text"
                            value=${goal}
                            onInput=${e => setGoal(e.target.value)}
                            onBlur=${handleGoalBlur}
                            placeholder="What should this task accomplish?"
                            style=${{
                                ...inputStyle,
                                ...(fieldErrors.goal ? { borderColor: colors.red } : {}),
                            }}
                        />
                        ${fieldErrors.goal ? html`<div style=${{ color: colors.red, fontSize: typography.size.xs, marginTop: '4px' }}>${fieldErrors.goal}</div>` : null}
                    </div>

                    <!-- Task ID -->
                    <div style=${fieldStyle}>
                        <${FieldLabel} label="Task ID *" tooltip="Unique identifier for this task. Must be project-scoped (e.g. my-project/fix-auth-bug). Auto-suggested from the goal." />
                        <input
                            type="text"
                            value=${taskId}
                            onInput=${e => setTaskId(e.target.value)}
                            placeholder="${selectedProject ? selectedProject + '/task-id' : 'project/task-id'}"
                            style=${{
                                ...monoInputStyle,
                                ...(fieldErrors.taskId ? { borderColor: colors.red } : {}),
                            }}
                        />
                        ${fieldErrors.taskId ? html`<div style=${{ color: colors.red, fontSize: typography.size.xs, marginTop: '4px' }}>${fieldErrors.taskId}</div>` : null}
                    </div>

                    <!-- Spec -->
                    <div style=${fieldStyle}>
                        <${FieldLabel} label="Spec" tooltip="Full specification in markdown: requirements, constraints, what NOT to do, acceptance criteria. The richer this is, the better the worker performs." />
                        <textarea
                            value=${spec}
                            onInput=${e => setSpec(e.target.value)}
                            placeholder="Full specification — requirements, constraints, what NOT to do..."
                            rows="8"
                            style=${{
                                ...inputStyle,
                                resize: 'vertical',
                                minHeight: '120px',
                                fontFamily: typography.fontMono,
                                fontSize: typography.size.xs,
                                lineHeight: typography.lineHeight.relaxed,
                            }}
                        />
                    </div>

                    <!-- Checklist -->
                    <div style=${fieldStyle}>
                        <${FieldLabel} label="Checklist" tooltip="Deliverables for the worker to check off as they complete them. Shown on the task dashboard." />
                        <div style=${{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                            ${checklist.map((item, i) => html`
                                <div key=${i} style=${{ display: 'flex', gap: '6px', alignItems: 'center' }}>
                                    <input
                                        type="text"
                                        value=${item}
                                        onInput=${e => updateChecklistItem(i, e.target.value)}
                                        placeholder="Checklist item…"
                                        style=${{ ...inputStyle, flex: 1 }}
                                    />
                                    <button
                                        type="button"
                                        onClick=${() => removeChecklistItem(i)}
                                        style=${{
                                            background: 'none',
                                            border: 'none',
                                            color: colors.textTertiary,
                                            cursor: 'pointer',
                                            fontSize: '16px',
                                            padding: '4px 6px',
                                            borderRadius: layout.borderRadius.sm,
                                            flexShrink: 0,
                                        }}
                                        title="Remove item"
                                    >×</button>
                                </div>
                            `)}
                            <button
                                type="button"
                                onClick=${addChecklistItem}
                                style=${{
                                    padding: '7px 12px',
                                    background: 'none',
                                    border: `1px dashed ${colors.border}`,
                                    borderRadius: layout.borderRadius.md,
                                    color: colors.textTertiary,
                                    fontSize: typography.size.sm,
                                    fontFamily: typography.fontBody,
                                    cursor: 'pointer',
                                    textAlign: 'left',
                                    marginTop: '2px',
                                }}
                            >+ Add item</button>
                        </div>
                    </div>
                </div>

                <!-- Configuration card (collapsible) -->
                <div style=${{ ...cardStyle, padding: 0 }}>
                    <div
                        onClick=${() => setConfigOpen(o => !o)}
                        style=${{
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'space-between',
                            padding: '16px 24px',
                            cursor: 'pointer',
                            userSelect: 'none',
                            borderRadius: configOpen ? `${layout.borderRadius.lg} ${layout.borderRadius.lg} 0 0` : layout.borderRadius.lg,
                        }}
                    >
                        <div>
                            <div style=${{
                                fontSize: typography.size.xs,
                                fontWeight: typography.weight.semibold,
                                color: colors.textTertiary,
                                letterSpacing: '0.08em',
                                textTransform: 'uppercase',
                            }}>Configuration</div>
                            ${selectedProject ? html`
                                <div style=${{
                                    fontSize: typography.size.xs,
                                    color: colors.textTertiary,
                                    marginTop: '2px',
                                }}>inheriting from ${selectedProject}</div>
                            ` : null}
                        </div>
                        <span style=${{ color: colors.textTertiary, fontSize: '12px' }}>${configOpen ? '▴' : '▾'}</span>
                    </div>

                    ${configOpen ? html`
                        <div style=${{
                            padding: '0 24px 24px',
                            borderTop: `1px solid ${colors.border}`,
                        }}>
                            <!-- Model -->
                            <div style=${fieldStyle}>
                                <${FieldLabel} label="Model" tooltip="Which Claude model the worker uses. 'inherit' uses the project default." />
                                <div style=${{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                                    <select
                                        value=${model}
                                        onChange=${e => setModel(e.target.value)}
                                        style=${{ ...inputStyle, width: 'auto', minWidth: '160px' }}
                                    >
                                        <option value="inherit">inherit</option>
                                        <option value="claude-sonnet-4-6">sonnet</option>
                                        <option value="claude-opus-4-6">opus</option>
                                    </select>
                                    ${model === 'inherit' ? html`
                                        <span style=${{ fontSize: typography.size.xs, color: colors.textTertiary, fontStyle: 'italic' }}>
                                            (${hint('model', 'sonnet')})
                                        </span>
                                    ` : null}
                                </div>
                            </div>

                            <!-- Review Model -->
                            <div style=${fieldStyle}>
                                <${FieldLabel} label="Review Model" tooltip="Which model reviews the worker's output during the review gate." />
                                <div style=${{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                                    <select
                                        value=${reviewModel}
                                        onChange=${e => setReviewModel(e.target.value)}
                                        style=${{ ...inputStyle, width: 'auto', minWidth: '160px' }}
                                    >
                                        <option value="inherit">inherit</option>
                                        <option value="claude-sonnet-4-6">sonnet</option>
                                        <option value="claude-opus-4-6">opus</option>
                                    </select>
                                    ${reviewModel === 'inherit' ? html`
                                        <span style=${{ fontSize: typography.size.xs, color: colors.textTertiary, fontStyle: 'italic' }}>
                                            (${hint('review_model', 'opus')})
                                        </span>
                                    ` : null}
                                </div>
                            </div>

                            <!-- Auto Test -->
                            <${TriState}
                                value=${autoTest}
                                onChange=${setAutoTest}
                                label="Auto Test"
                                tooltip="Run the test gate automatically after the worker finishes. 'inherit' uses the project default."
                                inheritHint=${triHint('auto_test', 'on')}
                            />

                            <!-- Auto Review -->
                            <${TriState}
                                value=${autoReview}
                                onChange=${setAutoReview}
                                label="Auto Review"
                                tooltip="Run the review gate automatically after the test gate passes."
                                inheritHint=${triHint('auto_review', 'on')}
                            />

                            <!-- Auto PR -->
                            <${TriState}
                                value=${autoPr}
                                onChange=${handleAutoPrChange}
                                label="Auto PR"
                                tooltip="Automatically open a GitHub PR when the task completes. Mutually exclusive with Auto Merge."
                                inheritHint=${triHint('auto_pr', 'off')}
                            />

                            <!-- Auto Merge -->
                            <${TriState}
                                value=${autoMerge}
                                onChange=${handleAutoMergeChange}
                                label="Auto Merge"
                                tooltip="Automatically merge the branch after gates pass. Mutually exclusive with Auto PR."
                                inheritHint=${triHint('auto_merge', 'off')}
                            />

                            <!-- Numeric limits row -->
                            <div style=${{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', marginBottom: '16px' }}>
                                <div>
                                    <${FieldLabel} label="Max Turns" tooltip="Maximum conversation turns the worker can use. Empty = inherit from project (default 200)." />
                                    <input
                                        type="number"
                                        value=${maxTurns}
                                        onInput=${e => setMaxTurns(e.target.value)}
                                        placeholder=${hint('max_turns', '200')}
                                        min="1"
                                        style=${inputStyle}
                                    />
                                </div>
                                <div>
                                    <${FieldLabel} label="Max Wall Clock (min)" tooltip="Maximum minutes the worker session can run. Empty = inherit from project (default 60)." />
                                    <input
                                        type="number"
                                        value=${maxWallClock}
                                        onInput=${e => setMaxWallClock(e.target.value)}
                                        placeholder=${hint('max_wall_clock', '60')}
                                        min="1"
                                        style=${inputStyle}
                                    />
                                </div>
                                <div>
                                    <${FieldLabel} label="Max Test Retries" tooltip="How many times to retry the test gate before giving up." />
                                    <input
                                        type="number"
                                        value=${maxTestRetries}
                                        onInput=${e => setMaxTestRetries(e.target.value)}
                                        placeholder="inherit (3)"
                                        min="0"
                                        style=${inputStyle}
                                    />
                                </div>
                                <div>
                                    <${FieldLabel} label="Max Review Retries" tooltip="How many times to retry the review gate before giving up." />
                                    <input
                                        type="number"
                                        value=${maxReviewRetries}
                                        onInput=${e => setMaxReviewRetries(e.target.value)}
                                        placeholder="inherit (2)"
                                        min="0"
                                        style=${inputStyle}
                                    />
                                </div>
                            </div>

                            <!-- Depends On -->
                            <div style=${fieldStyle} ref=${dependsOnRef}>
                                <${FieldLabel} label="Depends On" tooltip="Task ID of a prerequisite task (in the same project). This task won't start until the dependency's gates pass." />
                                ${dependsOn ? html`
                                    <div style=${{ display: 'flex', alignItems: 'center', gap: '8px', padding: '6px 10px', background: colors.bg2, border: '1px solid ' + colors.border, borderRadius: '6px', fontFamily: typography.mono }}>
                                        <span style=${{ flex: 1, fontSize: typography.size.sm }}>${dependsOn.includes('/') ? dependsOn.split('/').slice(1).join('/') : dependsOn}</span>
                                        <button type="button" onClick=${handleDependsOnClear} style=${{ background: 'none', border: 'none', color: colors.muted, cursor: 'pointer', padding: '2px 4px', fontSize: '14px' }}>✕</button>
                                    </div>
                                ` : html`
                                    <div style=${{ position: 'relative' }}>
                                        <input
                                            type="text"
                                            value=${dependsOnSearch}
                                            onInput=${e => { setDependsOnSearch(e.target.value); setDependsOnOpen(true); }}
                                            onFocus=${() => setDependsOnOpen(true)}
                                            placeholder=${dependsOnCandidates.length ? 'Search tasks...' : 'Select a project first'}
                                            disabled=${!selectedProject || dependsOnCandidates.length === 0}
                                            style=${{ ...monoInputStyle, ...(fieldErrors.depends_on ? { borderColor: colors.red } : {}) }}
                                        />
                                        ${dependsOnOpen && filteredCandidates.length > 0 ? html`
                                            <div style=${{ position: 'absolute', top: '100%', left: 0, right: 0, maxHeight: '200px', overflowY: 'auto', background: colors.bg2, border: '1px solid ' + colors.border, borderRadius: '0 0 6px 6px', zIndex: 10 }}>
                                                ${filteredCandidates.map(c => html`
                                                    <div
                                                        key=${c.id}
                                                        onClick=${() => handleDependsOnSelect(c)}
                                                        style=${{ padding: '6px 10px', cursor: 'pointer', borderBottom: '1px solid ' + colors.border, fontSize: typography.size.sm }}
                                                        onMouseOver=${e => e.currentTarget.style.background = colors.bg3}
                                                        onMouseOut=${e => e.currentTarget.style.background = 'transparent'}
                                                    >
                                                        <div style=${{ fontFamily: typography.mono, fontWeight: 500 }}>${c.id.includes('/') ? c.id.split('/').slice(1).join('/') : c.id}</div>
                                                        ${c.goal ? html`<div style=${{ color: colors.muted, fontSize: typography.size.xs, marginTop: '2px' }}>${c.goal}</div>` : null}
                                                    </div>
                                                `)}
                                            </div>
                                        ` : null}
                                    </div>
                                `}
                                ${fieldErrors.depends_on ? html`<div style=${{ color: colors.red, fontSize: typography.size.xs, marginTop: '4px' }}>${fieldErrors.depends_on}</div>` : null}
                            </div>

                            <!-- Base Branch -->
                            <div style=${fieldStyle}>
                                <${FieldLabel} label="Base Branch" tooltip="Branch to create the worktree from. Empty = inherit from project default." />
                                <input
                                    type="text"
                                    value=${baseBranch}
                                    onInput=${e => setBaseBranch(e.target.value)}
                                    placeholder=${projectData?.default_branch ? `inherit (${projectData.default_branch})` : 'inherit (main)'}
                                    style=${monoInputStyle}
                                />
                            </div>

                            <!-- Tags -->
                            <div style=${fieldStyle}>
                                <${FieldLabel} label="Tags" tooltip="Comma-separated labels for filtering and grouping tasks (e.g. bugfix, auth, frontend)." />
                                <input
                                    type="text"
                                    value=${tags}
                                    onInput=${e => setTags(e.target.value)}
                                    placeholder="bugfix, auth, frontend"
                                    style=${inputStyle}
                                />
                            </div>

                            <!-- Escalation Criteria -->
                            <div style=${fieldStyle}>
                                <${FieldLabel} label="Escalation Criteria" tooltip="Conditions under which the worker should pause and ask for human input rather than proceeding." />
                                <textarea
                                    value=${escalationCriteria}
                                    onInput=${e => setEscalationCriteria(e.target.value)}
                                    placeholder="e.g. If the approach requires deleting more than 10 files, pause and ask."
                                    rows="3"
                                    style=${{
                                        ...inputStyle,
                                        resize: 'vertical',
                                        fontFamily: typography.fontBody,
                                    }}
                                />
                            </div>

                            <!-- Held checkbox -->
                            <div style=${{
                                padding: '12px 14px',
                                background: colors.surfaceHover,
                                borderRadius: layout.borderRadius.md,
                                border: `1px solid ${colors.border}`,
                            }}>
                                <label style=${{
                                    display: 'flex',
                                    alignItems: 'flex-start',
                                    gap: '10px',
                                    cursor: 'pointer',
                                }}>
                                    <input
                                        type="checkbox"
                                        checked=${held}
                                        onChange=${e => setHeld(e.target.checked)}
                                        style=${{ marginTop: '2px', flexShrink: 0 }}
                                    />
                                    <div>
                                        <div style=${{
                                            fontSize: typography.size.sm,
                                            fontWeight: typography.weight.medium,
                                            color: colors.text,
                                        }}>Hold for review before dispatching</div>
                                        <div style=${{
                                            fontSize: typography.size.xs,
                                            color: colors.textTertiary,
                                            marginTop: '3px',
                                            lineHeight: typography.lineHeight.relaxed,
                                        }}>
                                            When checked, task is created but CC doesn't start. Uncheck or use the Approve button later.
                                            ${dependsOn.trim() ? html` <em>(auto-unchecked: has dependency)</em>` : null}
                                        </div>
                                    </div>
                                </label>
                            </div>
                        </div>
                    ` : null}
                </div>

                <!-- Footer actions -->
                <div style=${{
                    display: 'flex',
                    justifyContent: 'flex-end',
                    gap: '12px',
                    paddingBottom: '32px',
                }}>
                    <a
                        href=${selectedProject ? routes.project(selectedProject) : routes.landing()}
                        style=${{
                            padding: '8px 18px',
                            borderRadius: layout.borderRadius.md,
                            background: 'none',
                            border: `1px solid ${colors.border}`,
                            color: colors.textSecondary,
                            fontSize: typography.size.sm,
                            fontFamily: typography.fontBody,
                            cursor: 'pointer',
                            textDecoration: 'none',
                            display: 'inline-block',
                        }}
                    >Cancel</a>
                    <button
                        type="submit"
                        disabled=${submitting}
                        style=${{
                            padding: '8px 20px',
                            borderRadius: layout.borderRadius.md,
                            background: submitting ? colors.surfaceHover : colors.blue,
                            border: 'none',
                            color: submitting ? colors.textTertiary : '#fff',
                            fontSize: typography.size.sm,
                            fontFamily: typography.fontBody,
                            fontWeight: typography.weight.medium,
                            cursor: submitting ? 'not-allowed' : 'pointer',
                            transition: `background ${animation.durationFast}`,
                        }}
                    >${submitting ? 'Creating…' : held ? 'Create Task (Held)' : 'Create Task'}</button>
                </div>
            </form>
        </div>
    `;
}
