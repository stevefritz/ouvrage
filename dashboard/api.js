// Dashboard API client

const BASE = '/dashboard/api';

async function request(path, options = {}) {
    const resp = await fetch(BASE + path, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
    });
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({ error: resp.statusText }));
        throw new Error(err.error || resp.statusText);
    }
    const ct = resp.headers.get('content-type') || '';
    if (ct.includes('application/json')) return resp.json();
    return resp.text();
}

// Encode task IDs that contain slashes (e.g. "mcp-switchboard/code-roast")
const eid = (id) => encodeURIComponent(id);

export const api = {
    // Read
    getTasks: (params = {}) => {
        const qs = new URLSearchParams(params).toString();
        return request('/tasks' + (qs ? '?' + qs : ''));
    },
    getTask: (id) => request(`/tasks/${eid(id)}`),
    getMessages: (id, params = {}) => {
        const qs = new URLSearchParams(params).toString();
        return request(`/tasks/${eid(id)}/messages` + (qs ? '?' + qs : ''));
    },
    getSessionLog: (id) => request(`/tasks/${eid(id)}/session-log`),
    getDispatchLog: (id) => request(`/tasks/${eid(id)}/dispatch-log`),
    getProjects: () => request('/projects'),
    getProject: (id) => request(`/projects/${eid(id)}`),
    getSystem: () => request('/system'),
    getConversations: (params = {}) => {
        const qs = new URLSearchParams(params).toString();
        return request('/conversations' + (qs ? '?' + qs : ''));
    },
    getConversation: (id, params = {}) => {
        const qs = new URLSearchParams(params).toString();
        return request(`/conversations/${eid(id)}` + (qs ? '?' + qs : ''));
    },

    // Actions
    cancelTask: (id) => request(`/tasks/${eid(id)}/cancel`, { method: 'POST' }),
    retryTask: (id, clean = false) => request(`/tasks/${eid(id)}/retry`, {
        method: 'POST', body: JSON.stringify({ clean }),
    }),
    resumeTask: (id) => request(`/tasks/${eid(id)}/resume`, { method: 'POST' }),
    closeTask: (id) => request(`/tasks/${eid(id)}/close`, { method: 'POST' }),

    // Messages
    postMessage: (id, content, type = 'review', title = null) => request(`/tasks/${eid(id)}/messages`, {
        method: 'POST',
        body: JSON.stringify({ content, type, title }),
    }),
};
