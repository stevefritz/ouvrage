import { useState, useEffect, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { html, relativeTime, progressBar, navigate, StatusBadge, GateBadge, ActionButtons, Tip, WorktreeIndicator, HeartbeatIndicator, ClaudeChatLink, LoadingState, ErrorState, EmptyState, jiraUrl, jiraLabel } from './utils.js';

export function Board({ params = {}, jiraBaseUrl, onAction }) {
    const [tasks, setTasks] = useState(null);
    const [error, setError] = useState(null);
    const [statuses, setStatuses] = useState([]);
    const [projects, setProjects] = useState([]);
    const [filterStatus, setFilterStatus] = useState(params.status || '');
    const [filterProject, setFilterProject] = useState(params.project_id || '');
    const [lastUpdated, setLastUpdated] = useState(null);
    const [pollFailed, setPollFailed] = useState(false);
    const mountedRef = useRef(true);

    useEffect(() => {
        mountedRef.current = true;
        return () => { mountedRef.current = false; };
    }, []);

    const load = async () => {
        try {
            const data = await api.getTasks(params);
            if (!mountedRef.current) return;
            setTasks(data);
            setError(null);
            setPollFailed(false);
            setLastUpdated(new Date());
            const s = [...new Set(data.map(t => t.status))];
            setStatuses(s);
        } catch (e) {
            if (!mountedRef.current) return;
            if (!tasks) setError(e.message);
            else setPollFailed(true);
        }
    };

    useEffect(() => {
        let timer;
        load();
        async function loadProjects() {
            try {
                const p = await api.getProjects();
                if (mountedRef.current) setProjects(p);
            } catch (e) { /* ignore */ }
        }
        loadProjects();
        timer = setInterval(load, 10000);
        return () => clearInterval(timer);
    }, [params.status, params.project_id]);

    const applyFilter = (status, project) => {
        const p = {};
        if (status) p.status = status;
        if (project) p.project_id = project;
        const qs = new URLSearchParams(p).toString();
        navigate('#/' + (qs ? '?' + qs : ''));
    };

    if (error) {
        return html`<div class="p-6"><${ErrorState} message="Failed to load tasks: ${error}" onRetry=${load} /></div>`;
    }

    if (tasks === null) {
        return html`<div class="p-6"><${LoadingState} message="Loading tasks..." /></div>`;
    }

    return html`
        <div class="p-6">
            <div class="flex items-center gap-4 mb-4">
                <select class="bg-slate-800 border border-slate-700 rounded px-3 py-1.5 text-sm text-slate-300"
                    value=${filterStatus}
                    onChange=${(e) => { setFilterStatus(e.target.value); applyFilter(e.target.value, filterProject); }}>
                    <option value="">All statuses</option>
                    ${statuses.map(s => html`<option value=${s} selected=${params.status === s}>${s}</option>`)}
                </select>
                <select class="bg-slate-800 border border-slate-700 rounded px-3 py-1.5 text-sm text-slate-300"
                    value=${filterProject}
                    onChange=${(e) => { setFilterProject(e.target.value); applyFilter(filterStatus, e.target.value); }}>
                    <option value="">All projects</option>
                    ${projects.map(p => html`<option value=${p.id} selected=${params.project_id === p.id}>${p.id}</option>`)}
                </select>
                ${pollFailed && lastUpdated ? html`
                    <div class="stale-warning">
                        \u26A0 Last updated ${relativeTime(lastUpdated.toISOString())}
                    </div>
                ` : null}
            </div>
            <div class="bg-slate-900 border border-slate-800 rounded-lg overflow-hidden">
                <table class="w-full">
                    <thead>
                        <tr class="border-b border-slate-800 text-xs text-slate-500 uppercase">
                            <th class="p-3 text-left w-28">Status</th>
                            <th class="p-3 text-left">Task</th>
                            <th class="p-3 text-left w-40">Progress</th>
                            <th class="p-3 text-left w-20">Cost</th>
                            <th class="p-3 text-left w-24">Activity</th>
                            <th class="p-3 text-left w-24">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${tasks.length === 0
                            ? html`<tr><td colspan="6"><${EmptyState} message="No tasks found" /></td></tr>`
                            : tasks.map(t => html`
                                <tr key=${t.id} class="border-b border-slate-800 hover:bg-slate-800/50 cursor-pointer"
                                    onClick=${() => navigate(`#/tasks/${t.id}`)}>
                                    <td class="p-3">
                                        <div class="flex items-center gap-1.5">
                                            <${StatusBadge} status=${t.status} />
                                            <${GateBadge} task=${t} />
                                            <${HeartbeatIndicator} task=${t} />
                                        </div>
                                    </td>
                                    <td class="p-3">
                                        <div class="flex items-center gap-2">
                                            <span class="font-mono text-sm text-slate-200">${t.id}</span>
                                            <${WorktreeIndicator} task=${t} />
                                            ${t.pr_url ? html`<a href=${t.pr_url} target="_blank" rel="noopener"
                                                onClick=${(e) => e.stopPropagation()}
                                                class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs bg-purple-500/20 text-purple-400 hover:bg-purple-500/30" title="View PR">PR</a>` : null}
                                            ${t.jira_ticket ? html`<a href=${jiraUrl(t.jira_ticket, jiraBaseUrl)} target="_blank" rel="noopener"
                                                onClick=${(e) => e.stopPropagation()}
                                                class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs bg-cyan-500/20 text-cyan-400 hover:bg-cyan-500/30" title="Jira">${jiraLabel(t.jira_ticket)}</a>` : null}
                                            ${t.claude_chat_url ? html`<span onClick=${(e) => e.stopPropagation()}><${ClaudeChatLink} url=${t.claude_chat_url} /></span>` : null}
                                        </div>
                                        <div class="text-sm text-slate-400 truncate max-w-md">${t.goal}</div>
                                        <div class="flex items-center gap-1 mt-0.5">
                                            ${t.phase ? html`<span class="text-xs text-slate-500">${t.phase}</span>` : null}
                                            ${(t.tags || []).map(tag => html`<span class="px-1.5 py-0 rounded text-xs bg-slate-700 text-slate-300">${tag}</span>`)}
                                        </div>
                                    </td>
                                    <td class="p-3">
                                        <${Tip} text="${t.checklist_done} of ${t.checklist_total} checklist items done">
                                            <span>
                                                <span class="font-mono text-xs text-slate-400 progress-bar">${progressBar(t.checklist_done, t.checklist_total)}</span>
                                                <span class="text-xs text-slate-400 ml-1">${t.checklist_done}/${t.checklist_total}</span>
                                            </span>
                                        <//>
                                    </td>
                                    <td class="p-3">
                                        <${Tip} text="Total API cost across all dispatches">
                                            <span class="text-sm text-slate-400">$${(t.total_cost_usd || 0).toFixed(2)}</span>
                                        <//>
                                    </td>
                                    <td class="p-3 text-xs text-slate-500">${relativeTime(t.last_activity || t.updated_at)}</td>
                                    <td class="p-3" onClick=${(e) => e.stopPropagation()}>
                                        <${ActionButtons} task=${t} onAction=${onAction} stopPropagation=${true} />
                                    </td>
                                </tr>
                            `)}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}
