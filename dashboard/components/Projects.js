import { useState, useEffect } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { html, navigate, LoadingState, ErrorState, EmptyState, Tip } from './utils.js';

export function Projects() {
    const [projects, setProjects] = useState(null);
    const [error, setError] = useState(null);

    useEffect(() => {
        api.getProjects()
            .then(setProjects)
            .catch(e => setError(e.message));
    }, []);

    if (error) {
        return html`<div class="p-6"><${ErrorState} message="Failed to load projects: ${error}" onRetry=${() => { setError(null); api.getProjects().then(setProjects).catch(e => setError(e.message)); }} /></div>`;
    }

    if (projects === null) {
        return html`<div class="p-6"><${LoadingState} message="Loading projects..." /></div>`;
    }

    return html`
        <div class="p-6">
            <h2 class="text-lg font-medium text-slate-200 mb-4">Projects</h2>
            <div class="grid gap-4">
                ${projects.length === 0
                    ? html`<${EmptyState} message="No projects registered" />`
                    : projects.map(p => html`
                        <div key=${p.id} class="bg-slate-900 border ${p.paused ? 'border-amber-500/40' : 'border-slate-700'} rounded-lg p-4 hover:border-slate-600 ${p.paused ? 'opacity-75' : ''}">
                            <div class="flex items-start justify-between mb-1">
                                <div class="flex items-center gap-2 min-w-0">
                                    <h3 class="text-lg font-medium text-slate-200 cursor-pointer hover:text-slate-100"
                                        onClick=${() => navigate(`#/projects/${encodeURIComponent(p.id)}`)}>${p.id}</h3>
                                    ${p.paused ? html`<span class="px-1.5 py-0.5 rounded text-xs font-medium bg-amber-500/20 text-amber-400 border border-amber-500/30 shrink-0">PAUSED</span>` : null}
                                </div>
                                <a href="#/graph/${encodeURIComponent(p.id)}"
                                    class="px-2 py-1 text-xs rounded bg-blue-500/20 text-blue-400 hover:bg-blue-500/30 shrink-0"
                                    onClick=${(e) => e.stopPropagation()}>Graph \u2197</a>
                            </div>
                            <div class="text-sm text-slate-400 mb-2">
                                <span class="font-mono">${p.repo}</span>
                                <span class="mx-2">\u00B7</span>
                                branch: <span class="font-mono">${p.default_branch}</span>
                            </div>
                            <div class="flex gap-4 text-sm">
                                <span class=${p.active_task_count > 0 ? 'text-emerald-400' : 'text-slate-500'}>${p.active_task_count} active</span>
                                <span class="text-slate-500">${p.total_tasks} total</span>
                                <${Tip} text="Total API cost across all tasks"><span class="text-slate-500">$${p.total_cost.toFixed(2)}</span><//>
                            </div>
                        </div>
                    `)}
            </div>
        </div>
    `;
}
