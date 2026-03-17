import { useState, useEffect } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { html, relativeTime, navigate, LoadingState, ErrorState, EmptyState } from './utils.js';
import { MessageThread } from './MessageThread.js';

export function ConversationsList() {
    const [conversations, setConversations] = useState(null);
    const [error, setError] = useState(null);

    useEffect(() => {
        api.getConversations()
            .then(setConversations)
            .catch(e => setError(e.message));
    }, []);

    if (error) {
        return html`<div class="p-6"><${ErrorState} message="Failed to load conversations: ${error}" onRetry=${() => { setError(null); api.getConversations().then(setConversations).catch(e => setError(e.message)); }} /></div>`;
    }

    if (conversations === null) {
        return html`<div class="p-6"><${LoadingState} message="Loading conversations..." /></div>`;
    }

    return html`
        <div class="p-6">
            <h2 class="text-lg font-medium text-slate-200 mb-4">Conversations</h2>
            ${conversations.length === 0
                ? html`<${EmptyState} message="No conversations yet" />`
                : html`
                    <div class="bg-slate-900 border border-slate-800 rounded-lg overflow-hidden">
                        <table class="w-full">
                            <thead>
                                <tr class="border-b border-slate-800 text-xs text-slate-500 uppercase">
                                    <th class="p-3 text-left">Conversation</th>
                                    <th class="p-3 text-left w-32">Project</th>
                                    <th class="p-3 text-left w-20">Messages</th>
                                    <th class="p-3 text-left w-24">Activity</th>
                                    <th class="p-3 text-left w-16">Pinned</th>
                                </tr>
                            </thead>
                            <tbody>
                                ${conversations.map(c => html`
                                    <tr key=${c.id} class="border-b border-slate-800 hover:bg-slate-800/50 cursor-pointer"
                                        onClick=${() => navigate(`#/conversations/${encodeURIComponent(c.id)}`)}>
                                        <td class="p-3">
                                            <div class="font-mono text-sm text-slate-200">${c.id}</div>
                                            <div class="text-sm text-slate-400 truncate max-w-md">${c.goal || ''}</div>
                                        </td>
                                        <td class="p-3 text-sm text-slate-400">${c.project || ''}</td>
                                        <td class="p-3 text-sm text-slate-400">${c.message_count || 0}</td>
                                        <td class="p-3 text-xs text-slate-500">${relativeTime(c.last_message_at || c.updated_at)}</td>
                                        <td class="p-3 text-sm">${c.has_pinned ? '\uD83D\uDCCC' : ''}</td>
                                    </tr>
                                `)}
                            </tbody>
                        </table>
                    </div>
                `}
        </div>
    `;
}

export function ConversationDetail({ convId }) {
    const [thread, setThread] = useState(null);
    const [error, setError] = useState(null);

    useEffect(() => {
        setThread(null);
        setError(null);
        api.getConversation(convId)
            .then(setThread)
            .catch(e => setError(e.message));
    }, [convId]);

    if (error) {
        return html`<div class="p-6">
            <div class="mb-4"><a href="#/conversations" class="text-sm text-slate-400 hover:text-slate-200">\u2190 Conversations</a></div>
            <${ErrorState} message="Failed to load conversation: ${error}" onRetry=${() => { setError(null); api.getConversation(convId).then(setThread).catch(e => setError(e.message)); }} />
        </div>`;
    }

    if (!thread) {
        return html`<div class="p-6">
            <div class="mb-4"><a href="#/conversations" class="text-sm text-slate-400 hover:text-slate-200">\u2190 Conversations</a></div>
            <${LoadingState} message="Loading conversation..." />
        </div>`;
    }

    const msgs = thread.messages || [];

    return html`
        <div class="p-6">
            <div class="mb-4">
                <a href="#/conversations" class="text-sm text-slate-400 hover:text-slate-200">\u2190 Conversations</a>
            </div>
            <div class="bg-slate-900 border border-slate-800 rounded-lg p-4 mb-4">
                <div class="flex items-center gap-3 mb-1">
                    <span class="font-mono text-lg text-slate-200">${convId}</span>
                </div>
                <div class="text-sm text-slate-400">${msgs.length} messages</div>
            </div>
            <div class="bg-slate-900 border border-slate-800 rounded-lg p-4">
                <h3 class="text-sm font-medium text-slate-300 mb-3">Messages</h3>
                <${MessageThread} messages=${msgs} idPrefix="conv-msg" />
            </div>
        </div>
    `;
}
