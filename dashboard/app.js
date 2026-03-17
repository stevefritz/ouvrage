import { h, render } from 'https://esm.sh/preact@10.25.4';
import { useState, useEffect, useCallback, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import { html, getRoute, navigate } from './components/utils.js';
import { api } from './api.js';
import { Nav } from './components/Nav.js';
import { Board } from './components/Board.js';
import { TaskDetail } from './components/TaskDetail.js';
import { Projects } from './components/Projects.js';
import { ProjectDetail } from './components/ProjectDetail.js';
import { ComponentDetail } from './components/ComponentDetail.js';
import { ConversationsList, ConversationDetail } from './components/Conversations.js';
import { GraphView } from './components/GraphView.js';
import { ActivityBar } from './components/ActivityBar.js';

function App() {
    const [route, setRoute] = useState(getRoute());
    const [systemInfo, setSystemInfo] = useState(null);
    const [jiraBaseUrl, setJiraBaseUrl] = useState(null);

    // Router: listen for hash changes
    useEffect(() => {
        const onHashChange = () => setRoute(getRoute());
        window.addEventListener('hashchange', onHashChange);
        return () => window.removeEventListener('hashchange', onHashChange);
    }, []);

    // Load system info
    useEffect(() => {
        async function loadSystem() {
            try {
                const sys = await api.getSystem();
                setSystemInfo(sys);
                if (sys.jira_base_url) setJiraBaseUrl(sys.jira_base_url);
            } catch (e) {
                console.warn('System info error:', e.message);
            }
        }
        loadSystem();
        const timer = setInterval(loadSystem, 30000);
        return () => clearInterval(timer);
    }, []);

    // Action handler shared across views
    const handleAction = useCallback(async (action, taskId) => {
        const labels = { cancel: 'Cancel', retry: 'Retry', resume: 'Resume', close: 'Close', 'skip-gate': 'Skip Gate', 'advance-chain': 'Advance Chain', 'cancel-chain': 'Cancel Chain' };
        const msg = action === 'close'
            ? `Close task "${taskId}"? This will clean up the worktree and branch.`
            : action === 'skip-gate'
            ? `Skip gate for "${taskId}"? This bypasses automated checks.`
            : action === 'advance-chain'
            ? `Dispatch next dependent task in the chain?`
            : action === 'cancel-chain'
            ? `Cancel "${taskId}" and ALL dependent tasks?`
            : `${labels[action]} task "${taskId}"?`;
        if (!confirm(msg)) return;
        try {
            if (action === 'cancel') await api.cancelTask(taskId);
            else if (action === 'retry') await api.retryTask(taskId);
            else if (action === 'resume') await api.resumeTask(taskId);
            else if (action === 'close') await api.closeTask(taskId);
            else if (action === 'skip-gate') await api.skipGate(taskId);
            else if (action === 'advance-chain') await api.advanceChain(taskId);
            else if (action === 'cancel-chain') await api.cancelChain(taskId);
            // Force re-route to refresh the view
            setRoute({ ...getRoute() });
        } catch (e) {
            alert(`Error: ${e.message}`);
        }
    }, []);

    let view;
    if (route.view === 'board') {
        view = html`<${Board} key=${JSON.stringify(route.params)} params=${route.params || {}} jiraBaseUrl=${jiraBaseUrl} onAction=${handleAction} />`;
    } else if (route.view === 'graph') {
        view = html`<${GraphView} key=${route.projectId} projectId=${route.projectId} jiraBaseUrl=${jiraBaseUrl} onAction=${handleAction} />`;
    } else if (route.view === 'detail') {
        view = html`<${TaskDetail} key=${route.taskId} taskId=${route.taskId} jiraBaseUrl=${jiraBaseUrl} onAction=${handleAction} />`;
    } else if (route.view === 'projects') {
        view = html`<${Projects} />`;
    } else if (route.view === 'project-detail') {
        view = html`<${ProjectDetail} key=${route.projectId} projectId=${route.projectId} />`;
    } else if (route.view === 'component-detail') {
        view = html`<${ComponentDetail} key=${route.componentId} componentId=${route.componentId} />`;
    } else if (route.view === 'conversations') {
        view = html`<${ConversationsList} />`;
    } else if (route.view === 'conversation-detail') {
        view = html`<${ConversationDetail} key=${route.convId} convId=${route.convId} />`;
    }

    return html`
        <${Nav} route=${route} systemInfo=${systemInfo} />
        <${ActivityBar} />
        ${view}
    `;
}

render(html`<${App} />`, document.getElementById('app'));
