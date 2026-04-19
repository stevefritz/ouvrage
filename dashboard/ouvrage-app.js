// Ouvrage App — entry point for the Ouvrage UI
// Mounts into #ouvrage-app, uses hash router for views.

import { h, render } from 'https://esm.sh/preact@10.25.4';
import htm from 'https://esm.sh/htm@3.1.1';
import { useEffect } from 'https://esm.sh/preact@10.25.4/hooks';
import { useRouter, navigate } from './router.js';
import { OuvrageShell } from './ouvrage-shell.js';
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

function OuvrageApp() {
    // Login page is served at /dashboard/login (full URL path, not hash route)
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
            const gitMissing = !data?.git_credential?.configured;
            if (anthropicMissing || gitMissing) {
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
        <${OuvrageShell}>
            ${content}
        <//>
    `;
}

// Mount into #ouvrage-root. OuvrageShell renders #ouvrage-app inside it.
const mountEl = document.getElementById('ouvrage-root');
if (mountEl) {
    render(html`<${OuvrageApp} />`, mountEl);
} else {
    console.error('[Ouvrage] Mount element #ouvrage-root not found');
}
