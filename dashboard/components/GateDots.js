// Ouvrage — GateDots component
// Compact pipeline visualization: Task → Tests → Review → Advance
// Each stage is a dot colored by its status.

import { h } from 'https://esm.sh/preact@10.25.4';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography } from '../tokens.js';

const html = htm.bind(h);

// Gate pipeline stages in order
const STAGES = [
    { key: 'task',    label: 'Task' },
    { key: 'tests',   label: 'Tests' },
    { key: 'review',  label: 'Review' },
    { key: 'advance', label: 'Advance' },
];

// Map gate_status to which stages are complete/active/pending
function getStageMeta(gateStatus, taskStatus) {
    // Returns { task, tests, review, advance } each with state: 'done' | 'active' | 'pending' | 'failed'
    const isDone   = (s) => s === 'done';
    const isActive = (s) => s === 'active';

    const taskDone = ['completed', 'merged'].includes(taskStatus);

    if (!gateStatus || gateStatus === 'none') {
        return {
            task:    taskDone ? 'done' : (taskStatus === 'working' ? 'active' : 'pending'),
            tests:   'pending',
            review:  'pending',
            advance: 'pending',
        };
    }

    switch (gateStatus) {
        case 'testing':
            return { task: 'done', tests: 'active', review: 'pending', advance: 'pending' };
        case 'test-passed':
            return { task: 'done', tests: 'done', review: 'pending', advance: 'pending' };
        case 'test-failed':
            return { task: 'done', tests: 'failed', review: 'pending', advance: 'pending' };
        case 'reviewing':
            return { task: 'done', tests: 'done', review: 'active', advance: 'pending' };
        case 'review-failed':
            return { task: 'done', tests: 'done', review: 'failed', advance: 'pending' };
        case 'passed':
            return { task: 'done', tests: 'done', review: 'done', advance: 'done' };
        default:
            return { task: 'pending', tests: 'pending', review: 'pending', advance: 'pending' };
    }
}

function stateColor(state) {
    switch (state) {
        case 'done':    return colors.green;
        case 'active':  return colors.yellow;
        case 'failed':  return colors.red;
        case 'pending': return colors.border;
        default:        return colors.border;
    }
}

/**
 * GateDots — pipeline visualization row.
 * Props:
 *   gateStatus  — gate_status string from task (testing, test-passed, reviewing, passed, etc.)
 *   taskStatus  — task status string (working, completed, etc.)
 *   showLabels  — show stage labels below dots (default: false)
 *   size        — dot size in px (default: 7)
 */
export function GateDots({ gateStatus, taskStatus, showLabels = false, size = 7 }) {
    const stages = getStageMeta(gateStatus, taskStatus);

    const containerStyle = {
        display: 'inline-flex',
        alignItems: 'center',
        gap: '3px',
    };

    const connectorStyle = {
        width: '12px',
        height: '1px',
        background: colors.border,
        flexShrink: 0,
    };

    const wrapperStyle = {
        display: 'inline-flex',
        alignItems: 'center',
        flexDirection: showLabels ? 'column' : 'row',
        gap: showLabels ? '6px' : '0',
    };

    const labelStyle = {
        fontFamily: typography.fontMono,
        fontSize: '9px',
        color: colors.textTertiary,
        textAlign: 'center',
        whiteSpace: 'nowrap',
    };

    if (showLabels) {
        return html`
            <div style=${{ display: 'inline-flex', alignItems: 'flex-start', gap: '4px' }}>
                ${STAGES.map((stage, i) => {
                    const state = stages[stage.key];
                    const color = stateColor(state);
                    const isPulsing = state === 'active';
                    return html`
                        <div key=${stage.key} style=${{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '4px' }}>
                            ${i > 0 ? html`<div style=${{ ...connectorStyle, alignSelf: 'center', marginBottom: '10px', display: 'none' }} />` : null}
                            <span
                                class=${isPulsing ? 'ouvrage-status-dot-pulse' : ''}
                                style=${{
                                    width: `${size}px`,
                                    height: `${size}px`,
                                    borderRadius: '50%',
                                    background: color,
                                    display: 'block',
                                    flexShrink: 0,
                                }}
                                title=${`${stage.label}: ${state}`}
                            />
                            <span style=${labelStyle}>${stage.label}</span>
                        </div>
                        ${i < STAGES.length - 1 ? html`<span style=${{ ...connectorStyle, marginTop: `-${Math.floor(size / 2) + 10}px` }} />` : null}
                    `;
                })}
            </div>
        `;
    }

    return html`
        <span style=${containerStyle} title=${`Gate: ${gateStatus || 'none'}`}>
            ${STAGES.map((stage, i) => {
                const state = stages[stage.key];
                const color = stateColor(state);
                const isPulsing = state === 'active';
                return html`
                    ${i > 0 ? html`<span style=${connectorStyle} />` : null}
                    <span
                        key=${stage.key}
                        class=${isPulsing ? 'ouvrage-status-dot-pulse' : ''}
                        style=${{
                            width: `${size}px`,
                            height: `${size}px`,
                            borderRadius: '50%',
                            background: color,
                            display: 'inline-block',
                            flexShrink: 0,
                        }}
                        title=${`${stage.label}: ${state}`}
                    />
                `;
            })}
        </span>
    `;
}
