// Foreman App — entry point for the Foreman UI
// Mounts into #foreman-app, uses hash router for views.

import { h, render } from 'https://esm.sh/preact@10.25.4';
import htm from 'https://esm.sh/htm@3.1.1';
import { useEffect } from 'https://esm.sh/preact@10.25.4/hooks';
import { useRouter, navigate } from './router.js';
import { ForemanShell } from './foreman-shell.js';
import { LandingView } from './views/LandingView.js';
import { ProjectCreateView } from './views/ProjectCreateView.js';
import { ProjectView } from './views/ProjectView.js';
import { TaskView } from './views/TaskView.js';
import { TaskCreateView } from './views/TaskCreateView.js';
import { ConversationView } from './views/ConversationView.js';
import { LoginView } from './views/LoginView.js';
import { Settings } from './components/Settings.js';
import { Files } from './components/Files.js';
import { api } from './api.js';

const html = htm.bind(h);

// One-time flag: only redirect to settings on the very first page load.
// Module-level so it survives re-renders but resets on full page reload.
let _onboardingChecked = false;

function ForemanApp() {
    // Login page is served at /foreman/login (full URL path, not hash route)
    if (window.location.pathname === '/dashboard/login') {
        return html`<${LoginView} />`;
    }

    const { view, params } = useRouter();

    // On initial load, check if credentials are configured.
    // If not, redirect to Settings so the user can set them up.
    useEffect(() => {
        if (_onboardingChecked) return;
        _onboardingChecked = true;

        // Don't redirect if already on settings
        if (location.hash === '#/settings') return;

        api.getUserSettings().then(data => {
            const skip = data?.anthropic?.skip_credential_check;
            if (skip) return;
            const anthropicMissing = !data?.anthropic?.configured;
            const githubMissing = !data?.github?.configured;
            if (anthropicMissing || githubMissing) {
                navigate('/settings');
            }
        }).catch(() => {
            // Silently ignore — don't redirect on API errors
        });
    }, []);

    let content;

    if (view === 'landing') {
        content = html`<${LandingView} />`;
    } else if (view === 'project-new') {
        content = html`<${ProjectCreateView} />`;
    } else if (view === 'project') {
        content = html`<${ProjectView} id=${params.id} tab=${params.tab} />`;
    } else if (view === 'task-new') {
        content = html`<${TaskCreateView} project=${params.project} />`;
    } else if (view === 'task') {
        content = html`<${TaskView} id=${params.id} />`;
    } else if (view === 'conversation') {
        content = html`<${ConversationView} id=${params.id} />`;
    } else if (view === 'project-conversation') {
        content = html`<${ConversationView} id=${params.convId} projectId=${params.id} />`;
    } else if (view === 'files') {
        content = html`<${Files} />`;
    } else if (view === 'settings') {
        content = html`<${Settings} />`;
    } else {
        content = html`<${LandingView} />`;
    }

    return html`
        <${ForemanShell}>
            ${content}
        <//>
    `;
}

// Mount into #foreman-root. ForemanShell renders #foreman-app inside it.
const mountEl = document.getElementById('foreman-root');
if (mountEl) {
    render(html`<${ForemanApp} />`, mountEl);
} else {
    console.error('[Foreman] Mount element #foreman-root not found');
}
