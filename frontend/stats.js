//Visitor + usage dashboard. Fetches /api/stats on load + on theme
//change, renders four doughnut charts (country / browser / OS /
//device + a wide referrers doughnut), and five bar/line histograms
//(visits / conversions / card downloads / donations / server load)
//each with its own time-range tab cluster (24h / 7d / 30d / 1y).
//
//Same CSS variables drive the chart colours so a dark <-> light
//switch from the main page persists to this dashboard too.

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
    const opts = lineOptions();
    opts.scales.y.title = { display: true, text: 'Load (1 min)', color: readCssVar('--ink-soft', '#9ba0a6'), font: { size: 10 } };
    opts.scales.y1 = {
        position: 'right',
        ticks: { color: readCssVar('--ink-soft', '#9ba0a6'), font: { size: 10 } },
        grid:  { drawOnChartArea: false },
        beginAtZero: true, max: 100,
        title: { display: true, text: '% used', color: readCssVar('--ink-soft', '#9ba0a6'), font: { size: 10 } },
    };
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
                    tension: 0.25, pointRadius: 0, borderWidth: 2,
                },
                {
                    label: 'RAM used %',
                    data: (payload.mem_used_pct || []).map((p) => p.value),
                    borderColor: '#3b82f6', backgroundColor: 'transparent',
                    yAxisID: 'y1',
                    tension: 0.25, pointRadius: 0, borderWidth: 2,
                },
                {
                    label: 'Disk used %',
                    data: (payload.disk_used_pct || []).map((p) => p.value),
                    borderColor: '#22c55e', backgroundColor: 'transparent',
                    yAxisID: 'y1',
                    tension: 0.25, pointRadius: 0, borderWidth: 2, borderDash: [4, 4],
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
    visits:      { chart: null, range: '24h', src: {
        '24h': 'hourly_24h', '7d': 'hourly_7d', '30d': 'daily_30d', '1y': 'daily_1y',
    }, accent: () => readCssVar('--accent', '#f5a623') },
    conversions: { chart: null, range: '24h', src: {
        '24h': 'conversions_hourly_24h', '7d': 'conversions_hourly_7d',
        '30d': 'conversions_daily_30d',  '1y': 'conversions_daily_1y',
    }, accent: () => '#22c55e' },
    downloads:   { chart: null, range: '24h', src: {
        '24h': 'downloads_hourly_24h', '7d': 'downloads_hourly_7d',
        '30d': 'downloads_daily_30d',  '1y': 'downloads_daily_1y',
    }, accent: () => '#3b82f6' },
    donations:   { chart: null, range: '30d', src: {
        '30d': 'donations_daily_30d', '1y': 'donations_daily_1y',
    }, accent: () => '#ec4899' },
    server:      { chart: null, range: '24h', src: {
        '24h': 'server_hourly_24h', '7d': 'server_daily_7d',
        '30d': 'server_daily_30d',  '1y': 'server_daily_1y',
    }, accent: () => readCssVar('--accent', '#f5a623') },
};
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
    else
    {
        const series = Array.isArray(data) ? data : [];
        h.chart = renderBars(`chart-histogram-${name}`, series, h.range, h.accent());
    }
}

function updateDonationsNote()
{
    const el = document.getElementById('donations-note');
    if (!el || !snapshot) return;
    if (snapshot.donations_configured)
    {
        const total = snapshot.donations_total_amount || 0;
        try { el.textContent = `All-time total: ${total.toLocaleString()} (Buy Me a Coffee).`; }
        catch (_) { el.textContent = `All-time total: ${total} (Buy Me a Coffee).`; }
    }
    else
    {
        el.textContent = 'Set the BMAC_TOKEN env var on the VPS to enable donations history.';
    }
}

async function loadStats()
{
    try
    {
        const resp = await fetch('/api/stats', { credentials: 'omit' });
        if (!resp.ok) { console.warn('stats endpoint returned', resp.status); return; }
        snapshot = await resp.json();
    }
    catch (err) { console.warn('stats fetch failed', err); return; }

    document.querySelectorAll('[data-kpi]').forEach((el) =>
    {
        const key = el.getAttribute('data-kpi');
        el.textContent = KPI_FMT(snapshot[key]);
    });

    renderDoughnut('chart-countries', snapshot.countries);
    renderDoughnut('chart-browsers',  snapshot.browsers);
    renderDoughnut('chart-os',        snapshot.operating_systems);
    renderDoughnut('chart-devices',   snapshot.devices);
    if (Array.isArray(snapshot.referrers))
    {
        renderDoughnut('chart-referrers', snapshot.referrers);
    }

    //BMaC's API is opaque for now (the only public scope is
    //"read-only" and our token still returns Unauthenticated as
    //of May 2026), so hide the donations card until they ship a
    //usable token flow. We keep the section's markup + backend
    //wiring intact so a future re-enable is a one-line CSS flip.
    const donationsSection = document.querySelector('[data-hist="donations"]');
    if (donationsSection)
    {
        donationsSection.style.display = 'none';
    }

    Object.keys(histograms).forEach((name) =>
    {
        if (name === 'donations') return;
        applyHistogram(name);
    });
    updateDonationsNote();

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
    if (!histograms[name]) return;
    section.querySelectorAll('.hist-tab').forEach((tab) =>
    {
        tab.addEventListener('click', () =>
        {
            histograms[name].range = tab.dataset.range;
            applyHistogram(name);
        });
    });
});

new MutationObserver(() =>
{
    if (snapshot) loadStats();
}).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

loadStats();
