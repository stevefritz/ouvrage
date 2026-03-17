import { html } from './utils.js';
import { useState, useEffect, useCallback } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';

// ── Toggle component ──────────────────────────────────────────────────────

function Toggle({ checked, onChange, disabled = false }) {
    return html`
        <button
            role="switch"
            aria-checked=${checked}
            disabled=${disabled}
            onClick=${() => !disabled && onChange(!checked)}
            class="relative inline-flex h-5 w-9 items-center rounded-full transition-colors focus:outline-none
                   ${checked ? 'bg-indigo-500' : 'bg-slate-600'}
                   ${disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}"
        >
            <span class="inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform
                         ${checked ? 'translate-x-4' : 'translate-x-1'}" />
        </button>
    `;
}

// ── Subscribe/unsubscribe logic ───────────────────────────────────────────

async function getPushState() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
        return { supported: false, serverConfigured: true, subscribed: false };
    }
    // Check if server has VAPID keys configured
    let serverConfigured = true;
    try {
        const keyResp = await api.getVapidPublicKey();
        serverConfigured = !!keyResp.vapid_public_key;
    } catch {
        serverConfigured = false;
    }
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    return { supported: true, serverConfigured, subscribed: !!sub, subscription: sub };
}

async function subscribePush() {
    const reg = await navigator.serviceWorker.ready;
    const keyResp = await api.getVapidPublicKey();
    const vapidKey = keyResp.vapid_public_key;
    if (!vapidKey) throw new Error('VAPID public key not configured on server');

    // Convert base64 url-safe key to Uint8Array
    const padding = '='.repeat((4 - (vapidKey.length % 4)) % 4);
    const b64 = (vapidKey + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(b64);
    const applicationServerKey = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) applicationServerKey[i] = raw.charCodeAt(i);

    const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey,
    });

    const json = sub.toJSON();
    await api.pushSubscribe({
        endpoint: json.endpoint,
        p256dh: json.keys.p256dh,
        auth: json.keys.auth,
    });
    return sub;
}

async function unsubscribePush(subscription) {
    await api.pushUnsubscribe({ endpoint: subscription.endpoint });
    await subscription.unsubscribe();
}

// ── Settings component ────────────────────────────────────────────────────

export function Settings() {
    const [push, setPush] = useState({ supported: false, subscribed: false, subscription: null });
    const [pushLoading, setPushLoading] = useState(false);
    const [pushError, setPushError] = useState(null);

    const [settings, setSettings] = useState(null);
    const [settingsSaving, setSettingsSaving] = useState(false);
    const [settingsError, setSettingsError] = useState(null);
    const [saved, setSaved] = useState(false);

    // Load push state and notification settings on mount
    useEffect(() => {
        getPushState().then(setPush);
        api.getNotificationSettings()
            .then(setSettings)
            .catch(e => setSettingsError(e.message));
    }, []);

    const handlePushToggle = useCallback(async () => {
        setPushError(null);
        setPushLoading(true);
        try {
            if (push.subscribed) {
                await unsubscribePush(push.subscription);
                setPush(await getPushState());
            } else {
                const perm = await Notification.requestPermission();
                if (perm !== 'granted') {
                    throw new Error('Notification permission denied. Enable it in browser settings.');
                }
                await subscribePush();
                setPush(await getPushState());
            }
        } catch (e) {
            setPushError(e.message);
        } finally {
            setPushLoading(false);
        }
    }, [push]);

    const handleSettingToggle = useCallback(async (key, value) => {
        setSaved(false);
        setSettingsError(null);
        const updated = { ...settings, [key]: value };
        setSettings(updated);
        setSettingsSaving(true);
        try {
            const saved = await api.updateNotificationSettings({ [key]: value });
            setSettings(saved);
            setSaved(true);
            setTimeout(() => setSaved(false), 2000);
        } catch (e) {
            setSettingsError(e.message);
            // Revert optimistic update
            setSettings(settings);
        } finally {
            setSettingsSaving(false);
        }
    }, [settings]);

    const notifDisabled = !push.subscribed;

    return html`
        <div class="max-w-2xl mx-auto px-6 py-8">
            <h1 class="text-xl font-semibold text-slate-100 mb-6">Settings</h1>

            <!-- Push notifications card -->
            <div class="bg-slate-900 border border-slate-800 rounded-lg p-5 mb-6">
                <h2 class="text-base font-medium text-slate-200 mb-1">Push Notifications</h2>
                <p class="text-sm text-slate-400 mb-4">
                    Receive browser notifications when tasks complete, fail, or need attention.
                    This browser/device only.
                </p>

                ${!push.supported && html`
                    <p class="text-sm text-amber-400">
                        Push notifications are not supported in this browser.
                    </p>
                `}

                ${push.supported && !push.serverConfigured && html`
                    <p class="text-sm text-amber-400">
                        Push notifications require server configuration (VAPID keys not set).
                    </p>
                `}

                ${push.supported && push.serverConfigured && html`
                    <div class="flex items-center justify-between">
                        <div>
                            <div class="text-sm text-slate-200 font-medium">Enable push notifications</div>
                            <div class="text-xs text-slate-500 mt-0.5">
                                ${push.subscribed ? 'Active on this device' : 'Not subscribed'}
                            </div>
                        </div>
                        <${Toggle}
                            checked=${push.subscribed}
                            onChange=${handlePushToggle}
                            disabled=${pushLoading}
                        />
                    </div>
                `}

                ${pushError && html`
                    <p class="text-sm text-red-400 mt-3">${pushError}</p>
                `}
            </div>

            <!-- Notification types card -->
            <div class="bg-slate-900 border border-slate-800 rounded-lg p-5">
                <div class="flex items-center justify-between mb-1">
                    <h2 class="text-base font-medium text-slate-200">Notification Types</h2>
                    ${saved && html`<span class="text-xs text-emerald-400">Saved</span>`}
                </div>
                <p class="text-sm text-slate-400 mb-4">
                    Choose which events trigger a push notification.
                    ${notifDisabled ? html`<span class="text-slate-500"> Enable push above first.</span>` : ''}
                </p>

                ${!settings && !settingsError && html`
                    <p class="text-sm text-slate-500">Loading…</p>
                `}

                ${settingsError && html`
                    <p class="text-sm text-red-400">${settingsError}</p>
                `}

                ${settings && html`
                    <div class="space-y-4">
                        <${SettingRow}
                            label="Task failed"
                            description="Task ends with an error or exception."
                            checked=${!!settings.notify_failed}
                            disabled=${notifDisabled || settingsSaving}
                            onChange=${(v) => handleSettingToggle('notify_failed', v)}
                        />
                        <${SettingRow}
                            label="Needs review"
                            description="Task timed out, lost its process, or ended without a result."
                            checked=${!!settings.notify_needs_review}
                            disabled=${notifDisabled || settingsSaving}
                            onChange=${(v) => handleSettingToggle('notify_needs_review', v)}
                        />
                        <${SettingRow}
                            label="CC posted a question"
                            description="Task is paused waiting for your input."
                            checked=${!!settings.notify_question}
                            disabled=${notifDisabled || settingsSaving}
                            onChange=${(v) => handleSettingToggle('notify_question', v)}
                        />
                        <${SettingRow}
                            label="Task completed"
                            description="Task finishes successfully. Off by default to reduce noise."
                            checked=${!!settings.notify_completed}
                            disabled=${notifDisabled || settingsSaving}
                            onChange=${(v) => handleSettingToggle('notify_completed', v)}
                        />
                    </div>
                `}
            </div>
        </div>
    `;
}

function SettingRow({ label, description, checked, disabled, onChange }) {
    return html`
        <div class="flex items-start justify-between gap-4">
            <div>
                <div class="text-sm text-slate-200">${label}</div>
                <div class="text-xs text-slate-500 mt-0.5">${description}</div>
            </div>
            <div class="mt-0.5 flex-shrink-0">
                <${Toggle} checked=${checked} disabled=${disabled} onChange=${onChange} />
            </div>
        </div>
    `;
}
