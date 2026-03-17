import { useState, useEffect } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { html, navigate, StatusBadge, relativeTime } from './utils.js';

const COMPONENT_STATUS_COLORS = {
    planning:  { bg: 'bg-slate-500/20', text: 'text-slate-400', dot: '\u25CB' },
    active:    { bg: 'bg-emerald-500/20', text: 'text-emerald-400', dot: '\u25CF' },
    deployed:  { bg: 'bg-blue-500/20', text: 'text-blue-400', dot: '\u2713' },
    archived:  { bg: 'bg-slate-500/10', text: 'text-slate-500', dot: '\u2014' },
};

function ComponentStatusBadge({ status }) {
    const s = COMPONENT_STATUS_COLORS[status] || COMPONENT_STATUS_COLORS.planning;
    return html`<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-medium ${s.bg} ${s.text}">
        ${s.dot} ${(status || 'planning').toUpperCase()}
    </span>`;
}

function ProgressBar({ done, total }) {
    const pct = total > 0 ? Math.round(done / total * 100) : 0;
    return html`
        <div class="flex items-center gap-2">
            <div class="flex-1 bg-slate-700 rounded-full h-1.5 overflow-hidden">
                <div class="bg-emerald-500 h-1.5 rounded-full transition-all" style="width: ${pct}%"></div>
            </div>
            <span class="text-xs text-slate-400 tabular-nums">${done}/${total}</span>
        </div>
    `;
}

function ComponentCard({ comp }) {
    const hasActive = comp.active_tasks > 0;
    return html`
        <div class="bg-slate-900 border border-slate-700 rounded-lg p-4 hover:border-slate-500 cursor-pointer transition-colors"
            onClick=${() => navigate(`#/components/${encodeURIComponent(comp.id)}`)}>
            <div class="flex items-start justify-between mb-2">
                <h3 class="text-base font-medium text-slate-200">${comp.name}</h3>
                <${ComponentStatusBadge} status=${comp.status} />
            </div>
            ${comp.description && html`
                <p class="text-sm text-slate-400 mb-3 line-clamp-2">${comp.description}</p>
            `}
            <div class="text-xs font-mono text-slate-500 mb-3">${comp.base_branch || '\u2014'}</div>
            <${ProgressBar} done=${comp.done_tasks} total=${comp.total_tasks} />
            <div class="flex flex-wrap gap-3 mt-3 text-xs text-slate-400">
                <span class=${hasActive ? 'text-emerald-400 flex items-center gap-1' : ''}>
                    ${hasActive && html`<span class="status-dot-working">\u25CF</span>`}
                    ${comp.active_tasks} active
                </span>
                <span>$${(comp.total_cost || 0).toFixed(2)}</span>
                ${comp.conversation_count > 0 && html`
                    <span>${comp.conversation_count} conv${comp.conversation_count !== 1 ? 's' : ''}</span>
                `}
                ${comp.open_punchlist > 0 && html`
                    <span class="text-amber-400">${comp.open_punchlist} punchlist</span>
                `}
            </div>
        </div>
    `;
}

export function ProjectDetail({ projectId }) {
    const [project, setProject] = useState(null);
    const [components, setComponents] = useState(null);
    const [error, setError] = useState(null);

    useEffect(() => {
        async function load() {
            try {
                const [proj, comps] = await Promise.all([
                    api.getProject(projectId),
                    api.getComponents(projectId),
                ]);
                setProject(proj);
                setComponents(comps);
            } catch (e) {
                setError(e.message);
            }
        }
        load();
    }, [projectId]);

    if (error) {
        return html`<div class="p-6"><p class="text-red-400">Error: ${error}</p></div>`;
    }
    if (!project || components === null) {
        return html`<div class="p-6"><p class="text-slate-500">Loading...</p></div>`;
    }

    // Tasks without a component_id
    const ungroupedTasks = (project.tasks || []).filter(t => !t.component_id);

    return html`
        <div class="p-6 max-w-7xl">
            <!-- Breadcrumb -->
            <div class="flex items-center gap-2 text-sm text-slate-500 mb-4">
                <a href="#/projects" class="hover:text-slate-300">Projects</a>
                <span>/</span>
                <span class="text-slate-300">${projectId}</span>
            </div>

            <div class="flex items-center justify-between mb-6">
                <div>
                    <h2 class="text-xl font-semibold text-slate-100">${projectId}</h2>
                    <div class="text-sm text-slate-400 mt-1">
                        <span class="font-mono">${project.repo}</span>
                        <span class="mx-2">·</span>
                        branch: <span class="font-mono">${project.default_branch}</span>
                    </div>
                </div>
                <a href=${`#/?project_id=${encodeURIComponent(projectId)}`}
                    class="px-3 py-1.5 text-sm rounded bg-slate-800 text-slate-300 hover:bg-slate-700">
                    View All Tasks
                </a>
            </div>

            <!-- Component cards -->
            ${components.length === 0
                ? html`<div class="rounded-lg border border-slate-700 border-dashed p-8 text-center">
                    <p class="text-slate-500 mb-1">No components defined</p>
                    <p class="text-xs text-slate-600">Create components via the MCP tools to group tasks</p>
                  </div>`
                : html`
                    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 mb-8">
                        ${components.map(comp => html`<${ComponentCard} key=${comp.id} comp=${comp} />`)}
                    </div>
                `
            }

            <!-- Ungrouped tasks -->
            ${ungroupedTasks.length > 0 && html`
                <div class="mt-6">
                    <h3 class="text-sm font-medium text-slate-400 uppercase tracking-wide mb-3">
                        Ungrouped Tasks
                        <span class="ml-2 px-1.5 py-0.5 rounded bg-slate-700 text-slate-300 normal-case text-xs">${ungroupedTasks.length}</span>
                    </h3>
                    <div class="bg-slate-900 border border-slate-700 rounded-lg divide-y divide-slate-800">
                        ${ungroupedTasks.map(t => html`
                            <div key=${t.id} class="px-4 py-3 flex items-center gap-3 hover:bg-slate-800/50 cursor-pointer"
                                onClick=${() => navigate(`#/tasks/${encodeURIComponent(t.id)}`)}>
                                <${StatusBadge} status=${t.status} />
                                <span class="flex-1 text-sm text-slate-300 truncate">${t.goal}</span>
                                <span class="text-xs text-slate-500 font-mono">${t.id.split('/').pop()}</span>
                            </div>
                        `)}
                    </div>
                </div>
            `}
        </div>
    `;
}
