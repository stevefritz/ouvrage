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
    getActivity: (params = {}) => {
        const qs = new URLSearchParams(params).toString();
        return request('/activity' + (qs ? '?' + qs : ''));
    },

    // Actions
    cancelTask: (id) => request(`/tasks/${eid(id)}/cancel`, { method: 'POST' }),
    retryTask: (id, clean = false) => request(`/tasks/${eid(id)}/retry`, {
        method: 'POST', body: JSON.stringify({ clean }),
    }),
    resumeTask: (id) => request(`/tasks/${eid(id)}/resume`, { method: 'POST' }),
    closeTask: (id) => request(`/tasks/${eid(id)}/close`, { method: 'POST' }),
    skipGate: (id) => request(`/tasks/${eid(id)}/skip-gate`, { method: 'POST' }),
    advanceChain: (id) => request(`/tasks/${eid(id)}/advance-chain`, { method: 'POST' }),
    cancelChain: (id) => request(`/tasks/${eid(id)}/cancel-chain`, { method: 'POST' }),
    releaseWorktree: (id) => request(`/tasks/${eid(id)}/release-worktree`, { method: 'POST' }),
    getChain: (id) => request(`/tasks/${eid(id)}/chain`),
    getReviewTask: (id) => request(`/tasks/${eid(id)}/review-task`),

    // Messages
    postMessage: (id, content, type = 'review', title = null) => request(`/tasks/${eid(id)}/messages`, {
        method: 'POST',
        body: JSON.stringify({ content, type, title }),
    }),

    // Push subscriptions
    pushSubscribe: (data) => request('/push/subscribe', {
        method: 'POST',
        body: JSON.stringify(data),
    }),
    pushUnsubscribe: (data) => request('/push/unsubscribe', {
        method: 'POST',
        body: JSON.stringify(data),
    }),
    getVapidPublicKey: () => request('/push/vapid-public-key'),

    // Notification settings
    getNotificationSettings: () => request('/settings/notifications'),
    updateNotificationSettings: (data) => request('/settings/notifications', {
        method: 'POST',
        body: JSON.stringify(data),
    }),
};
