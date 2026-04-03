// Foreman Hash Router
// Handles Foreman-specific routes: #/ #/project/:id #/task/:id #/conversation/:id
// Separate from utils.js getRoute() which handles the existing dashboard routes.
//
// Usage:
//   import { useRouter, navigate } from './router.js';
//   const { route, params } = useRouter();

import { useState, useEffect } from 'https://esm.sh/preact@10.25.4/hooks';

/**
 * Parse a Foreman-style hash route.
 * Returns { view, params } where params is an object of named captures.
 *
 * Routes:
 *   #/                       → { view: 'landing', params: {} }
 *   #/project/:id            → { view: 'project', params: { id } }
 *   #/task/:id               → { view: 'task', params: { id } }
 *   #/conversation/:id       → { view: 'conversation', params: { id } }
 *
 * Falls back to { view: 'landing', params: {} } for unknown routes.
 */
export function parseRoute() {
    const hash = location.hash.slice(1) || '/';

    if (hash === '/' || hash === '') {
        return { view: 'landing', params: {} };
    }

    // /project/new — must come before /project/:id
    if (hash === '/project/new') {
        return { view: 'project-new', params: {} };
    }

    // /project/:id
    const projectMatch = hash.match(/^\/project\/(.+)$/);
    if (projectMatch) {
        return { view: 'project', params: { id: decodeURIComponent(projectMatch[1]) } };
    }

    // /task/new — must come before /task/:id
    if (hash === '/task/new' || hash.startsWith('/task/new?')) {
        const query = {};
        const qIndex = hash.indexOf('?');
        if (qIndex !== -1) {
            new URLSearchParams(hash.slice(qIndex + 1)).forEach((v, k) => { query[k] = v; });
        }
        return { view: 'task-new', params: query };
    }

    // /task/:id
    const taskMatch = hash.match(/^\/task\/(.+)$/);
    if (taskMatch) {
        return { view: 'task', params: { id: decodeURIComponent(taskMatch[1]) } };
    }

    // /conversation/:id
    const convMatch = hash.match(/^\/conversation\/(.+)$/);
    if (convMatch) {
        return { view: 'conversation', params: { id: decodeURIComponent(convMatch[1]) } };
    }

    // /files
    if (hash === '/files') {
        return { view: 'files', params: {} };
    }

    // /settings
    if (hash === '/settings') {
        return { view: 'settings', params: {} };
    }

    // /docs
    if (hash === '/docs') {
        return { view: 'docs', params: {} };
    }

    return { view: 'landing', params: {} };
}

/**
 * Navigate to a Foreman route.
 * Examples:
 *   navigate('/')
 *   navigate('/project/mcp-switchboard')
 *   navigate('/task/mcp-switchboard/foreman-design-system')
 *   navigate('/conversation/foreman-design')
 */
export function navigate(path) {
    location.hash = path.startsWith('/') ? path : '/' + path;
}

/**
 * Preact hook: returns { view, params } and re-renders on hash change.
 * Use this in your Foreman app root component.
 *
 * Example:
 *   function ForemanApp() {
 *     const { view, params } = useRouter();
 *     if (view === 'landing')      return html`<${LandingView} />`;
 *     if (view === 'project')      return html`<${ProjectView} id=${params.id} />`;
 *     if (view === 'task')         return html`<${TaskView} id=${params.id} />`;
 *     if (view === 'conversation') return html`<${ConversationView} id=${params.id} />`;
 *   }
 */
export function useRouter() {
    const [route, setRoute] = useState(parseRoute);

    useEffect(() => {
        const onHashChange = () => setRoute(parseRoute());
        window.addEventListener('hashchange', onHashChange);
        return () => window.removeEventListener('hashchange', onHashChange);
    }, []);

    return route;
}

/**
 * Build route URLs for use in href attributes.
 * Avoids string concatenation at call sites.
 */
export const routes = {
    landing:      () => '#/',
    projectNew:   () => '#/project/new',
    project:      (id) => `#/project/${encodeURIComponent(id)}`,
    task:         (id) => `#/task/${encodeURIComponent(id)}`,
    taskNew:      (projectId) => projectId ? `#/task/new?project=${encodeURIComponent(projectId)}` : '#/task/new',
    conversation: (id) => `#/conversation/${encodeURIComponent(id)}`,
    files:        () => '#/files',
    settings:     () => '#/settings',
};
