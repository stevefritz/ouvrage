import { h, render, Component } from 'https://esm.sh/preact@10.25.4';
import { useState, useEffect, useCallback, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import { html, getRoute, navigate, ConfirmDialog } from './components/utils.js';
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
import { Settings } from './components/Settings.js';

// Register service worker for push notifications
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/dashboard/sw.js', { scope: '/dashboard/' })
        .catch(e => console.warn('SW registration failed:', e));
}

// ── Error Boundary ───────────────────────────────────────────
class ErrorBoundary extends Component {
    constructor(props) {
        super(props);
        this.state = { error: null };
    }
    static getDerivedStateFromError(error) {
        return { error: error.message || 'An unexpected error occurred' };
    }
    componentDidCatch(error) {
        console.error('ErrorBoundary caught:', error);
    }
    render() {
        if (this.state.error) {
            return html`<div class="error-state" style="margin-top: 4rem;">
                <div class="error-state-icon">\u26A0</div>
                <div class="error-state-msg">${this.state.error}</div>
                <button class="error-state-retry" onClick=${() => { this.setState({ error: null }); location.reload(); }}>Reload</button>
            </div>`;
        }
        return this.props.children;
    }
}

// ── Theme ────────────────────────────────────────────────────
function useTheme() {
    const [theme, setThemeState] = useState(() => localStorage.getItem('switchboard-theme') || 'dark');

    const setTheme = useCallback((t) => {
        setThemeState(t);
        localStorage.setItem('switchboard-theme', t);
        if (t === 'light') {
            document.documentElement.classList.add('theme-light');
        } else {
            document.documentElement.classList.remove('theme-light');
        }
    }, []);

    const toggleTheme = useCallback(() => {
        setTheme(theme === 'dark' ? 'light' : 'dark');
    }, [theme, setTheme]);

    return { theme, toggleTheme };
}

function App() {
    const [route, setRoute] = useState(getRoute());
    const [systemInfo, setSystemInfo] = useState(null);
    const [jiraBaseUrl, setJiraBaseUrl] = useState(null);
    const [confirmState, setConfirmState] = useState(null); // { action, taskId }
    const { theme, toggleTheme } = useTheme();

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

    // Action handler — show confirmation dialog
    const handleAction = useCallback(async (action, taskId) => {
        setConfirmState({ action, taskId });
    }, []);

    // Execute confirmed action
    const executeAction = useCallback(async () => {
        if (!confirmState) return;
        const { action, taskId } = confirmState;
        setConfirmState(null);
        try {
            if (action === 'cancel') await api.cancelTask(taskId);
            else if (action === 'retry') await api.retryTask(taskId);
            else if (action === 'resume') await api.resumeTask(taskId);
            else if (action === 'close') await api.closeTask(taskId);
            else if (action === 'skip-gate') await api.skipGate(taskId);
            else if (action === 'advance-chain') await api.advanceChain(taskId);
            else if (action === 'cancel-chain') await api.cancelChain(taskId);
            else if (action === 'release-worktree') await api.releaseWorktree(taskId);
            // Force re-route to refresh the view
            setRoute({ ...getRoute() });
        } catch (e) {
            alert(`Error: ${e.message}`);
        }
    }, [confirmState]);

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
        view = html`<${ProjectDetail} key=${route.projectId} projectId=${route.projectId} jiraBaseUrl=${jiraBaseUrl} onAction=${handleAction} />`;
    } else if (route.view === 'component-detail') {
        view = html`<${ComponentDetail} key=${route.componentId} componentId=${route.componentId} jiraBaseUrl=${jiraBaseUrl} onAction=${handleAction} />`;
    } else if (route.view === 'conversations') {
        view = html`<${ConversationsList} />`;
    } else if (route.view === 'conversation-detail') {
        view = html`<${ConversationDetail} key=${route.convId} convId=${route.convId} />`;
    } else if (route.view === 'settings') {
        view = html`<${Settings} />`;
    }

    return html`
        <${Nav} route=${route} systemInfo=${systemInfo} theme=${theme} onToggleTheme=${toggleTheme} />
        <${ActivityBar} />
        <${ErrorBoundary}>
            ${view}
        <//>
        <${ConfirmDialog}
            action=${confirmState?.action}
            taskId=${confirmState?.taskId}
            onConfirm=${executeAction}
            onCancel=${() => setConfirmState(null)} />
    `;
}

render(html`<${App} />`, document.getElementById('app'));
