// Ouvrage Design System — Design Tokens
// Copper Forge palette: warm amber on dark brown. Import these constants into any view.

export const colors = {
    // Backgrounds — warm brown-black layers with clear separation
    bg:           '#0f0d0b',
    surface:      '#1e1a14',
    surfaceHover: '#252019',
    surfaceActive:'#2c261e',
    input:        '#2c2620',  // form input fields

    // Borders — warm brown
    border:       '#3e3326',
    borderSubtle: 'rgba(58, 48, 35, 0.5)',
    borderHover:  '#4e4030',

    // Text — warm whites and tans
    text:          '#ede8e3',
    textSecondary: '#b0a89e',
    textTertiary:  '#887e72',

    // Accent — amber
    accent:  '#d97706',

    // Status colors
    green:  '#3dd68c',
    yellow: '#f5a623',
    red:    '#f25c5c',
    blue:   '#4da3ff',

    // Status backgrounds (low opacity)
    greenBg:  'rgba(61, 214, 140, 0.12)',
    yellowBg: 'rgba(245, 166, 35, 0.12)',
    redBg:    'rgba(242, 92, 92, 0.12)',
    blueBg:   'rgba(77, 163, 255, 0.12)',
    accentBg: 'rgba(217, 119, 6, 0.15)',
};

export const typography = {
    fontBody: "'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    fontMono: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",

    // Size scale (px values as strings for use in CSS)
    size: {
        xs:   '12px',
        sm:   '13px',
        base: '14px',
        md:   '14px',
        lg:   '16px',
        xl:   '18px',
        '2xl':'22px',
        '3xl':'28px',
    },

    weight: {
        light:   300,
        normal:  400,
        medium:  500,
        semibold:600,
        bold:    700,
    },

    lineHeight: {
        tight:  1.3,
        normal: 1.5,
        relaxed:1.7,
    },
};

export const spacing = {
    1:  '4px',
    2:  '8px',
    3:  '12px',
    4:  '16px',
    5:  '20px',
    6:  '24px',
    8:  '32px',
    10: '40px',
    12: '48px',
    16: '64px',
};

export const layout = {
    headerHeight: '52px',
    contentMaxWidth: '900px',
    contentPadding: '24px',
    sidebarWidth: '220px',
    borderRadius: {
        sm: '4px',
        md: '6px',
        lg: '10px',
        pill: '999px',
    },
};

export const animation = {
    durationFast:   '120ms',
    durationNormal: '200ms',
    durationSlow:   '350ms',
    easing: 'cubic-bezier(0.16, 1, 0.3, 1)',
};

// Status → color mapping for convenience
export const statusColors = {
    working:              '#3b82f6',
    queued:               '#6b7280',
    ready:                '#6b7280',
    'needs-review':       '#d97706',
    completed:            '#10b981',
    merged:               '#10b981',
    stopped:              '#ef4444',
    failed:               '#ef4444',
    'rate-limited':       '#8b5cf6',
    cancelled:            '#6b7280',
    'turns-exhausted':    '#ef4444',
    error:                '#ef4444',
    conflict:             '#ef4444',
    reopened:             '#d97706',
    'pending-validation': '#6b7280',
};

export const statusBgs = {
    working:              'rgba(59, 130, 246, 0.12)',
    queued:               'rgba(107, 114, 128, 0.12)',
    ready:                'rgba(107, 114, 128, 0.10)',
    'needs-review':       'rgba(217, 119, 6, 0.15)',
    completed:            'rgba(16, 185, 129, 0.12)',
    merged:               'rgba(16, 185, 129, 0.12)',
    stopped:              'rgba(239, 68, 68, 0.12)',
    failed:               'rgba(239, 68, 68, 0.12)',
    'rate-limited':       'rgba(139, 92, 246, 0.12)',
    cancelled:            'rgba(107, 114, 128, 0.12)',
    'turns-exhausted':    'rgba(239, 68, 68, 0.12)',
    error:                'rgba(239, 68, 68, 0.12)',
    conflict:             'rgba(239, 68, 68, 0.12)',
    reopened:             'rgba(217, 119, 6, 0.15)',
    'pending-validation': 'rgba(107, 114, 128, 0.12)',
};
