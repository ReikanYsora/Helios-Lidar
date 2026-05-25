//Visitor + usage dashboard. Fetches /api/stats on load + on theme
//change, renders four doughnut charts (country / browser / OS /
//device + a wide referrers doughnut), and five bar/line histograms
//(visits / conversions / card downloads / donations / server load)
//each with its own time-range tab cluster (24h / 7d / 30d / 1y).
//
//Same CSS variables drive the chart colours so a dark <-> light
//switch from the main page persists to this dashboard too.

//Visible error chip when the /api/stats fetch fails (most often
//on mobile when the saved Basic credentials don't auto-fill, so
//the API returns 401 and the dashboard would otherwise render
//empty without a clue).
function showFetchError(detail)
{
    let el = document.getElementById('stats-fetch-error');
    if (!el)
    {
        el = document.createElement('div');
        el.id = 'stats-fetch-error';
        el.style.cssText = 'margin:12px 0;padding:10px 14px;border-radius:6px;background:#ef4444;color:#fff;font-size:13px;text-align:center;';
        const host = document.querySelector('.stats-main') || document.body;
        host.insertBefore(el, host.firstChild);
    }
    el.textContent = `Could not load /api/stats (${detail}). Try reloading the page; on mobile you may need to re-enter the password.`;
}

const KPI_FMT = (n) =>
{
    if (!Number.isFinite(n)) return ',';
    try { return n.toLocaleString(); }
    catch (_) { return String(n); }
};

const THEME_STORAGE_KEY = 'helios-lidar-site-theme';

function getSavedTheme()
{
    try
    {
        const saved = localStorage.getItem(THEME_STORAGE_KEY);
        if (saved === 'light' || saved === 'dark') return saved;
    }
    catch (_) { /* private mode */ }
    return 'dark';
}
document.documentElement.setAttribute('data-theme', getSavedTheme());

function readCssVar(name, fallback)
{
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
}

const SLICE_COLORS = [
    '#f5a623', '#3b82f6', '#22c55e', '#ec4899', '#a855f7',
    '#14b8a6', '#f97316', '#eab308', '#ef4444', '#6366f1',
    '#84cc16', '#06b6d4',
];

function sliceColors(n)
{
    const out = [];
    for (let i = 0; i < n; i++) out.push(SLICE_COLORS[i % SLICE_COLORS.length]);
    return out;
}

function doughnutOptions()
{
    const ink = readCssVar('--ink', '#e6e6e6');
    return {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: { position: 'right', align: 'center',
                      labels: { color: ink, boxWidth: 12, padding: 8, font: { size: 12 } } },
            tooltip: {
                backgroundColor: readCssVar('--surface', '#191a1b'),
                titleColor: ink, bodyColor: ink,
                borderColor: readCssVar('--border', 'rgba(255,255,255,0.2)'), borderWidth: 1,
            },
        },
        cutout: '55%',
    };
}

function barOptions(range)
{
    const ink     = readCssVar('--ink', '#e6e6e6');
    const inkSoft = readCssVar('--ink-soft', '#9ba0a6');
    const grid    = readCssVar('--border-soft', 'rgba(255,255,255,0.08)');
    //On the 7-day view (168 hourly bars) we force every label to
    //render: midnight buckets carry an MM-DD date, the other 23
    //bars per day carry an empty string. Without autoSkip:false,
    //Chart.js's default tick selector hides them all.
    const autoSkip = range !== '7d';
    return {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: { display: false },
            tooltip: {
                backgroundColor: readCssVar('--surface', '#191a1b'),
                titleColor: ink, bodyColor: ink,
                borderColor: readCssVar('--border', 'rgba(255,255,255,0.2)'), borderWidth: 1,
                callbacks: { title: (items) => fullTooltipLabel(items[0].label) },
            },
        },
        scales: {
            x: {
                ticks: { color: inkSoft, maxRotation: 0, autoSkip, font: { size: 10 } },
                grid:  { color: 'transparent' },
            },
            y: {
                ticks: { color: inkSoft, font: { size: 10 }, precision: 0 },
                grid:  { color: grid }, beginAtZero: true,
            },
        },
    };
}

function lineOptions()
{
    const ink     = readCssVar('--ink', '#e6e6e6');
    const inkSoft = readCssVar('--ink-soft', '#9ba0a6');
    const grid    = readCssVar('--border-soft', 'rgba(255,255,255,0.08)');
    return {
        responsive: true,
        maintainAspectRatio: false,
        spanGaps: true,
        plugins: {
            legend: { position: 'top', align: 'end',
                      labels: { color: ink, boxWidth: 12, padding: 8, font: { size: 11 } } },
            tooltip: {
                backgroundColor: readCssVar('--surface', '#191a1b'),
                titleColor: ink, bodyColor: ink,
                borderColor: readCssVar('--border', 'rgba(255,255,255,0.2)'), borderWidth: 1,
                callbacks: { title: (items) => fullTooltipLabel(items[0].label) },
            },
        },
        scales: {
            x: { ticks: { color: inkSoft, maxRotation: 0, autoSkip: true, font: { size: 10 } }, grid: { color: 'transparent' } },
            y: { ticks: { color: inkSoft, font: { size: 10 } }, grid: { color: grid }, beginAtZero: true },
        },
    };
}

function renderDoughnut(canvasId, series)
{
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    const labels = series.map((s) => s.label);
    const data   = series.map((s) => s.count);
    return new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels,
            datasets: [{
                data, backgroundColor: sliceColors(labels.length),
                borderWidth: 1, borderColor: readCssVar('--bg', '#0b0d10'),
            }],
        },
        options: doughnutOptions(),
    });
}

function renderBars(canvasId, series, range, accent)
{
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    const plugins = [];
    if (range === '7d')
    {
        plugins.push({
            id: 'day-separators',
            afterDatasetsDraw(chart)
            {
                const xs = chart.scales.x;
                const ya = chart.scales.y;
                const c  = chart.ctx;
                c.save();
                c.strokeStyle = readCssVar('--border', 'rgba(255,255,255,0.2)');
                c.lineWidth = 1;
                c.setLineDash([2, 3]);
                series.forEach((b, i) =>
                {
                    if (b.label && b.label.endsWith && b.label.endsWith('T00') && i > 0)
                    {
                        const halfBar = (xs.getPixelForValue(1) - xs.getPixelForValue(0)) / 2;
                        const x = xs.getPixelForValue(i) - halfBar;
                        c.beginPath();
                        c.moveTo(x, ya.top);
                        c.lineTo(x, ya.bottom);
                        c.stroke();
                    }
                });
                c.restore();
            },
        });
    }
    return new Chart(ctx, {
        type: 'bar',
        data: {
            labels:   series.map((s) => formatXLabel(s.label, range)),
            datasets: [{
                data: series.map((s) => s.count),
                backgroundColor: accent,
                borderRadius: 3, borderSkipped: false, maxBarThickness: 24,
            }],
        },
        options: barOptions(range),
        plugins,
    });
}

//Multi-line chart for server load (load_1m on left axis,
//mem_used_pct + disk_used_pct on right axis). Series objects are
//{label, value} with null for empty buckets.
function renderServerLines(canvasId, payload, range)
{
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    const labels = (payload.load_1m || []).map((p) => formatXLabel(p.label, range));
    const rawLabels = (payload.load_1m || []).map((p) => p.label);
    const inkSoft = readCssVar('--ink-soft', '#9ba0a6');
    const opts = lineOptions();
    opts.scales.y.title = { display: true, text: 'Load (1 min)', color: inkSoft, font: { size: 10 } };
    opts.scales.y1 = {
        position: 'right',
        ticks: { color: inkSoft, font: { size: 10 } },
        grid:  { drawOnChartArea: false },
        beginAtZero: true, max: 100,
        title: { display: true, text: '% used', color: inkSoft, font: { size: 10 } },
    };
    /*  Third axis (also on the right, offset out so it doesn't
        overlap y1) for network throughput in Mbps. Lets us read
        "is the box maxing out its OVH bandwidth cap?" against the
        same time axis as RAM / disk / load.                       */
    opts.scales.y2 = {
        position: 'right',
        ticks: { color: inkSoft, font: { size: 10 } },
        grid:  { drawOnChartArea: false },
        beginAtZero: true,
        offset: true,
        title: { display: true, text: 'Net Mbps', color: inkSoft, font: { size: 10 } },
    };
    opts.plugins.tooltip.callbacks.title = (items) => fullTooltipLabel(rawLabels[items[0].dataIndex] || items[0].label);
    return new Chart(ctx, {
        type: 'line',
        data: {
            labels,
            datasets: [
                {
                    label: 'Load avg (1 min)',
                    data: (payload.load_1m || []).map((p) => p.value),
                    borderColor: '#f5a623', backgroundColor: 'transparent',
                    yAxisID: 'y',
                    tension: 0.25, pointRadius: 2.5, pointHoverRadius: 4, borderWidth: 2,
                },
                {
                    label: 'RAM used %',
                    data: (payload.mem_used_pct || []).map((p) => p.value),
                    borderColor: '#3b82f6', backgroundColor: 'transparent',
                    yAxisID: 'y1',
                    tension: 0.25, pointRadius: 2.5, pointHoverRadius: 4, borderWidth: 2,
                },
                {
                    label: 'Disk used %',
                    data: (payload.disk_used_pct || []).map((p) => p.value),
                    borderColor: '#22c55e', backgroundColor: 'transparent',
                    yAxisID: 'y1',
                    tension: 0.25, pointRadius: 2.5, pointHoverRadius: 4, borderWidth: 2, borderDash: [4, 4],
                },
                {
                    label: 'Net RX (Mbps)',
                    data: (payload.net_rx_mbps || []).map((p) => p.value),
                    borderColor: '#a855f7', backgroundColor: 'transparent',
                    yAxisID: 'y2',
                    tension: 0.25, pointRadius: 2, pointHoverRadius: 4, borderWidth: 2,
                },
                {
                    label: 'Net TX (Mbps)',
                    data: (payload.net_tx_mbps || []).map((p) => p.value),
                    borderColor: '#ec4899', backgroundColor: 'transparent',
                    yAxisID: 'y2',
                    tension: 0.25, pointRadius: 2, pointHoverRadius: 4, borderWidth: 2, borderDash: [4, 4],
                },
            ],
        },
        options: opts,
    });
}

function formatXLabel(key, range)
{
    if (!key) return '';
    if (range === '24h' && key.length === 13) return key.slice(11) + 'h';
    if (range === '7d'  && key.length === 13) return key.endsWith('T00') ? key.slice(5, 10) : '';
    return key.slice(5);  // MM-DD for daily ranges
}

function fullTooltipLabel(key)
{
    if (!key) return '';
    if (key.length === 13) return key.replace('T', ' ') + ':00 UTC';
    return key + ' UTC';
}

//Per-histogram state: chart instance + active range + which fields
//to read from the snapshot for each range.
const histograms = {
    visits:        { chart: null, range: '24h', src: {
        '24h': 'hourly_24h', '7d': 'hourly_7d', '30d': 'daily_30d', '1y': 'daily_1y',
    }, accent: () => readCssVar('--accent', '#f5a623') },
    conversions:   { chart: null, range: '24h', src: {
        '24h': 'conversions_hourly_24h', '7d': 'conversions_hourly_7d',
        '30d': 'conversions_daily_30d',  '1y': 'conversions_daily_1y',
    }, accent: () => '#22c55e' },
    'downloads-pv':{ chart: null, range: '24h', src: {
        '24h': 'downloads_pv_hourly_24h', '7d': 'downloads_pv_hourly_7d',
        '30d': 'downloads_pv_daily_30d',  '1y': 'downloads_pv_daily_1y',
    }, accent: () => '#3b82f6' },
    server:        { chart: null, range: '24h', src: {
        '24h': 'server_hourly_24h', '7d': 'server_daily_7d',
        '30d': 'server_daily_30d',  '1y': 'server_daily_1y',
    }, accent: () => readCssVar('--accent', '#f5a623') },
    growth:        { chart: null, range: '1y', src: {
        '1y': 'growth_index_1y',
    }, accent: () => readCssVar('--accent', '#f5a623') },
};

//Each table section: tab state + the snapshot keys per range.
//Mirrors the histograms registry but the renderer paints HTML
//<tr>s instead of a Chart.js canvas.
const tables = {
    countries: { range: '24h', src: {
        '24h': 'countries_table_24h', '7d': 'countries_table_7d',
        '30d': 'countries_table_30d', '1y': 'countries_table_1y',
    }, body: 'table-countries-body', kind: 'country' },
    referrers: { range: '24h', src: {
        '24h': 'referrers_table_24h', '7d': 'referrers_table_7d',
        '30d': 'referrers_table_30d', '1y': 'referrers_table_1y',
    }, body: 'table-referrers-body', kind: 'referrer' },
};
let downloadsByVersionChart = null;
let snapshot = null;

function applyHistogram(name)
{
    if (!snapshot) return;
    const h = histograms[name];
    if (!h) return;
    const root = document.querySelector(`[data-hist="${name}"]`);
    if (!root) return;
    root.querySelectorAll('.hist-tab').forEach((t) =>
    {
        const isActive = t.dataset.range === h.range;
        t.classList.toggle('is-active', isActive);
        t.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
    const key = h.src[h.range];
    const data = snapshot[key];

    if (h.chart) { h.chart.destroy(); h.chart = null; }
    if (name === 'server')
    {
        h.chart = renderServerLines(`chart-histogram-${name}`, data || {}, h.range);
    }
    else if (name === 'downloads-pv')
    {
        h.chart = renderStackedPerVersion(`chart-downloads-pv`, data || {labels: [], datasets: []}, h.range);
    }
    else if (name === 'growth')
    {
        h.chart = renderGrowthIndex(`chart-histogram-${name}`, data || {labels: [], raw: [], ema: [], trend: []});
        renderGrowthKpis(data || {});
    }
    else
    {
        const series = Array.isArray(data) ? data : [];
        h.chart = renderBars(`chart-histogram-${name}`, series, h.range, h.accent());
    }
}

//Three-layer line chart for the composite growth index.
//  - raw daily score   : transparent fill, no line, just a hint
//  - 7-day EMA         : bold accent line (the "real" trajectory)
//  - linear trend      : dashed neutral line (where we'd be if the
//                        last 365 days extrapolated as a straight
//                        line, ie the average score-points / day)
//Single 1y range so the eye reads the long-horizon shape rather
//than chasing a 24h spike.
function renderGrowthIndex(canvasId, payload)
{
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    const labels    = (payload.labels || []);
    const labelTxt  = labels.map((l) => l.slice(5));  //MM-DD on the X axis
    const accent    = readCssVar('--accent', '#f5a623');
    const inkSoft   = readCssVar('--ink-soft', '#9ba0a6');
    const opts = lineOptions();
    opts.plugins.tooltip.callbacks.title = (items) => labels[items[0].dataIndex] + ' UTC';
    opts.plugins.tooltip.callbacks.label = (item) =>
    {
        const v = item.parsed.y;
        return `${item.dataset.label}: ${v == null ? ',' : Math.round(v).toLocaleString()}`;
    };
    return new Chart(ctx, {
        type: 'line',
        data: {
            labels: labelTxt,
            datasets: [
                {
                    label: 'Raw daily score',
                    data:  payload.raw || [],
                    borderColor: 'transparent',
                    backgroundColor: accent + '22',
                    pointRadius: 0, fill: 'origin',
                },
                {
                    label: '7-day EMA',
                    data:  payload.ema || [],
                    borderColor: accent, backgroundColor: 'transparent',
                    borderWidth: 2.5, tension: 0.25, pointRadius: 0, fill: false,
                },
                {
                    label: 'Linear trend',
                    data:  payload.trend || [],
                    borderColor: inkSoft, backgroundColor: 'transparent',
                    borderWidth: 1.5, borderDash: [6, 4], pointRadius: 0, fill: false,
                },
            ],
        },
        options: opts,
    });
}

function renderGrowthKpis(payload)
{
    const fmtPct = (v) =>
    {
        if (v == null || !Number.isFinite(v)) return ',';
        return (v > 0 ? '+' : '') + v.toFixed(1) + '%';
    };
    const setKpi = (id, prefix, value, isPctOrSlope) =>
    {
        const el = document.getElementById(id);
        if (!el) return;
        const num = (typeof value === 'number') ? value : null;
        el.textContent = `${prefix} ${isPctOrSlope === 'pct' ? fmtPct(num) : (num == null ? ',' : (num > 0 ? '+' : '') + num.toFixed(1))}`;
        el.classList.toggle('is-up',   num != null && num > 0);
        el.classList.toggle('is-down', num != null && num < 0);
    };
    setKpi('growth-kpi-wow',   'WoW',     payload.growth_pct_wow, 'pct');
    setKpi('growth-kpi-mom',   'MoM',     payload.growth_pct_mom, 'pct');
    setKpi('growth-kpi-slope', 'Slope/d', payload.slope_per_day,  'num');
}

//ISO 3166-1 alpha-2 -> Unicode regional indicator pair, which
//browsers render as the country flag emoji (Apple Color Emoji,
//Segoe UI Emoji, Noto Color Emoji, etc.). Returns an empty
//string when the code is missing or not a 2-letter A-Z pair.
function flagEmoji(code)
{
    if (!code || code.length !== 2) return '';
    const a = code.toUpperCase().charCodeAt(0);
    const b = code.toUpperCase().charCodeAt(1);
    if (a < 65 || a > 90 || b < 65 || b > 90) return '';
    return String.fromCodePoint(0x1F1E6 + (a - 65), 0x1F1E6 + (b - 65));
}

function escapeHTML(s)
{
    return String(s).replace(/[&<>"']/g, (c) =>
        ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

//Repaint a table body from the current snapshot + active range.
//`name` is the key in the tables registry. Tab pills are updated
//to reflect the active range so the markup stays accessible.
function applyTable(name)
{
    if (!snapshot) return;
    const t = tables[name];
    if (!t) return;
    const root = document.querySelector(`[data-table="${name}"]`);
    if (!root) return;
    root.querySelectorAll('.hist-tab').forEach((tab) =>
    {
        const isActive = tab.dataset.range === t.range;
        tab.classList.toggle('is-active', isActive);
        tab.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
    const body = document.getElementById(t.body);
    if (!body) return;
    const rows = snapshot[t.src[t.range]] || [];
    if (!rows.length)
    {
        const cols = (t.kind === 'country') ? 3 : 2;
        body.innerHTML = `<tr><td colspan="${cols}" class="stats-table-empty">No data in this window yet.</td></tr>`;
        return;
    }
    body.innerHTML = rows.map((r) =>
    {
        const count = KPI_FMT(r.count);
        if (t.kind === 'country')
        {
            const flag = escapeHTML(flagEmoji(r.code));
            return `<tr><td class="flag">${flag}</td><td>${escapeHTML(r.name || 'Unknown')}</td><td class="stats-table-num">${count}</td></tr>`;
        }
        return `<tr><td>${escapeHTML(r.host || 'Direct')}</td><td class="stats-table-num">${count}</td></tr>`;
    }).join('');
}

//Stacked bar chart for per-version downloads over time. Each
//release tag is a separate stack segment, sized by the delta in
//that bucket. Same X-axis formatting as the single-series bars
//(midnight day separators on the 7-day view, etc.).
function renderStackedPerVersion(canvasId, payload, range)
{
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    const labels   = (payload.labels   || []);
    const datasets = (payload.datasets || []).map((ds, i) => ({
        label: ds.tag,
        data:  ds.data,
        backgroundColor: SLICE_COLORS[i % SLICE_COLORS.length],
        borderWidth: 0, borderSkipped: false, borderRadius: 2,
    }));
    const opts = barOptions(range);
    opts.scales.x.stacked = true;
    opts.scales.y.stacked = true;
    opts.plugins.legend = {
        display: datasets.length > 0,
        position: 'top', align: 'end',
        labels: { color: readCssVar('--ink', '#e6e6e6'), boxWidth: 12, padding: 8, font: { size: 11 } },
    };
    opts.plugins.tooltip.callbacks.title = (items) => fullTooltipLabel(labels[items[0].dataIndex] || items[0].label);
    //Day separators on the 7d view via the same plugin used by
    //single-series bars; reuse the visits-style plugin inline here.
    const plugins = [];
    if (range === '7d')
    {
        plugins.push({
            id: 'day-separators-pv',
            afterDatasetsDraw(chart)
            {
                const xs = chart.scales.x; const ya = chart.scales.y; const c = chart.ctx;
                c.save();
                c.strokeStyle = readCssVar('--border', 'rgba(255,255,255,0.2)');
                c.lineWidth = 1; c.setLineDash([2, 3]);
                labels.forEach((lbl, i) =>
                {
                    if (lbl && lbl.endsWith && lbl.endsWith('T00') && i > 0)
                    {
                        const halfBar = (xs.getPixelForValue(1) - xs.getPixelForValue(0)) / 2;
                        const x = xs.getPixelForValue(i) - halfBar;
                        c.beginPath(); c.moveTo(x, ya.top); c.lineTo(x, ya.bottom); c.stroke();
                    }
                });
                c.restore();
            },
        });
    }
    return new Chart(ctx, {
        type: 'bar',
        data: { labels: labels.map((l) => formatXLabel(l, range)), datasets },
        options: opts,
        plugins,
    });
}

//Per-version downloads bar chart (cumulative count per release
//tag). Sourced from /api/helios-downloads which we already proxy
//and surface inside /api/stats? No: it's a separate field on the
//main page only. We re-fetch the same endpoint here, lightly,
//since it has its own 5-min cache server-side.
async function renderDownloadsPerVersion()
{
    const ctx = document.getElementById('chart-downloads-per-version');
    if (!ctx) return;
    let data;
    try
    {
        const resp = await fetch('/api/helios-downloads', { credentials: 'same-origin' });
        if (!resp.ok) return;
        data = await resp.json();
    }
    catch (_) { return; }
    if (!data || !Array.isArray(data.by_version)) return;
    const versions = [...data.by_version].reverse();  //oldest -> newest reads as a timeline
    if (downloadsByVersionChart) { downloadsByVersionChart.destroy(); downloadsByVersionChart = null; }
    const accent = readCssVar('--accent', '#f5a623');
    const opts = barOptions('static');
    //Tooltip wants the full tag, not the truncated X-axis label.
    opts.plugins.tooltip.callbacks = { title: (items) => items[0].label };
    downloadsByVersionChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels:   versions.map((v) => v.tag),
            datasets: [{
                data: versions.map((v) => v.downloads),
                backgroundColor: accent,
                borderRadius: 3, borderSkipped: false, maxBarThickness: 48,
            }],
        },
        options: opts,
    });
}

async function loadStats()
{
    try
    {
        /*  credentials: 'same-origin' so the browser includes the
            HTTP Basic Authorization header it cached when the user
            unlocked /stats. Safari iOS strips the header entirely
            under credentials: 'omit', which made the page render
            but every dataset come back empty on mobile.            */
        const resp = await fetch('/api/stats', { credentials: 'same-origin' });
        if (!resp.ok) { showFetchError(resp.status); return; }
        snapshot = await resp.json();
    }
    catch (err) { showFetchError(err && err.message || err); return; }

    document.querySelectorAll('[data-kpi]').forEach((el) =>
    {
        const key = el.getAttribute('data-kpi');
        el.textContent = KPI_FMT(snapshot[key]);
    });

    renderDoughnut('chart-browsers',  snapshot.browsers);
    renderDoughnut('chart-os',        snapshot.operating_systems);
    renderDoughnut('chart-devices',   snapshot.devices);

    Object.keys(histograms).forEach(applyHistogram);
    Object.keys(tables).forEach(applyTable);
    renderDownloadsPerVersion();

    const refreshEl = document.getElementById('stats-fetched-at');
    if (refreshEl && snapshot.fetched_at_unix)
    {
        const d = new Date(snapshot.fetched_at_unix * 1000);
        try { refreshEl.textContent = d.toLocaleString(); }
        catch (_) { refreshEl.textContent = d.toISOString(); }
    }
}

document.querySelectorAll('.stats-histogram').forEach((section) =>
{
    const name = section.getAttribute('data-hist');
    if (histograms[name])
    {
        section.querySelectorAll('.hist-tab').forEach((tab) =>
        {
            tab.addEventListener('click', () =>
            {
                histograms[name].range = tab.dataset.range;
                applyHistogram(name);
            });
        });
        return;
    }
    const tableName = section.getAttribute('data-table');
    if (tables[tableName])
    {
        section.querySelectorAll('.hist-tab').forEach((tab) =>
        {
            tab.addEventListener('click', () =>
            {
                tables[tableName].range = tab.dataset.range;
                applyTable(tableName);
            });
        });
    }
});

new MutationObserver(() =>
{
    if (snapshot) loadStats();
}).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

loadStats();
