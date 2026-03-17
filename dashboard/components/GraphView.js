// GraphView — wrapper that combines DAG graph + slide-in detail panel
import { useState, useEffect, useCallback, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { html, navigate } from './utils.js';
import { DagGraph } from './DagGraph.js';
import { GraphDetailPanel } from './GraphDetailPanel.js';

export function GraphView({ projectId, onAction }) {
    const [selectedTaskId, setSelectedTaskId] = useState(null);
    const [allTasks, setAllTasks] = useState(null);

    // Fetch all tasks for cross-reference in detail panel (blockers, etc.)
    useEffect(() => {
        api.getTasks({ project_id: projectId })
            .then(setAllTasks)
            .catch(() => {});
    }, [projectId]);

    const handleSelect = useCallback((taskId) => {
        setSelectedTaskId(taskId);
    }, []);

    const handleClose = useCallback(() => {
        setSelectedTaskId(null);
    }, []);

    return html`
        <div class="p-4">
            <div class="flex items-center gap-3 mb-3">
                <a href="#/projects" class="text-sm text-slate-400 hover:text-slate-200">\u2190 Projects</a>
                <h2 class="text-lg font-medium text-slate-200">${projectId}</h2>
                <span class="text-xs text-slate-500">Dependency Graph</span>
            </div>

            <div class="graph-layout ${selectedTaskId ? 'graph-layout-split' : ''}">
                <div class="graph-main">
                    <${DagGraph} projectId=${projectId}
                        onSelectTask=${handleSelect}
                        selectedTaskId=${selectedTaskId} />
                </div>

                ${selectedTaskId ? html`
                    <${GraphDetailPanel}
                        key=${selectedTaskId}
                        taskId=${selectedTaskId}
                        allTasks=${allTasks}
                        onClose=${handleClose}
                        onAction=${onAction} />
                ` : null}
            </div>
        </div>
    `;
}
