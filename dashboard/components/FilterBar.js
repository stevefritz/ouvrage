import { h } from 'https://esm.sh/preact@10.25.4';
import { useState, useEffect, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography, layout, animation } from '../tokens.js';

const html = htm.bind(h);

export const ALL_STATUSES = ['working', 'completed', 'failed', 'needs-review', 'ready', 'cancelled',
    'rate-limited', 'turns-exhausted'];

export function FilterBar({ statusFilter, onStatusFilter, searchQuery, onSearch }) {
    const [rawSearch, setRawSearch] = useState(searchQuery || '');
    const debounceRef = useRef(null);

    // Keep rawSearch in sync if parent clears searchQuery externally
    useEffect(() => {
        if (!searchQuery) setRawSearch('');
    }, [searchQuery]);

    const handleSearchChange = (e) => {
        const val = e.target.value;
        setRawSearch(val);
        if (debounceRef.current) clearTimeout(debounceRef.current);
        debounceRef.current = setTimeout(() => {
            onSearch(val.trim());
        }, 300);
    };

    const handleClear = () => {
        setRawSearch('');
        if (debounceRef.current) clearTimeout(debounceRef.current);
        onSearch('');
    };

    const selectStyle = {
        fontFamily: typography.fontBody,
        fontSize: typography.size.sm,
        color: colors.textSecondary,
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.sm,
        padding: '4px 8px',
        cursor: 'pointer',
        outline: 'none',
        appearance: 'none',
        WebkitAppearance: 'none',
        paddingRight: '24px',
    };

    const wrapStyle = {
        position: 'relative',
        display: 'inline-block',
    };

    const arrowStyle = {
        position: 'absolute',
        right: '7px',
        top: '50%',
        transform: 'translateY(-50%)',
        fontSize: '9px',
        color: colors.textTertiary,
        pointerEvents: 'none',
    };

    const searchInputStyle = {
        fontFamily: typography.fontBody,
        fontSize: typography.size.sm,
        color: colors.text,
        background: colors.surface,
        border: `1px solid ${colors.border}`,
        borderRadius: layout.borderRadius.sm,
        padding: '4px 28px 4px 28px',
        outline: 'none',
        width: '200px',
        transition: `border-color ${animation.durationFast}`,
    };

    const searchWrapStyle = {
        position: 'relative',
        display: 'flex',
        alignItems: 'center',
        flex: 1,
        minWidth: '160px',
    };

    const searchIconStyle = {
        position: 'absolute',
        left: '8px',
        fontSize: '11px',
        color: colors.textTertiary,
        pointerEvents: 'none',
        lineHeight: 1,
    };

    const clearBtnStyle = {
        position: 'absolute',
        right: '6px',
        background: 'none',
        border: 'none',
        cursor: 'pointer',
        color: colors.textTertiary,
        fontSize: '13px',
        lineHeight: 1,
        padding: '2px',
        display: rawSearch ? 'block' : 'none',
    };

    return html`
        <div style=${{ marginBottom: '12px' }}>
            <style>${`
                @media (max-width: 640px) {
                    .foreman-filterbar { flex-direction: column; align-items: stretch !important; }
                    .foreman-filterbar-search { max-width: none !important; width: 100% !important; }
                    .foreman-filterbar-search input { width: 100% !important; box-sizing: border-box; }
                    .foreman-filterbar-dropdowns { flex-wrap: wrap; }
                }
                .foreman-filterbar-search input:focus { border-color: ${colors.accent}; }
            `}</style>
            <div class="foreman-filterbar" style=${{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                <!-- Search input -->
                <div class="foreman-filterbar-search" style=${searchWrapStyle}>
                    <span style=${searchIconStyle}>⌕</span>
                    <input
                        type="text"
                        placeholder="Search tasks..."
                        value=${rawSearch}
                        onInput=${handleSearchChange}
                        style=${{ ...searchInputStyle, width: '100%' }}
                    />
                    <button style=${clearBtnStyle} onClick=${handleClear} title="Clear search">✕</button>
                </div>

                <!-- Dropdowns -->
                <div class="foreman-filterbar-dropdowns" style=${{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <div style=${wrapStyle}>
                        <select
                            style=${selectStyle}
                            value=${statusFilter}
                            onChange=${e => onStatusFilter(e.target.value)}
                        >
                            <option value="">All statuses</option>
                            ${ALL_STATUSES.map(s => html`<option key=${s} value=${s}>${s}</option>`)}
                        </select>
                        <span style=${arrowStyle}>▾</span>
                    </div>
                </div>
            </div>
        </div>
    `;
}
