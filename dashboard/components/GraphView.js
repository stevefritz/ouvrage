// GraphView — wrapper that combines DAG graph + slide-in detail panel
import { useState, useEffect, useCallback, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { html, navigate } from './utils.js';
import { DagGraph } from './DagGraph.js';
import { GraphDetailPanel } from './GraphDetailPanel.js';
import { ActivityTimeline } from './ActivityTimeline.js';

export function GraphView({ projectId, jiraBaseUrl, onAction }) {
    const [selectedTaskId, setSelectedTaskId] = useState(null);
    const [allTasks, setAllTasks] = useState(null);

    // Receive task data from DagGraph's polling cycle (avoids double-fetching)
    const handleTasksUpdate = useCallback((tasks) => {
        setAllTasks(tasks);
    }, []);

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
                        onTasksUpdate=${handleTasksUpdate}
                        selectedTaskId=${selectedTaskId} />
                </div>

                ${selectedTaskId ? html`
                    <div class="panel-backdrop" onClick=${handleClose}></div>
                    <${GraphDetailPanel}
                        key=${selectedTaskId}
                        taskId=${selectedTaskId}
                        allTasks=${allTasks}
                        jiraBaseUrl=${jiraBaseUrl}
                        onClose=${handleClose}
                        onAction=${onAction} />
                ` : null}
            </div>

            <${ActivityTimeline} projectId=${projectId} />
        </div>
    `;
}
