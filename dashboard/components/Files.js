// Files Manager — upload, list, rename, delete files
import { html } from './utils.js';
import { useState, useEffect, useCallback, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { styles, SectionHeader, ConfirmAction } from './FormKit.js';
import { colors, typography, spacing, layout, animation } from '../tokens.js';

// ── Helpers ──────────────────────────────────────────────────────────────────

function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function timeAgo(isoString) {
    const now = Date.now();
    const then = new Date(isoString).getTime();
    const secs = Math.floor((now - then) / 1000);
    if (secs < 60) return 'just now';
    const mins = Math.floor(secs / 60);
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    if (days < 30) return `${days}d ago`;
    return new Date(isoString).toLocaleDateString();
}

function fileIcon(mime) {
    if (!mime) return '\u{1F4C4}';
    if (mime.startsWith('image/')) return '\u{1F5BC}\uFE0F';
    if (mime === 'application/pdf') return '\u{1F4D1}';
    if (mime.startsWith('text/')) return '\u{1F4C4}';
    return '\u{1F4CE}';
}

// ── Upload Zone ──────────────────────────────────────────────────────────────

function UploadZone({ onUpload }) {
    const [dragOver, setDragOver] = useState(false);
    const [uploading, setUploading] = useState(false);
    const [error, setError] = useState(null);
    const inputRef = useRef(null);

    const handleFiles = useCallback(async (fileList) => {
        if (!fileList || fileList.length === 0) return;
        setError(null);
        setUploading(true);
        try {
            for (const file of fileList) {
                await onUpload(file);
            }
        } catch (e) {
            setError(e.message);
        } finally {
            setUploading(false);
        }
    }, [onUpload]);

    const onDrop = useCallback((e) => {
        e.preventDefault();
        setDragOver(false);
        handleFiles(e.dataTransfer.files);
    }, [handleFiles]);

    const onDragOver = useCallback((e) => {
        e.preventDefault();
        setDragOver(true);
    }, []);

    const onDragLeave = useCallback(() => setDragOver(false), []);

    const zoneStyle = {
        border: `2px dashed ${dragOver ? colors.accent : colors.border}`,
        borderRadius: layout.borderRadius.lg,
        padding: `${spacing[8]} ${spacing[4]}`,
        textAlign: 'center',
        cursor: uploading ? 'wait' : 'pointer',
        transition: `border-color ${animation.durationNormal}`,
        background: dragOver ? colors.accentBg : 'transparent',
        marginBottom: spacing[6],
    };

    return html`
        <div
            style=${zoneStyle}
            onClick=${() => !uploading && inputRef.current?.click()}
            onDrop=${onDrop}
            onDragOver=${onDragOver}
            onDragLeave=${onDragLeave}
        >
            <input
                ref=${inputRef}
                type="file"
                multiple
                style=${{ display: 'none' }}
                onChange=${(e) => { handleFiles(e.target.files); e.target.value = ''; }}
            />
            ${uploading
                ? html`<span style=${{ fontSize: typography.size.sm, color: colors.textTertiary }}>Uploading...</span>`
                : html`
                    <div style=${{ fontSize: '24px', marginBottom: spacing[2] }}>\u{2B06}\uFE0F</div>
                    <div style=${{ fontSize: typography.size.sm, color: colors.textTertiary }}>
                        Drag and drop files here, or click to browse
                    </div>
                `
            }
            ${error && html`
                <div style=${{
                    marginTop: spacing[2],
                    fontSize: typography.size.xs,
                    color: colors.red,
                }}>${error}</div>
            `}
        </div>
    `;
}

// ── File Row ─────────────────────────────────────────────────────────────────

function FileRow({ file, onRename, onDelete }) {
    const [editing, setEditing] = useState(false);
    const [editValue, setEditValue] = useState(file.filename);
    const [copied, setCopied] = useState(false);
    const [hoverCopy, setHoverCopy] = useState(false);
    const inputRef = useRef(null);

    useEffect(() => {
        if (editing && inputRef.current) {
            inputRef.current.focus();
            // Select filename without extension
            const dot = editValue.lastIndexOf('.');
            inputRef.current.setSelectionRange(0, dot > 0 ? dot : editValue.length);
        }
    }, [editing]);

    const handleRename = useCallback(() => {
        const trimmed = editValue.trim();
        if (trimmed && trimmed !== file.filename) {
            onRename(file.id, trimmed);
        }
        setEditing(false);
    }, [editValue, file.id, file.filename, onRename]);

    const handleKeyDown = useCallback((e) => {
        if (e.key === 'Enter') handleRename();
        if (e.key === 'Escape') { setEditValue(file.filename); setEditing(false); }
    }, [handleRename, file.filename]);

    const handleCopy = useCallback(async () => {
        try {
            await navigator.clipboard.writeText(file.stored_path);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
        } catch (_) { /* ignore */ }
    }, [file.stored_path]);

    const rowStyle = {
        display: 'grid',
        gridTemplateColumns: '32px 1fr auto',
        alignItems: 'center',
        gap: spacing[3],
        padding: `${spacing[3]} ${spacing[4]}`,
        borderBottom: `0.5px solid ${colors.borderSubtle}`,
    };

    const copyBtnStyle = {
        fontSize: '11px',
        padding: '2px 8px',
        borderRadius: layout.borderRadius.sm,
        border: `1px solid ${colors.border}`,
        background: hoverCopy ? colors.surfaceHover : 'transparent',
        color: copied ? colors.green : colors.textTertiary,
        cursor: 'pointer',
        fontFamily: 'inherit',
        flexShrink: 0,
        transition: `background ${animation.durationFast}, color ${animation.durationFast}`,
    };

    return html`
        <div style=${rowStyle}>
            <!-- Icon -->
            <span style=${{ fontSize: '20px', textAlign: 'center' }}>${fileIcon(file.mime_type)}</span>

            <!-- Info -->
            <div style=${{ minWidth: 0 }}>
                <!-- Filename -->
                <div style=${{ marginBottom: '2px' }}>
                    ${editing
                        ? html`<input
                            ref=${inputRef}
                            style=${{
                                ...styles.input,
                                width: '280px',
                                padding: '3px 8px',
                                fontSize: typography.size.sm,
                            }}
                            value=${editValue}
                            onInput=${(e) => setEditValue(e.target.value)}
                            onKeyDown=${handleKeyDown}
                            onBlur=${handleRename}
                        />`
                        : html`<span
                            style=${{
                                fontSize: typography.size.sm,
                                fontWeight: typography.weight.medium,
                                color: colors.text,
                                cursor: 'pointer',
                            }}
                            onClick=${() => { setEditValue(file.filename); setEditing(true); }}
                            title="Click to rename"
                        >${file.filename}</span>`
                    }
                </div>

                <!-- Meta: size + date -->
                <div style=${{
                    fontSize: typography.size.xs,
                    color: colors.textTertiary,
                    marginBottom: '4px',
                }}>
                    ${formatSize(file.size_bytes)} \u00B7 ${timeAgo(file.created_at)}
                </div>

                <!-- Path with copy -->
                <div style=${{ display: 'flex', alignItems: 'center', gap: spacing[2] }}>
                    <span style=${{
                        fontFamily: typography.fontMono,
                        fontSize: typography.size.xs,
                        color: colors.textSecondary,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        minWidth: 0,
                    }}>${file.stored_path}</span>
                    <button
                        style=${copyBtnStyle}
                        onMouseEnter=${() => setHoverCopy(true)}
                        onMouseLeave=${() => setHoverCopy(false)}
                        onClick=${handleCopy}
                    >${copied ? 'Copied!' : 'Copy'}</button>
                </div>
            </div>

            <!-- Delete -->
            <div style=${{ flexShrink: 0 }}>
                <${ConfirmAction}
                    label="Delete"
                    confirmLabel="Yes, delete"
                    warningText="Delete this file?"
                    danger=${true}
                    onConfirm=${() => onDelete(file.id)}
                />
            </div>
        </div>
    `;
}

// ── Files Page ───────────────────────────────────────────────────────────────

export function Files() {
    const [files, setFiles] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    const loadFiles = useCallback(async () => {
        try {
            const data = await api.getFiles();
            // Sort most recent first
            const sorted = (data.files || data || []).sort(
                (a, b) => new Date(b.created_at) - new Date(a.created_at)
            );
            setFiles(sorted);
            setError(null);
        } catch (e) {
            setError(e.message);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { loadFiles(); }, [loadFiles]);

    const handleUpload = useCallback(async (file) => {
        await api.uploadFile(file);
        await loadFiles();
    }, [loadFiles]);

    const handleRename = useCallback(async (id, filename) => {
        try {
            await api.renameFile(id, filename);
            await loadFiles();
        } catch (e) {
            setError(e.message);
        }
    }, [loadFiles]);

    const handleDelete = useCallback(async (id) => {
        try {
            await api.deleteFile(id);
            setFiles(prev => prev.filter(f => f.id !== id));
        } catch (e) {
            setError(e.message);
        }
    }, []);

    const pageStyle = {
        maxWidth: layout.contentMaxWidth,
        margin: '0 auto',
        padding: `${spacing[6]} ${layout.contentPadding}`,
    };

    return html`
        <div style=${pageStyle}>
            <${SectionHeader} text="FILES" />

            <${UploadZone} onUpload=${handleUpload} />

            ${error && html`
                <div style=${{
                    padding: spacing[3],
                    marginBottom: spacing[4],
                    fontSize: typography.size.xs,
                    color: colors.red,
                    background: colors.redBg,
                    borderRadius: layout.borderRadius.md,
                }}>${error}</div>
            `}

            ${loading
                ? html`<div style=${{ textAlign: 'center', padding: spacing[8], color: colors.textTertiary, fontSize: typography.size.sm }}>Loading...</div>`
                : files.length === 0
                    ? html`<div style=${{
                        textAlign: 'center',
                        padding: `${spacing[12]} ${spacing[4]}`,
                        color: colors.textTertiary,
                        fontSize: typography.size.sm,
                    }}>Upload files to reference them in task specs. Drag and drop or click to browse.</div>`
                    : html`
                        <div style=${{
                            border: `0.5px solid ${colors.border}`,
                            borderRadius: layout.borderRadius.lg,
                            overflow: 'hidden',
                        }}>
                            ${files.map(f => html`
                                <${FileRow}
                                    key=${f.id}
                                    file=${f}
                                    onRename=${handleRename}
                                    onDelete=${handleDelete}
                                />
                            `)}
                        </div>
                    `
            }
        </div>
    `;
}
