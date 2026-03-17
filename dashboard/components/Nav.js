import { html, navigate } from './utils.js';

export function Nav({ route, systemInfo }) {
    const isActive = (navView) => {
        if (navView === 'board' && route.view === 'board') return true;
        if (navView === 'conversations' && (route.view === 'conversations' || route.view === 'conversation-detail')) return true;
        if (navView === 'projects' && (route.view === 'projects' || route.view === 'project-detail' || route.view === 'component-detail' || route.view === 'graph')) return true;
        if (navView === 'settings' && route.view === 'settings') return true;
        return false;
    };

    const linkClass = (navView) =>
        `text-sm ${isActive(navView) ? 'text-slate-100' : 'text-slate-400 hover:text-slate-200'}`;

    return html`
        <nav class="border-b border-slate-800 px-6 py-3 flex items-center justify-between">
            <div class="flex items-center gap-6">
                <a href="#/" class="text-lg font-bold tracking-wide text-slate-100">SWITCHBOARD</a>
                <a href="#/" class=${linkClass('board')}>Board</a>
                <a href="#/conversations" class=${linkClass('conversations')}>Conversations</a>
                <a href="#/projects" class=${linkClass('projects')}>Projects</a>
                <a href="#/settings" class=${linkClass('settings')}>Settings</a>
            </div>
            <div class="flex items-center gap-4 text-sm text-slate-400">
                <span>${systemInfo ? `${systemInfo.active_tasks} active` : '\u2014'}</span>
                <span>${systemInfo ? `$${systemInfo.total_cost_usd.toFixed(2)} total` : '\u2014'}</span>
            </div>
        </nav>
    `;
}
