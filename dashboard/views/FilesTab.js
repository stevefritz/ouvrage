// FilesTab — Project-scoped file manager
// Shown in the Files tab of a project page (#/project/:id/files).
// Supports drag-and-drop upload, file list with download/view/delete, and empty state.

import { h } from 'https://esm.sh/preact@10.25.4';
import htm from 'https://esm.sh/htm@3.1.1';
import { useState, useEffect, useCallback, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { colors, typography, spacing, layout, animation } from '../tokens.js';
import { MarkdownLightbox, isMarkdownFile } from '../components/MarkdownLightbox.js';
import { ImageLightbox, isImageFile } from '../components/ImageLightbox.js';

const html = htm.bind(h);

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatSize(bytes) {
    if (!bytes && bytes !== 0) return '—';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function timeAgo(isoString) {
    if (!isoString) return '';
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
    if (!mime) return '📄';
    if (mime.startsWith('image/')) return '🖼️';
    if (mime === 'application/pdf') return '📑';
    if (mime.startsWith('text/')) return '📄';
    return '📎';
}

// ── Upload Zone ───────────────────────────────────────────────────────────────

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
                    <div style=${{ fontSize: '24px', marginBottom: spacing[2] }}>⬆️</div>
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

// ── File Row ──────────────────────────────────────────────────────────────────

function FileRow({ file, onDelete }) {
    const [deleteConfirm, setDeleteConfirm] = useState(false);
    const [lightbox, setLightbox] = useState(false);

    const isImage = isImageFile(file.filename);
    const isMd = isMarkdownFile(file.filename);
    const downloadUrl = `/dashboard/api/files/${file.id}/download`;

    const smallBtn = {
        fontSize: '11px',
        padding: '2px 8px',
        borderRadius: layout.borderRadius.sm,
        border: `1px solid ${colors.border}`,
        background: 'transparent',
        color: colors.textTertiary,
        cursor: 'pointer',
        fontFamily: 'inherit',
        flexShrink: 0,
        textDecoration: 'none',
    };

    const rowStyle = {
        display: 'flex',
        alignItems: 'flex-start',
        gap: spacing[3],
        padding: `${spacing[3]} ${spacing[4]}`,
        borderBottom: `0.5px solid ${colors.borderSubtle}`,
    };

    return html`
        <div style=${rowStyle}>
            <!-- Icon -->
            ${isImage
                ? html`<img
                    src=${downloadUrl}
                    alt=${file.filename}
                    style=${{
                        width: '36px', height: '36px',
                        objectFit: 'cover', flexShrink: 0,
                        borderRadius: layout.borderRadius.sm,
                        border: `1px solid ${colors.border}`,
                        cursor: 'pointer',
                    }}
                    onClick=${() => setLightbox(true)}
                    title="Click to preview"
                />`
                : html`<span style=${{ fontSize: '20px', textAlign: 'center', flexShrink: 0, width: '36px', paddingTop: '2px' }}>${fileIcon(file.mime_type)}</span>`
            }

            <!-- Info -->
            <div style=${{ minWidth: 0, flex: 1 }}>
                <div style=${{ marginBottom: '2px' }}>
                    <span
                        style=${{
                            fontSize: typography.size.sm,
                            fontWeight: typography.weight.medium,
                            color: (isImage || isMd) ? colors.accent : colors.text,
                            cursor: (isImage || isMd) ? 'pointer' : 'default',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                            display: 'block',
                        }}
                        onClick=${(isImage || isMd) ? () => setLightbox(true) : undefined}
                        title=${file.filename}
                    >${file.filename}</span>
                </div>
                <div style=${{
                    fontSize: typography.size.xs,
                    color: colors.textTertiary,
                }}>
                    ${formatSize(file.size_bytes)} · ${timeAgo(file.created_at)}
                    ${file.task_id ? html` · promoted from task` : null}
                </div>
            </div>

            <!-- Actions -->
            <div style=${{
                display: 'flex',
                justifyContent: 'flex-end',
                alignItems: 'center',
                gap: spacing[2],
                flexShrink: 0,
            }}>
                ${isMd ? html`
                    <button
                        style=${{ ...smallBtn, color: colors.accent, borderColor: colors.accent }}
                        onClick=${() => setLightbox(true)}
                    >View</button>
                ` : null}
                <a
                    href=${downloadUrl}
                    download=${file.filename}
                    style=${smallBtn}
                >Download</a>
                ${deleteConfirm ? html`
                    <span style=${{ display: 'flex', gap: spacing[1], alignItems: 'center', flexShrink: 0 }}>
                        <span style=${{ fontSize: '11px', color: colors.textTertiary }}>Delete?</span>
                        <button style=${{ ...smallBtn, color: colors.red, borderColor: colors.red }}
                            onClick=${() => onDelete(file.id)}>Yes</button>
                        <button style=${smallBtn}
                            onClick=${() => setDeleteConfirm(false)}>Cancel</button>
                    </span>
                ` : html`
                    <button
                        style=${{ ...smallBtn, fontSize: '12px', padding: '1px 6px' }}
                        onClick=${() => setDeleteConfirm(true)}
                    >✕</button>
                `}
            </div>
        </div>

        ${lightbox && isImage && html`
            <${ImageLightbox}
                src=${downloadUrl}
                alt=${file.filename}
                onClose=${() => setLightbox(false)}
            />
        `}
        ${lightbox && isMd && html`
            <${MarkdownLightbox}
                src=${downloadUrl}
                filename=${file.filename}
                onClose=${() => setLightbox(false)}
            />
        `}
    `;
}

// ── FilesTab ──────────────────────────────────────────────────────────────────

export function FilesTab({ projectId }) {
    const [files, setFiles] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    const loadFiles = useCallback(async () => {
        try {
            const data = await api.getProjectFiles(projectId);
            const list = (data.files || data || []).sort(
                (a, b) => new Date(b.created_at) - new Date(a.created_at)
            );
            setFiles(list);
            setError(null);
        } catch (e) {
            setError(e.message);
        } finally {
            setLoading(false);
        }
    }, [projectId]);

    useEffect(() => { loadFiles(); }, [loadFiles]);

    const handleUpload = useCallback(async (file) => {
        await api.uploadProjectFile(projectId, file);
        await loadFiles();
    }, [projectId, loadFiles]);

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
                ? html`<div style=${{
                    textAlign: 'center',
                    padding: spacing[8],
                    color: colors.textTertiary,
                    fontSize: typography.size.sm,
                }}>Loading...</div>`
                : files.length === 0
                    ? html`<div style=${{
                        textAlign: 'center',
                        padding: `${spacing[12]} ${spacing[4]}`,
                        color: colors.textTertiary,
                        fontSize: typography.size.sm,
                    }}>No project files yet. Upload files or promote task artifacts.</div>`
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
                                    onDelete=${handleDelete}
                                />
                            `)}
                        </div>
                    `
            }
        </div>
    `;
}
