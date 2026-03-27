import { html } from './utils.js';
import { useState, useEffect, useCallback } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import {
    styles, SectionHeader, FormField, FormRow,
    CredentialCard, SecretRow, Toggle, ConfirmAction,
} from './FormKit.js';
import { colors } from '../tokens.js';

// ── Subscribe/unsubscribe logic ───────────────────────────────────────────

async function getPushState() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
        return { supported: false, serverConfigured: true, subscribed: false };
    }
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

// ── Common timezone list ──────────────────────────────────────────────────

const TIMEZONES = [
    'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
    'America/Anchorage', 'Pacific/Honolulu', 'America/Toronto', 'America/Vancouver',
    'America/Halifax', 'America/St_Johns', 'America/Edmonton', 'America/Winnipeg',
    'America/Sao_Paulo', 'America/Argentina/Buenos_Aires', 'America/Mexico_City',
    'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Europe/Madrid', 'Europe/Rome',
    'Europe/Amsterdam', 'Europe/Brussels', 'Europe/Zurich', 'Europe/Stockholm',
    'Europe/Oslo', 'Europe/Helsinki', 'Europe/Warsaw', 'Europe/Prague',
    'Europe/Moscow', 'Europe/Istanbul',
    'Asia/Dubai', 'Asia/Kolkata', 'Asia/Bangkok', 'Asia/Singapore',
    'Asia/Shanghai', 'Asia/Tokyo', 'Asia/Seoul', 'Asia/Hong_Kong',
    'Australia/Sydney', 'Australia/Melbourne', 'Australia/Perth',
    'Pacific/Auckland', 'UTC',
];

// ── Feedback banner ───────────────────────────────────────────────────────

function FeedbackBanner({ message, type = 'success' }) {
    if (!message) return null;
    const color = type === 'success' ? colors.green : colors.red;
    return html`<div style=${{ fontSize: '12px', color, marginTop: '8px' }}>${message}</div>`;
}

// ── Notification checkbox row ─────────────────────────────────────────────

function NotifCheckbox({ label, description, checked, disabled, onChange }) {
    return html`
        <label style=${{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            cursor: disabled ? 'not-allowed' : 'pointer',
            opacity: disabled ? 0.5 : 1,
        }}>
            <input type="checkbox"
                checked=${checked}
                disabled=${disabled}
                onChange=${(e) => onChange(e.target.checked)}
                style=${{ accentColor: colors.accent, flexShrink: 0 }}
            />
            <span style=${{ fontSize: '13px', fontWeight: '500', color: colors.text }}>${label}</span>
            <span style=${{ fontSize: '12px', color: colors.textTertiary }}>${description}</span>
        </label>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// GitHub credential card
// ══════════════════════════════════════════════════════════════════════════

function GitHubCard({ github, onSaved }) {
    const [pat, setPat] = useState('');
    const [saving, setSaving] = useState(false);
    const [testing, setTesting] = useState(false);
    const [feedback, setFeedback] = useState(null);
    const [editing, setEditing] = useState(!github.connected && !github.pat_last4);

    const handleSave = useCallback(async () => {
        if (!pat.trim()) return;
        setSaving(true);
        setFeedback(null);
        try {
            await api.patchInstanceSettings({ github_pat: pat.trim() });
            setPat('');
            setEditing(false);
            setFeedback({ type: 'success', message: 'GitHub PAT saved' });
            if (onSaved) onSaved();
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setSaving(false);
        }
    }, [pat, onSaved]);

    const handleTest = useCallback(async () => {
        setTesting(true);
        setFeedback(null);
        try {
            const result = await api.testGithub();
            if (result.valid) {
                setFeedback({ type: 'success', message: `Connected as ${result.username}` });
            } else {
                setFeedback({ type: 'error', message: result.error || 'Connection failed' });
            }
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setTesting(false);
        }
    }, []);

    const statusText = !editing
        ? (github.connected
            ? `Connected${github.username ? ` as ${github.username}` : ''}`
            : (github.pat_last4 ? 'Not connected' : undefined))
        : undefined;
    const maskedValue = (!editing && github.pat_last4) ? `····${github.pat_last4}` : undefined;

    const editForm = html`
        <div style=${{ display: 'flex', gap: '8px' }}>
            <input type="password"
                style=${styles.input}
                placeholder="ghp_xxxxxxxxxxxx"
                value=${pat}
                onInput=${(e) => setPat(e.target.value)}
                onKeyDown=${(e) => e.key === 'Enter' && handleSave()} />
            <button
                style=${{
                    ...styles.buttonPrimary,
                    opacity: (saving || !pat.trim()) ? 0.5 : 1,
                    cursor: (saving || !pat.trim()) ? 'not-allowed' : 'pointer',
                    whiteSpace: 'nowrap',
                }}
                onClick=${handleSave}
                disabled=${saving || !pat.trim()}>
                ${saving ? 'Saving…' : (github.pat_last4 ? 'Update' : 'Connect')}
            </button>
            ${github.pat_last4 && html`
                <button
                    style=${styles.button}
                    onClick=${() => { setEditing(false); setPat(''); setFeedback(null); }}>
                    Cancel
                </button>
            `}
        </div>
    `;

    return html`
        <div style=${{ marginBottom: '12px' }}>
            <${CredentialCard}
                icon="🐙"
                name="GitHub"
                connected=${github.connected}
                statusText=${statusText}
                maskedValue=${maskedValue}
                onUpdate=${editing ? undefined : () => setEditing(true)}
                onTest=${(!editing && !testing && github.pat_last4) ? handleTest : undefined}
            >
                ${editing ? editForm : null}
            </${CredentialCard}>
            ${feedback && html`
                <div style=${{ padding: '4px 16px 0' }}>
                    <${FeedbackBanner} message=${feedback.message} type=${feedback.type} />
                </div>
            `}
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// OAuth / MCP Connection
// ══════════════════════════════════════════════════════════════════════════

function OAuthCard({ oauth, onRegenerated }) {
    const [regenerating, setRegenerating] = useState(false);
    const [feedback, setFeedback] = useState(null);

    const handleRegenerate = useCallback(async () => {
        setRegenerating(true);
        setFeedback(null);
        try {
            await api.regenerateOAuthSecret();
            setFeedback({ type: 'success', message: 'Secret regenerated. Update your Claude.ai configuration.' });
            if (onRegenerated) onRegenerated();
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setRegenerating(false);
        }
    }, [onRegenerated]);

    if (!oauth.client_id) {
        return html`
            <div style=${styles.card}>
                <div style=${styles.cardTitle}>OAuth / MCP connection</div>
                <div style=${{ ...styles.cardSubtitle, marginTop: '4px' }}>OAuth client not configured.</div>
            </div>
        `;
    }

    return html`
        <div style=${styles.card}>
            <div style=${{ ...styles.cardTitle, marginBottom: '4px' }}>OAuth / MCP connection</div>
            <div style=${{ fontSize: '12px', color: colors.textSecondary, marginBottom: '14px' }}>
                Use these credentials to connect Claude.ai to your Switchboard instance
            </div>

            <${SecretRow} label="Client ID" value=${oauth.client_id} alwaysVisible=${true} />
            <${SecretRow} label="Client secret" value=${oauth.client_secret} />

            <div style=${{ marginTop: '10px', display: 'flex', alignItems: 'center', gap: '12px' }}>
                <${ConfirmAction}
                    label="Regenerate secret"
                    confirmLabel="Yes, regenerate"
                    warningText="Are you sure?"
                    onConfirm=${handleRegenerate}
                    danger=${true}
                />
                <span style=${{ fontSize: '11px', color: colors.textTertiary }}>
                    Disconnects existing MCP connections
                </span>
            </div>

            <${FeedbackBanner} message=${feedback?.message} type=${feedback?.type} />
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// Anthropic API Key card
// ══════════════════════════════════════════════════════════════════════════

function AnthropicKeyCard({ anthropic, onSaved }) {
    const [key, setKey] = useState('');
    const [saving, setSaving] = useState(false);
    const [testing, setTesting] = useState(false);
    const [feedback, setFeedback] = useState(null);
    const [editing, setEditing] = useState(!anthropic.configured);

    const handleSave = useCallback(async () => {
        if (!key.trim()) return;
        setSaving(true);
        setFeedback(null);
        try {
            await api.patchUserSettings({ anthropic_api_key: key.trim() });
            setKey('');
            setEditing(false);
            setFeedback({ type: 'success', message: 'API key saved' });
            if (onSaved) onSaved();
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setSaving(false);
        }
    }, [key, onSaved]);

    const handleTest = useCallback(async () => {
        setTesting(true);
        setFeedback(null);
        try {
            const result = await api.testAnthropic();
            if (result.valid) {
                setFeedback({ type: 'success', message: 'API key is valid' });
            } else {
                setFeedback({ type: 'error', message: result.error || 'Validation failed' });
            }
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setTesting(false);
        }
    }, []);

    const statusText = !editing
        ? (anthropic.configured ? 'Configured' : undefined)
        : undefined;
    const maskedValue = (!editing && anthropic.key_last4) ? `····${anthropic.key_last4}` : undefined;

    const editForm = html`
        <div style=${{ display: 'flex', gap: '8px' }}>
            <input type="password"
                style=${styles.input}
                placeholder="sk-ant-xxxxxxxxxxxx"
                value=${key}
                onInput=${(e) => setKey(e.target.value)}
                onKeyDown=${(e) => e.key === 'Enter' && handleSave()} />
            <button
                style=${{
                    ...styles.buttonPrimary,
                    opacity: (saving || !key.trim()) ? 0.5 : 1,
                    cursor: (saving || !key.trim()) ? 'not-allowed' : 'pointer',
                    whiteSpace: 'nowrap',
                }}
                onClick=${handleSave}
                disabled=${saving || !key.trim()}>
                ${saving ? 'Saving…' : 'Save'}
            </button>
            ${anthropic.configured && html`
                <button
                    style=${styles.button}
                    onClick=${() => { setEditing(false); setKey(''); setFeedback(null); }}>
                    Cancel
                </button>
            `}
        </div>
    `;

    return html`
        <div style=${{ marginBottom: '12px' }}>
            <${CredentialCard}
                icon="🔑"
                name="Anthropic API key"
                connected=${anthropic.configured}
                statusText=${statusText}
                maskedValue=${maskedValue}
                onUpdate=${editing ? undefined : () => setEditing(true)}
                onTest=${(!editing && !testing && anthropic.configured) ? handleTest : undefined}
            >
                ${editing ? editForm : null}
            </${CredentialCard}>
            ${feedback && html`
                <div style=${{ padding: '4px 16px 0' }}>
                    <${FeedbackBanner} message=${feedback.message} type=${feedback.type} />
                </div>
            `}
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// Profile — two-column grid
// ══════════════════════════════════════════════════════════════════════════

function ProfileCard({ profile, onSaved }) {
    const [name, setName] = useState(profile.name || '');
    const [email, setEmail] = useState(profile.email || '');
    const [timezone, setTimezone] = useState(profile.timezone || '');
    const [saving, setSaving] = useState(false);
    const [feedback, setFeedback] = useState(null);

    const handleSave = useCallback(async () => {
        setSaving(true);
        setFeedback(null);
        try {
            const updates = {};
            if (name !== (profile.name || '')) updates.name = name;
            if (email !== (profile.email || '')) updates.email = email;
            if (timezone !== (profile.timezone || '')) updates.timezone = timezone;
            if (Object.keys(updates).length === 0) {
                setFeedback({ type: 'success', message: 'No changes' });
                setSaving(false);
                return;
            }
            await api.patchUserSettings(updates);
            setFeedback({ type: 'success', message: 'Profile updated' });
            if (onSaved) onSaved();
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setSaving(false);
        }
    }, [name, email, timezone, profile, onSaved]);

    return html`
        <div style=${styles.card}>
            <div style=${{ ...styles.cardTitle, marginBottom: '14px' }}>Profile</div>

            <${FormRow}>
                <${FormField} label="Name">
                    <input type="text"
                        style=${styles.input}
                        value=${name}
                        onInput=${(e) => setName(e.target.value)} />
                </${FormField}>
                <${FormField} label="Email">
                    <input type="email"
                        style=${styles.input}
                        value=${email}
                        onInput=${(e) => setEmail(e.target.value)} />
                </${FormField}>
            </${FormRow}>

            <${FormField} label="Timezone">
                <select
                    style=${styles.select}
                    value=${timezone}
                    onChange=${(e) => setTimezone(e.target.value)}>
                    <option value="">Select timezone…</option>
                    ${TIMEZONES.map(tz => html`<option key=${tz} value=${tz}>${tz}</option>`)}
                </select>
            </${FormField}>

            <button
                style=${{
                    ...styles.button,
                    opacity: saving ? 0.5 : 1,
                    cursor: saving ? 'not-allowed' : 'pointer',
                }}
                onClick=${handleSave}
                disabled=${saving}>
                ${saving ? 'Saving…' : 'Save profile'}
            </button>

            <${FeedbackBanner} message=${feedback?.message} type=${feedback?.type} />
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// Change Password — contained width
// ══════════════════════════════════════════════════════════════════════════

function ChangePasswordCard() {
    const [currentPassword, setCurrentPassword] = useState('');
    const [newPassword, setNewPassword] = useState('');
    const [confirmPassword, setConfirmPassword] = useState('');
    const [saving, setSaving] = useState(false);
    const [feedback, setFeedback] = useState(null);

    const handleSubmit = useCallback(async () => {
        setFeedback(null);
        if (!currentPassword || !newPassword) {
            setFeedback({ type: 'error', message: 'All fields are required' });
            return;
        }
        if (newPassword !== confirmPassword) {
            setFeedback({ type: 'error', message: 'New passwords do not match' });
            return;
        }
        if (newPassword.length < 8) {
            setFeedback({ type: 'error', message: 'New password must be at least 8 characters' });
            return;
        }
        setSaving(true);
        try {
            await api.changePassword({ current_password: currentPassword, new_password: newPassword });
            setCurrentPassword('');
            setNewPassword('');
            setConfirmPassword('');
            setFeedback({ type: 'success', message: 'Password changed' });
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setSaving(false);
        }
    }, [currentPassword, newPassword, confirmPassword]);

    const allFilled = currentPassword && newPassword && confirmPassword;

    return html`
        <div style=${styles.card}>
            <div style=${{ ...styles.cardTitle, marginBottom: '14px' }}>Change password</div>

            <div style=${{ maxWidth: '280px' }}>
                <${FormField} label="Current password">
                    <input type="password"
                        style=${styles.input}
                        value=${currentPassword}
                        onInput=${(e) => setCurrentPassword(e.target.value)} />
                </${FormField}>
                <${FormField} label="New password">
                    <input type="password"
                        style=${styles.input}
                        value=${newPassword}
                        onInput=${(e) => setNewPassword(e.target.value)} />
                </${FormField}>
                <${FormField} label="Confirm new password">
                    <input type="password"
                        style=${styles.input}
                        value=${confirmPassword}
                        onInput=${(e) => setConfirmPassword(e.target.value)}
                        onKeyDown=${(e) => e.key === 'Enter' && handleSubmit()} />
                </${FormField}>
            </div>

            <button
                style=${{
                    ...styles.button,
                    opacity: (saving || !allFilled) ? 0.5 : 1,
                    cursor: (saving || !allFilled) ? 'not-allowed' : 'pointer',
                }}
                onClick=${handleSubmit}
                disabled=${saving || !allFilled}>
                ${saving ? 'Updating…' : 'Update password'}
            </button>

            <${FeedbackBanner} message=${feedback?.message} type=${feedback?.type} />
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// Main Settings component
// ══════════════════════════════════════════════════════════════════════════

export function Settings() {
    const [push, setPush] = useState({ supported: false, subscribed: false, subscription: null });
    const [pushLoading, setPushLoading] = useState(false);
    const [pushError, setPushError] = useState(null);
    const [settings, setSettings] = useState(null);
    const [settingsSaving, setSettingsSaving] = useState(false);
    const [settingsError, setSettingsError] = useState(null);
    const [saved, setSaved] = useState(false);

    const [userSettings, setUserSettings] = useState(null);
    const [userLoading, setUserLoading] = useState(true);
    const [userError, setUserError] = useState(null);

    const [instanceSettings, setInstanceSettings] = useState(null);
    const [instanceLoading, setInstanceLoading] = useState(false);
    const [instanceError, setInstanceError] = useState(null);

    const isAdmin = userSettings?.profile?.role === 'owner' || userSettings?.profile?.role === 'admin';

    const loadUserSettings = useCallback(async () => {
        try {
            const data = await api.getUserSettings();
            setUserSettings(data);
            setUserError(null);
        } catch (e) {
            setUserError(e.message);
        } finally {
            setUserLoading(false);
        }
    }, []);

    const loadInstanceSettings = useCallback(async () => {
        setInstanceLoading(true);
        try {
            const data = await api.getInstanceSettings();
            setInstanceSettings(data);
            setInstanceError(null);
        } catch (e) {
            if (!e.message.includes('Forbidden')) {
                setInstanceError(e.message);
            }
        } finally {
            setInstanceLoading(false);
        }
    }, []);

    useEffect(() => {
        loadUserSettings();
        getPushState().then(setPush);
        api.getNotificationSettings()
            .then(setSettings)
            .catch(e => setSettingsError(e.message));
    }, []);

    useEffect(() => {
        if (isAdmin) loadInstanceSettings();
    }, [isAdmin]);

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
            const result = await api.updateNotificationSettings({ [key]: value });
            setSettings(result);
            setSaved(true);
            setTimeout(() => setSaved(false), 2000);
        } catch (e) {
            setSettingsError(e.message);
            setSettings(settings);
        } finally {
            setSettingsSaving(false);
        }
    }, [settings]);

    const notifDisabled = !push.subscribed || settingsSaving;

    if (userLoading) {
        return html`<div style=${{ padding: '24px', maxWidth: '800px' }}>
            <div style=${{ display: 'flex', alignItems: 'center', gap: '12px', padding: '32px 0' }}>
                <span class="loading-spinner"></span>
                <span style=${{ fontSize: '13px', color: colors.textSecondary }}>Loading settings…</span>
            </div>
        </div>`;
    }

    return html`
        <div style=${{ padding: '24px', maxWidth: '800px' }}>

            ${userError && html`
                <div style=${{ fontSize: '13px', color: colors.red, marginBottom: '16px' }}>
                    Failed to load user settings: ${userError}
                </div>
            `}

            <!-- ═══ INSTANCE section (admin/owner only) ═══ -->
            ${isAdmin && html`
                <div style=${{ marginBottom: '28px' }}>
                    <${SectionHeader} text="Instance" />

                    ${instanceLoading && html`
                        <div style=${{ display: 'flex', alignItems: 'center', gap: '12px', padding: '16px 0' }}>
                            <span class="loading-spinner"></span>
                            <span style=${{ fontSize: '13px', color: colors.textSecondary }}>Loading…</span>
                        </div>
                    `}

                    ${instanceError && html`
                        <div style=${{ fontSize: '13px', color: colors.red, marginBottom: '12px' }}>
                            ${instanceError}
                        </div>
                    `}

                    ${instanceSettings && html`
                        <${GitHubCard}
                            github=${instanceSettings.github}
                            onSaved=${loadInstanceSettings}
                        />
                        <${OAuthCard}
                            oauth=${instanceSettings.oauth}
                            onRegenerated=${loadInstanceSettings}
                        />
                    `}
                </div>
            `}

            <!-- ═══ ACCOUNT section ═══ -->
            <div style=${{ marginBottom: '28px' }}>
                <${SectionHeader} text="Account" />

                ${userSettings && html`
                    <${AnthropicKeyCard}
                        anthropic=${userSettings.anthropic}
                        onSaved=${loadUserSettings}
                    />
                    <${ProfileCard}
                        profile=${userSettings.profile}
                        onSaved=${loadUserSettings}
                    />
                    <${ChangePasswordCard} />
                `}
            </div>

            <!-- ═══ NOTIFICATIONS section ═══ -->
            <div style=${{ marginBottom: '28px' }}>
                <${SectionHeader} text="Notifications" />

                <div style=${styles.card}>
                    ${!push.supported && html`
                        <div style=${{ fontSize: '13px', color: colors.yellow }}>
                            Push notifications are not supported in this browser.
                        </div>
                    `}

                    ${push.supported && !push.serverConfigured && html`
                        <div style=${{ fontSize: '13px', color: colors.yellow }}>
                            Push notifications require server configuration (VAPID keys not set).
                        </div>
                    `}

                    ${push.supported && push.serverConfigured && html`
                        <div style=${{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '12px' }}>
                            <div>
                                <div style=${styles.cardTitle}>Push notifications</div>
                                <div style=${{ fontSize: '12px', color: colors.textTertiary, marginTop: '4px' }}>
                                    Browser notifications when tasks complete, fail, or need attention
                                </div>
                            </div>
                            <${Toggle}
                                checked=${push.subscribed}
                                onChange=${handlePushToggle}
                                disabled=${pushLoading}
                            />
                        </div>

                        ${pushError && html`
                            <div style=${{ fontSize: '12px', color: colors.red, marginTop: '8px' }}>
                                ${pushError}
                            </div>
                        `}

                        ${settings && html`
                            <div style=${{
                                marginTop: '14px',
                                paddingTop: '12px',
                                borderTop: `0.5px solid ${colors.borderSubtle}`,
                                display: 'flex',
                                flexDirection: 'column',
                                gap: '10px',
                            }}>
                                ${saved && html`
                                    <span style=${{ fontSize: '11px', color: colors.green, alignSelf: 'flex-end' }}>Saved</span>
                                `}
                                ${settingsError && html`
                                    <div style=${{ fontSize: '12px', color: colors.red }}>${settingsError}</div>
                                `}
                                <${NotifCheckbox}
                                    label="Task failed"
                                    description="Error or exception"
                                    checked=${!!settings.notify_failed}
                                    disabled=${notifDisabled}
                                    onChange=${(v) => handleSettingToggle('notify_failed', v)}
                                />
                                <${NotifCheckbox}
                                    label="Needs review"
                                    description="Timed out or lost process"
                                    checked=${!!settings.notify_needs_review}
                                    disabled=${notifDisabled}
                                    onChange=${(v) => handleSettingToggle('notify_needs_review', v)}
                                />
                                <${NotifCheckbox}
                                    label="CC posted a question"
                                    description="Waiting for your input"
                                    checked=${!!settings.notify_question}
                                    disabled=${notifDisabled}
                                    onChange=${(v) => handleSettingToggle('notify_question', v)}
                                />
                                <${NotifCheckbox}
                                    label="Task completed"
                                    description="Off by default to reduce noise"
                                    checked=${!!settings.notify_completed}
                                    disabled=${notifDisabled}
                                    onChange=${(v) => handleSettingToggle('notify_completed', v)}
                                />
                            </div>
                        `}

                        ${!settings && !settingsError && html`
                            <div style=${{ fontSize: '13px', color: colors.textSecondary, marginTop: '12px' }}>
                                Loading notification preferences…
                            </div>
                        `}
                    `}
                </div>
            </div>
        </div>
    `;
}
