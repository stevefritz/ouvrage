// Foreman App — entry point for the Foreman UI
// Mounts into #foreman-app, uses hash router for views.

import { h, render } from 'https://esm.sh/preact@10.25.4';
import htm from 'https://esm.sh/htm@3.1.1';
import { useRouter } from './router.js';
import { ForemanShell } from './foreman-shell.js';
import { LandingView } from './views/LandingView.js';

const html = htm.bind(h);

function ForemanApp() {
    const { view, params } = useRouter();

    let content;

    if (view === 'landing') {
        content = html`<${LandingView} />`;
    } else if (view === 'project') {
        // Placeholder — project view is a future task
        content = html`
            <div style="padding: 40px; text-align: center; color: #5c5e66; font-size: 13px;">
                Project view for <strong style="color: #9899a1;">${params.id}</strong> — coming soon.
                <br /><br />
                <a href="#/" style="color: #7c5af6; text-decoration: none; font-size: 12px;">← Back to projects</a>
            </div>
        `;
    } else if (view === 'task') {
        content = html`
            <div style="padding: 40px; text-align: center; color: #5c5e66; font-size: 13px;">
                Task view for <strong style="color: #9899a1;">${params.id}</strong> — coming soon.
                <br /><br />
                <a href="#/" style="color: #7c5af6; text-decoration: none; font-size: 12px;">← Back to projects</a>
            </div>
        `;
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
