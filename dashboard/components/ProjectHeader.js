import { h } from 'https://esm.sh/preact@10.25.4';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, layout, animation } from '../tokens.js';
import { routes } from '../router.js';

const html = htm.bind(h);

// ---------------------------------------------------------------------------
// ProjectHeader — project name, repo, action buttons
// ---------------------------------------------------------------------------

export function ProjectHeader({ project, id, onEdit }) {
    const repoShort = project?.repo ? project.repo.split('/').pop() : '';

    const headerStyle = {
        display: 'flex',
        alignItems: 'center',
        flexWrap: 'wrap',
        paddingBottom: '16px',
        borderBottom: `1px solid ${colors.border}`,
        gap: '12px',
    };

    const titleStyle = {
        fontFamily: typography.fontBody,
        fontSize: typography.size['2xl'],
        fontWeight: typography.weight.semibold,
        color: colors.text,
        margin: 0,
        letterSpacing: '-0.02em',
        flex: '1 0 auto',
        minWidth: '200px',
    };

    const repoTagStyle = {
        fontFamily: typography.fontMono,
        fontSize: typography.size.sm,
        color: colors.textTertiary,
        flexShrink: 0,
    };

    return html`
        <div style=${headerStyle}>
            <h1 style=${titleStyle}>${project?.id || id}</h1>
            ${repoShort ? html`<span style=${repoTagStyle}>${repoShort}</span>` : null}
            <div style=${{ display: 'flex', alignItems: 'center', gap: '8px', marginLeft: 'auto', flexShrink: 0 }}>
                <button
                    onClick=${onEdit}
                    title="Edit project configuration"
                    style=${{
                        background: 'transparent',
                        border: `1px solid ${colors.border}`,
                        borderRadius: layout.borderRadius.sm,
                        color: colors.textTertiary,
                        cursor: 'pointer',
                        fontSize: '14px',
                        padding: '2px 7px',
                        lineHeight: 1,
                        transition: 'color 120ms, border-color 120ms',
                        flexShrink: 0,
                    }}
                >✎</button>
                <a
                    href=${routes.taskNew(id)}
                    style=${{
                        padding: '6px 14px',
                        borderRadius: layout.borderRadius.md,
                        background: colors.blue,
                        border: 'none',
                        color: '#fff',
                        fontSize: typography.size.sm,
                        fontFamily: typography.fontBody,
                        fontWeight: typography.weight.medium,
                        cursor: 'pointer',
                        whiteSpace: 'nowrap',
                        textDecoration: 'none',
                        display: 'inline-block',
                    }}
                >+ New Task</a>
            </div>
        </div>
    `;
}
