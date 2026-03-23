// Foreman App — entry point for the Foreman UI
// Mounts into #foreman-app, uses hash router for views.

import { h, render } from 'https://esm.sh/preact@10.25.4';
import htm from 'https://esm.sh/htm@3.1.1';
import { useRouter } from './router.js';
import { ForemanShell } from './foreman-shell.js';
import { LandingView } from './views/LandingView.js';
import { ProjectView } from './views/ProjectView.js';
import { TaskView } from './views/TaskView.js';

const html = htm.bind(h);

function ForemanApp() {
    const { view, params } = useRouter();

    let content;

    if (view === 'landing') {
        content = html`<${LandingView} />`;
    } else if (view === 'project') {
        content = html`<${ProjectView} id=${params.id} />`;
    } else if (view === 'task') {
        content = html`<${TaskView} id=${params.id} />`;
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
