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
    const color = type === 'success' ? colors.green
                : type === 'warning' ? colors.yellow
                : type === 'info' ? colors.blue
                : colors.red;
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
// Git provider credential cards
// ══════════════════════════════════════════════════════════════════════════

const GIT_PROVIDER_CONFIG = {
    github: {
        icon: '🐙',
        name: 'GitHub',
        defaultHostname: 'github.com',
        credentialLabel: 'Personal Access Token',
        credentialPlaceholder: 'ghp_xxxxxxxxxxxx',
        scopeText: 'Classic: repo (full control). Fine-grained: Contents + PRs (r/w), Metadata (read).',
        createLinks: [
            { label: 'Create classic token', url: 'https://github.com/settings/tokens/new?scopes=repo&description=Ouvrage' },
            { label: 'Create fine-grained token', url: 'https://github.com/settings/personal-access-tokens/new' },
        ],
    },
    gitlab: {
        icon: '🦊',
        name: 'GitLab',
        defaultHostname: 'gitlab.com',
        credentialLabel: 'Personal Access Token',
        credentialPlaceholder: 'glpat-xxxxxxxxxxxx',
        scopeText: 'Scopes: api or read_repository + write_repository.',
        createLinks: [
            { label: 'Create token', url: 'https://gitlab.com/-/user_settings/personal_access_tokens' },
        ],
    },
    bitbucket: {
        icon: '🪣',
        name: 'Bitbucket',
        defaultHostname: 'bitbucket.org',
        credentialLabel: 'API Token',
        credentialPlaceholder: 'email@example.com:ATBBxxxxxxxxx',
        scopeText: 'Required scopes: read:repository:bitbucket, write:repository:bitbucket, read:pullrequest:bitbucket, write:pullrequest:bitbucket, read:user:bitbucket. Format: email:api_token',
        createLinks: [
            { label: 'Create API token', url: 'https://id.atlassian.com/manage-profile/security/api-tokens' },
        ],
    },
};

function GitProviderCard({ cred, onSaved }) {
    const cfg = GIT_PROVIDER_CONFIG[cred.provider];
    const [credential, setCredential] = useState('');
    const [hostname, setHostname] = useState(cred.hostname || cfg.defaultHostname);
    const [saving, setSaving] = useState(false);
    const [testing, setTesting] = useState(false);
    const [removing, setRemoving] = useState(false);
    const [feedback, setFeedback] = useState(null);
    const [editing, setEditing] = useState(!cred.configured);
    const [authWarning, setAuthWarning] = useState(false);

    const handleSave = useCallback(async () => {
        if (!credential.trim()) return;
        setSaving(true);
        setFeedback(null);
        try {
            const result = await api.putGitCredential(cred.provider, {
                credential: credential.trim(),
                hostname: hostname.trim() || cfg.defaultHostname,
            });
            setCredential('');
            setEditing(false);
            if (result.warning) {
                setAuthWarning(true);
                setFeedback({ type: 'warning', message: result.warning });
            } else {
                setAuthWarning(false);
                const extra = result.username ? ` — authenticated as ${result.username}` : '';
                setFeedback({ type: 'success', message: `${cfg.name} credential saved${extra}` });
            }
            if (onSaved) onSaved();
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setSaving(false);
        }
    }, [credential, hostname, cred.provider, cfg, onSaved]);

    const handleTest = useCallback(async () => {
        setTesting(true);
        setFeedback(null);
        try {
            const result = await api.testGitCredential(cred.provider);
            if (result.ok) {
                setFeedback({ type: 'success', message: result.message || `Connected as ${result.username || '(unknown)'}` });
            } else {
                setFeedback({ type: 'error', message: result.message || 'Connection failed' });
            }
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setTesting(false);
        }
    }, [cred.provider]);

    const handleRemove = useCallback(async () => {
        setRemoving(true);
        setFeedback(null);
        try {
            await api.deleteGitCredential(cred.provider);
            setFeedback({ type: 'success', message: `${cfg.name} credential removed` });
            if (onSaved) onSaved();
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setRemoving(false);
        }
    }, [cred.provider, cfg, onSaved]);

    const isNonDefault = hostname !== cfg.defaultHostname;

    // View-mode status shown in CredentialCard row
    const statusText = !editing
        ? (cred.configured ? undefined : 'Not connected')
        : undefined;
    const maskedValue = (!editing && cred.credential_last4)
        ? `····${cred.credential_last4}`
        : undefined;

    const editForm = html`
        <div>
            <!-- Hostname + credential side by side -->
            <div style=${{ display: 'flex', gap: '8px', alignItems: 'flex-end' }}>
                <div style=${{ width: '160px', flexShrink: 0 }}>
                    <label style=${{ ...styles.label, marginBottom: '4px' }}>
                        Hostname
                        ${isNonDefault && html`
                            <span style=${{ color: '#f59e0b', marginLeft: '4px' }}>●</span>
                        `}
                    </label>
                    <input type="text"
                        style=${{ ...styles.input, color: isNonDefault ? '#f59e0b' : colors.text }}
                        value=${hostname}
                        onInput=${(e) => setHostname(e.target.value)}
                        placeholder=${cfg.defaultHostname} />
                </div>
                <div style=${{ flex: 1, minWidth: 0 }}>
                    <label style=${{ ...styles.label, marginBottom: '4px' }}>${cfg.credentialLabel}</label>
                    <input type="password"
                        style=${styles.input}
                        placeholder=${cfg.credentialPlaceholder}
                        value=${credential}
                        onInput=${(e) => setCredential(e.target.value)}
                        onKeyDown=${(e) => e.key === 'Enter' && handleSave()} />
                </div>
                <button
                    style=${{
                        ...styles.buttonPrimary,
                        opacity: (saving || !credential.trim()) ? 0.5 : 1,
                        cursor: (saving || !credential.trim()) ? 'not-allowed' : 'pointer',
                        whiteSpace: 'nowrap',
                        flexShrink: 0,
                    }}
                    onClick=${handleSave}
                    disabled=${saving || !credential.trim()}>
                    ${saving ? 'Saving…' : 'Save'}
                </button>
                ${cred.configured && html`
                    <button
                        style=${{ ...styles.button, flexShrink: 0 }}
                        onClick=${() => { setEditing(false); setCredential(''); setHostname(cred.hostname || cfg.defaultHostname); setFeedback(null); }}>
                        Cancel
                    </button>
                    <button
                        style=${{ ...styles.button, color: '#dc3545', flexShrink: 0 }}
                        onClick=${handleRemove}
                        disabled=${removing}>
                        ${removing ? 'Removing…' : 'Remove'}
                    </button>
                `}
            </div>
            <!-- Scope help + create links -->
            <div style=${{ fontSize: '11px', color: colors.textTertiary, marginTop: '6px', lineHeight: '1.5' }}>
                ${cfg.scopeText}
                ${cfg.createLinks.map((lnk, i) => html`
                    ${i > 0 ? ' · ' : ' '}
                    <a href=${lnk.url} target="_blank" rel="noopener"
                        style=${{ color: colors.accent, textDecoration: 'none' }}>
                        ${lnk.label} →
                    </a>
                `)}
            </div>
            ${isNonDefault && html`
                <div style=${{ fontSize: '11px', color: '#f59e0b', marginTop: '4px' }}>
                    Custom host — auto-detection will match this
                </div>
            `}
        </div>
    `;

    // In view mode, show hostname in amber if non-default
    const nameExtra = !editing && isNonDefault
        ? html` <span style=${{ fontSize: '11px', color: '#f59e0b', fontWeight: 'normal', marginLeft: '4px' }}>${hostname}</span>`
        : (!editing && cred.configured)
            ? html` <span style=${{ fontSize: '11px', color: colors.textTertiary, fontWeight: 'normal', marginLeft: '4px' }}>${hostname}</span>`
            : null;

    const displayName = html`<span>${cfg.name}${nameExtra}</span>`;

    return html`
        <div style=${{ marginBottom: '12px' }}>
            <${CredentialCard}
                icon=${cfg.icon}
                name=${displayName}
                connected=${cred.configured}
                warning=${authWarning}
                statusText=${statusText}
                maskedValue=${maskedValue}
                onUpdate=${editing ? undefined : () => { setHostname(cred.hostname || cfg.defaultHostname); setEditing(true); }}
                onTest=${(!editing && !testing && cred.configured) ? handleTest : undefined}
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

function GitProvidersSection({ onSaved }) {
    const [credentials, setCredentials] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    const load = useCallback(async () => {
        try {
            const data = await api.getGitCredentials();
            setCredentials(data.credentials);
            setError(null);
        } catch (e) {
            setError(e.message);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { load(); }, []);

    const handleSaved = useCallback(async () => {
        await load();
        if (onSaved) onSaved();
    }, [load, onSaved]);

    if (loading) return html`
        <div style=${{ display: 'flex', alignItems: 'center', gap: '8px', padding: '12px 0' }}>
            <span class="loading-spinner"></span>
            <span style=${{ fontSize: '13px', color: colors.textSecondary }}>Loading…</span>
        </div>
    `;

    if (error) return html`
        <div style=${{ fontSize: '13px', color: colors.red, marginBottom: '12px' }}>${error}</div>
    `;

    return html`
        <div id="instance-git-credentials">
            <div style=${{ fontSize: '11px', color: colors.textTertiary, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: '8px' }}>
                Instance Git Credentials
            </div>
            ${credentials && credentials.map(cred => html`
                <${GitProviderCard}
                    key=${cred.provider}
                    cred=${cred}
                    onSaved=${handleSaved}
                />
            `)}
            <div style=${{ fontSize: '11px', color: colors.textTertiary, marginTop: '4px', lineHeight: '1.5' }}>
                One credential per provider. Custom hostnames (self-hosted GitLab, etc.) are matched automatically when creating projects — no per-project selection needed.
            </div>
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// Setup banner — shown when credentials are missing
// ══════════════════════════════════════════════════════════════════════════

const DOCS_URL = 'https://ouvrage.build/docs/getting-started';

function SetupBanner({ anthropic, git_credential }) {
    if (!anthropic || !git_credential) return null;
    if (anthropic.skip_credential_check) return null;

    const anthropicDone = anthropic.configured;
    const gitDone = git_credential.configured;
    if (anthropicDone && gitDone) return null;

    const scrollToGitCredentials = (e) => {
        e.preventDefault();
        const el = document.getElementById('instance-git-credentials');
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    };

    return html`
        <div style=${{
            border: `1px solid ${colors.yellow || '#d97706'}`,
            borderRadius: '8px',
            padding: '16px 20px',
            marginBottom: '24px',
            background: 'rgba(217, 119, 6, 0.06)',
        }}>
            <div style=${{
                fontSize: '13px',
                fontWeight: '600',
                color: colors.text,
                marginBottom: '10px',
            }}>
                ⚠️ Complete your setup to start using Ouvrage
            </div>
            <div style=${{ display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: '10px' }}>
                <div style=${{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '13px', color: colors.text }}>
                    <span style=${{ color: anthropicDone ? colors.green : colors.textTertiary, fontSize: '14px' }}>
                        ${anthropicDone ? '✓' : '☐'}
                    </span>
                    <span style=${{ color: anthropicDone ? colors.textSecondary : colors.text }}>
                        Anthropic API Key${anthropicDone ? ' — configured' : ' — required to dispatch workers'}
                    </span>
                </div>
                <div style=${{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '13px', color: colors.text }}>
                    <span style=${{ color: gitDone ? colors.green : colors.textTertiary, fontSize: '14px' }}>
                        ${gitDone ? '✓' : '☐'}
                    </span>
                    <span style=${{ color: gitDone ? colors.textSecondary : colors.text }}>
                        Git credential${gitDone ? ' — configured' : ' — required to connect your repos'}
                    </span>
                </div>
            </div>
            <div style=${{ fontSize: '12px', color: colors.textTertiary }}>
                <a href="#instance-git-credentials"
                    onClick=${scrollToGitCredentials}
                    style=${{ color: colors.accent, textDecoration: 'none' }}>
                    Set these up below
                </a>, then you're ready to go.
            </div>
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

    const mcpUrl = window.location.origin + '/mcp';

    if (!oauth.client_id) {
        return html`
            <div style=${styles.card}>
                <div style=${styles.cardTitle}>MCP Connection</div>
                <div style=${{ ...styles.cardSubtitle, marginTop: '4px' }}>OAuth client not configured.</div>
            </div>
        `;
    }

    return html`
        <div style=${styles.card}>
            <div style=${{ ...styles.cardTitle, marginBottom: '4px' }}>Connect to Ouvrage</div>
            <div style=${{ fontSize: '12px', color: colors.textSecondary, marginBottom: '14px' }}>
                Use these credentials to connect Claude.ai or any MCP client to your Ouvrage instance
            </div>

            <${SecretRow} label="MCP Endpoint URL" value=${mcpUrl} alwaysVisible=${true} />
            <${SecretRow} label="Client ID" value=${oauth.client_id} alwaysVisible=${true} />
            <${SecretRow} label="Client secret" value=${oauth.client_secret} />

            <div style=${{
                marginTop: '14px',
                padding: '12px 14px',
                background: colors.surface,
                border: `0.5px solid ${colors.border}`,
                borderRadius: '6px',
                fontSize: '12px',
                color: colors.textSecondary,
                lineHeight: '1.6',
            }}>
                <div style=${{ fontWeight: '500', color: colors.text, marginBottom: '6px' }}>
                    To connect Claude.ai:
                </div>
                <ol style=${{ margin: '0', paddingLeft: '18px', display: 'flex', flexDirection: 'column', gap: '2px' }}>
                    <li>Open Claude.ai Settings → Integrations</li>
                    <li>Add a new MCP connector</li>
                    <li>Enter your MCP URL and credentials above</li>
                </ol>
                <div style=${{ marginTop: '8px' }}>
                    <a href=${DOCS_URL} target="_blank" rel="noopener"
                        style=${{ color: colors.accent, textDecoration: 'none', fontSize: '12px' }}>
                        See detailed setup instructions →
                    </a>
                </div>
            </div>

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

    const handleRemove = useCallback(async () => {
        setSaving(true);
        setFeedback(null);
        try {
            await api.patchUserSettings({ anthropic_api_key: '' });
            setKey('');
            setEditing(false);
            setFeedback({ type: 'success', message: 'API key removed' });
            if (onSaved) onSaved();
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setSaving(false);
        }
    }, [onSaved]);

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
        ? (anthropic.configured
            ? 'Configured'
            : anthropic.skip_credential_check
                ? 'Not set'
                : 'Not set — required to dispatch tasks')
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
                <button
                    style=${{ ...styles.button, color: 'var(--color-danger, #dc3545)' }}
                    onClick=${handleRemove}
                    disabled=${saving}>
                    Remove
                </button>
            `}
        </div>
    `;

    return html`
        <div style=${{ marginBottom: '12px' }}>
            <${CredentialCard}
                icon="🔑"
                name="Anthropic API key"
                connected=${anthropic.configured || anthropic.skip_credential_check}
                statusText=${statusText}
                maskedValue=${maskedValue}
                onUpdate=${editing ? undefined : () => setEditing(true)}
                onTest=${(!editing && !testing && anthropic.configured) ? handleTest : undefined}
            >
                ${editing ? editForm : null}
            </${CredentialCard}>
            ${anthropic.skip_credential_check && html`
                <div style=${{ padding: '4px 16px 0' }}>
                    <${FeedbackBanner} message="Optional — not required for Claude Code subscriptions" type="info" />
                </div>
            `}
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
// API Tokens section
// ══════════════════════════════════════════════════════════════════════════

function ApiTokensSection() {
    const [tokens, setTokens] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [creating, setCreating] = useState(false);
    const [newTokenName, setNewTokenName] = useState('');
    const [newToken, setNewToken] = useState(null); // raw token shown once
    const [copied, setCopied] = useState(false);
    const [revokeError, setRevokeError] = useState(null);

    const loadTokens = useCallback(async () => {
        try {
            const data = await api.listTokens();
            setTokens(data.tokens || []);
            setError(null);
        } catch (e) {
            setError(e.message);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { loadTokens(); }, []);

    const handleGenerate = useCallback(async () => {
        setCreating(true);
        setError(null);
        try {
            const result = await api.createToken(newTokenName.trim() || null);
            setNewToken(result);
            setNewTokenName('');
            await loadTokens();
        } catch (e) {
            setError(e.message);
        } finally {
            setCreating(false);
        }
    }, [newTokenName, loadTokens]);

    const handleCopy = useCallback(async () => {
        if (!newToken?.token) return;
        try {
            await navigator.clipboard.writeText(newToken.token);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        } catch (_) {}
    }, [newToken]);

    const handleDismiss = useCallback(() => {
        setNewToken(null);
        setCopied(false);
    }, []);

    const handleRevoke = useCallback(async (tokenId) => {
        setRevokeError(null);
        try {
            await api.revokeToken(tokenId);
            await loadTokens();
        } catch (e) {
            setRevokeError(e.message);
        }
    }, [loadTokens]);

    const formatDate = (iso) => {
        if (!iso) return '—';
        try {
            return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
        } catch { return iso; }
    };

    return html`
        <div style=${{ marginBottom: '28px' }}>
            <${SectionHeader} text="API Tokens" />

            <!-- One-time token reveal card -->
            ${newToken && html`
                <div style=${{
                    ...styles.card,
                    border: `1px solid ${colors.accent}`,
                    marginBottom: '12px',
                    background: colors.surface,
                }}>
                    <div style=${{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
                        <span style=${{ fontSize: '14px' }}>🔑</span>
                        <span style=${{ fontSize: '13px', fontWeight: '600', color: colors.text }}>
                            Token created${newToken.name ? `: ${newToken.name}` : ''}
                        </span>
                    </div>
                    <div style=${{
                        fontSize: '12px',
                        color: colors.yellow || '#d97706',
                        marginBottom: '10px',
                    }}>
                        ⚠ Copy this token now. You won't be able to see it again.
                    </div>
                    <div style=${{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '8px',
                        background: colors.bg || '#0d1117',
                        border: `1px solid ${colors.border}`,
                        borderRadius: '6px',
                        padding: '8px 10px',
                        marginBottom: '10px',
                    }}>
                        <code style=${{
                            fontFamily: 'monospace',
                            fontSize: '12px',
                            color: colors.text,
                            flex: 1,
                            wordBreak: 'break-all',
                        }}>${newToken.token}</code>
                        <button
                            style=${{
                                ...styles.buttonPrimary,
                                padding: '4px 12px',
                                fontSize: '11px',
                                whiteSpace: 'nowrap',
                            }}
                            onClick=${handleCopy}>
                            ${copied ? '✓ Copied' : 'Copy'}
                        </button>
                    </div>
                    <button
                        style=${{ ...styles.button, fontSize: '11px', padding: '4px 12px' }}
                        onClick=${handleDismiss}>
                        Done, I've saved it
                    </button>
                </div>
            `}

            <!-- Token list -->
            <div style=${styles.card}>
                <div style=${{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    marginBottom: '14px',
                }}>
                    <div style=${styles.cardTitle}>Your API tokens</div>
                </div>

                ${loading && html`
                    <div style=${{ display: 'flex', alignItems: 'center', gap: '8px', padding: '12px 0' }}>
                        <span class="loading-spinner"></span>
                        <span style=${{ fontSize: '13px', color: colors.textSecondary }}>Loading tokens…</span>
                    </div>
                `}

                ${error && html`
                    <div style=${{ fontSize: '12px', color: colors.red, marginBottom: '12px' }}>${error}</div>
                `}

                ${revokeError && html`
                    <div style=${{ fontSize: '12px', color: colors.red, marginBottom: '8px' }}>${revokeError}</div>
                `}

                ${!loading && tokens && tokens.length === 0 && html`
                    <div style=${{ fontSize: '13px', color: colors.textTertiary, padding: '8px 0 12px' }}>
                        No tokens yet. Generate one to connect Claude Desktop, scripts, or other MCP clients.
                    </div>
                `}

                ${!loading && tokens && tokens.length > 0 && html`
                    <div style=${{ marginBottom: '14px' }}>
                        ${tokens.map(t => html`
                            <div key=${t.id} style=${{
                                display: 'flex',
                                alignItems: 'center',
                                gap: '10px',
                                padding: '8px 0',
                                borderBottom: `0.5px solid ${colors.borderSubtle || colors.border}`,
                            }}>
                                <div style=${{ flex: 1, minWidth: 0 }}>
                                    <div style=${{ fontSize: '13px', color: colors.text, fontWeight: '500' }}>
                                        ${t.name || html`<span style=${{ color: colors.textTertiary, fontStyle: 'italic' }}>Unnamed</span>`}
                                    </div>
                                    <div style=${{ display: 'flex', gap: '12px', marginTop: '2px' }}>
                                        ${t.token_prefix && html`
                                            <span style=${{
                                                fontFamily: 'monospace',
                                                fontSize: '11px',
                                                color: colors.textTertiary,
                                            }}>${t.token_prefix}…</span>
                                        `}
                                        <span style=${{ fontSize: '11px', color: colors.textTertiary }}>
                                            Created ${formatDate(t.created_at)}
                                        </span>
                                        <span style=${{ fontSize: '11px', color: colors.textTertiary }}>
                                            ${t.last_used_at ? `Last used ${formatDate(t.last_used_at)}` : 'Never used'}
                                        </span>
                                    </div>
                                </div>
                                <${ConfirmAction}
                                    label="Revoke"
                                    confirmLabel="Yes, revoke"
                                    warningText="Revoke this token?"
                                    onConfirm=${() => handleRevoke(t.id)}
                                    danger=${true}
                                />
                            </div>
                        `)}
                    </div>
                `}

                <!-- Generate token row -->
                <div style=${{ display: 'flex', gap: '8px', alignItems: 'center', paddingTop: '4px' }}>
                    <input
                        type="text"
                        style=${{ ...styles.input, flex: 1, maxWidth: '220px' }}
                        placeholder="Token name (optional)"
                        value=${newTokenName}
                        onInput=${(e) => setNewTokenName(e.target.value)}
                        onKeyDown=${(e) => e.key === 'Enter' && !creating && handleGenerate()}
                    />
                    <button
                        style=${{
                            ...styles.buttonPrimary,
                            opacity: creating ? 0.5 : 1,
                            cursor: creating ? 'not-allowed' : 'pointer',
                            whiteSpace: 'nowrap',
                        }}
                        onClick=${handleGenerate}
                        disabled=${creating}>
                        ${creating ? 'Generating…' : 'Generate Token'}
                    </button>
                </div>
            </div>
        </div>
    `;
}


// ══════════════════════════════════════════════════════════════════════════
// Runtime Environment section
// ══════════════════════════════════════════════════════════════════════════

function RuntimeSection() {
    const [runtimes, setRuntimes] = useState(null);
    const [error, setError] = useState(null);

    useEffect(() => {
        api.getRuntimeInfo()
            .then(setRuntimes)
            .catch(e => setError(e.message));
    }, []);

    return html`
        <div style=${{ marginBottom: '28px' }}>
            <${SectionHeader} text="Available Runtimes" />

            <div style=${styles.card}>
                ${error && html`
                    <div style=${{ fontSize: '13px', color: colors.yellow }}>${error}</div>
                `}

                ${!runtimes && !error && html`
                    <div style=${{ fontSize: '13px', color: colors.textSecondary }}>
                        Loading runtimes…
                    </div>
                `}

                ${runtimes && html`
                    <div style=${{
                        display: 'grid',
                        gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
                        gap: '8px',
                        marginBottom: '14px',
                    }}>
                        ${runtimes.map(r => html`
                            <div key=${r.key} style=${{
                                background: colors.surface,
                                border: `0.5px solid ${colors.borderSubtle}`,
                                borderRadius: '6px',
                                padding: '10px 12px',
                            }}>
                                <div style=${{
                                    fontSize: '13px',
                                    fontWeight: 500,
                                    color: colors.text,
                                    marginBottom: '3px',
                                }}>${r.name}</div>
                                <div style=${{
                                    fontSize: '12px',
                                    color: r.version === 'not installed' ? colors.textTertiary : colors.textSecondary,
                                    fontStyle: r.version === 'not installed' ? 'italic' : 'normal',
                                }}>${r.version}</div>
                                ${r.pkg_manager && html`
                                    <div style=${{
                                        fontSize: '11px',
                                        color: colors.textTertiary,
                                        marginTop: '2px',
                                    }}>${r.pkg_manager}</div>
                                `}
                            </div>
                        `)}
                    </div>

                    <div style=${{
                        fontSize: '12px',
                        color: colors.textTertiary,
                        lineHeight: 1.5,
                        borderTop: `0.5px solid ${colors.borderSubtle}`,
                        paddingTop: '10px',
                    }}>
                        These runtimes are pre-installed in your Ouvrage instance.
                        Use the project's Setup Command to install your project's specific dependencies
                        (e.g. <code style=${{ fontFamily: 'monospace', color: colors.textSecondary }}>composer install</code>,
                        <code style=${{ fontFamily: 'monospace', color: colors.textSecondary }}>pip install -r requirements.txt</code>).
                    </div>
                `}
            </div>
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

            <!-- ═══ SETUP BANNER (shown when credentials missing) ═══ -->
            ${userSettings && html`
                <${SetupBanner}
                    anthropic=${userSettings.anthropic}
                    git_credential=${userSettings.git_credential}
                />
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

                    <${GitProvidersSection}
                        onSaved=${loadUserSettings}
                    />
                    ${instanceSettings && html`
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
                    ${!isAdmin && html`
                        <div style=${{ marginBottom: '12px' }}>
                            <${CredentialCard}
                                icon="🔑"
                                name="GitHub PAT"
                                connected=${userSettings.github?.configured ?? false}
                                statusText=${(userSettings.github?.configured)
                                    ? 'Configured'
                                    : 'Not set — required to create projects (configured by admin)'}
                            />
                        </div>
                    `}
                    <${ProfileCard}
                        profile=${userSettings.profile}
                        onSaved=${loadUserSettings}
                    />
                    <${ChangePasswordCard} />
                `}
            </div>

            <!-- ═══ API TOKENS section ═══ -->
            <${ApiTokensSection} />

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

            <!-- ═══ AVAILABLE RUNTIMES section ═══ -->
            <${RuntimeSection} />
        </div>
    `;
}
