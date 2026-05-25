//Visitor-stats dashboard. Fetches /api/stats once on load, renders
//four doughnut charts (country / browser / OS / device) and three
//bar histograms (visits / conversions / card downloads), each with
//its own 24 h / 7 d / 30 d range tab. Same theme variables drive
//both Chart.js datasets and the surrounding chrome, so a light
//<-> dark switch from the main page persists across the dashboard
//too.

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
    catch (_) { /* private mode etc */ }
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
            legend: {
                position: 'right',
                align: 'center',
                labels: { color: ink, boxWidth: 12, padding: 8, font: { size: 12 } },
            },
            tooltip: {
                backgroundColor: readCssVar('--surface', '#191a1b'),
                titleColor: ink,
                bodyColor: ink,
                borderColor: readCssVar('--border', 'rgba(255,255,255,0.2)'),
                borderWidth: 1,
            },
        },
        cutout: '55%',
    };
}

function barOptions(labelFmt)
{
    const ink     = readCssVar('--ink', '#e6e6e6');
    const inkSoft = readCssVar('--ink-soft', '#9ba0a6');
    const grid    = readCssVar('--border-soft', 'rgba(255,255,255,0.08)');
    return {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: { display: false },
            tooltip: {
                backgroundColor: readCssVar('--surface', '#191a1b'),
                titleColor: ink,
                bodyColor: ink,
                borderColor: readCssVar('--border', 'rgba(255,255,255,0.2)'),
                borderWidth: 1,
                callbacks: {
                    title: (items) => labelFmt(items[0].label),
                },
            },
        },
        scales: {
            x: {
                ticks: { color: inkSoft, maxRotation: 0, autoSkip: true, font: { size: 10 } },
                grid:  { color: 'transparent' },
            },
            y: {
                ticks: { color: inkSoft, font: { size: 10 }, precision: 0 },
                grid:  { color: grid },
                beginAtZero: true,
            },
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
                data,
                backgroundColor: sliceColors(labels.length),
                borderWidth: 1,
                borderColor: readCssVar('--bg', '#0b0d10'),
            }],
        },
        options: doughnutOptions(),
    });
}

//Render a bar chart for a [{label, count}] series. `range` drives
//the X-axis label format AND, on the 7-day view, the vertical day
//separator overlay so the 168 hourly bars don't read as one long
//flat strip. The separator is drawn via a custom Chart.js plugin
//(no extra deps) that paints a thin vertical line at every
//midnight boundary based on the bucket label parsing.
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
                const ctx2 = chart.ctx;
                ctx2.save();
                ctx2.strokeStyle = readCssVar('--border', 'rgba(255,255,255,0.2)');
                ctx2.lineWidth = 1;
                ctx2.setLineDash([2, 3]);
                series.forEach((b, i) =>
                {
                    if (b.label.endsWith('T00') && i > 0)
                    {
                        const x = xs.getPixelForValue(i) - (xs.getPixelForValue(1) - xs.getPixelForValue(0)) / 2;
                        ctx2.beginPath();
                        ctx2.moveTo(x, ya.top);
                        ctx2.lineTo(x, ya.bottom);
                        ctx2.stroke();
                    }
                });
                ctx2.restore();
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
                borderRadius: 3,
                borderSkipped: false,
                maxBarThickness: 24,
            }],
        },
        options: barOptions(fullTooltipLabel),
        plugins,
    });
}

function formatXLabel(key, range)
{
    //24h tab: 24 hourly bars, show just the hour
    if (range === '24h' && key.length === 13) return key.slice(11) + 'h';
    //7d tab: 168 hourly bars, show date only on midnight buckets,
    //the rest are empty strings so the X axis doesn't turn into a
    //wall of duplicates
    if (range === '7d' && key.length === 13)
    {
        return key.endsWith('T00') ? key.slice(5, 10) : '';
    }
    //30d tab: 30 daily bars, show MM-DD
    return key.slice(5);
}

function fullTooltipLabel(key)
{
    if (key.length === 13) return key.replace('T', ' ') + ':00 UTC';
    return key + ' UTC';
}

//Module state: keep references to chart instances so re-renders
//(theme flip, range tab change) can destroy + re-mount cleanly.
const histograms = {
    visits:      { chart: null, range: '24h', series24: 'hourly_24h',          series7: 'hourly_7d',          series30: 'daily_30d' },
    conversions: { chart: null, range: '24h', series24: 'conversions_hourly_24h', series7: 'conversions_hourly_7d', series30: 'conversions_daily_30d' },
    downloads:   { chart: null, range: '24h', series24: 'downloads_hourly_24h',   series7: 'downloads_hourly_7d',   series30: 'downloads_daily_30d' },
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

    let series;
    if      (h.range === '24h') series = snapshot[h.series24];
    else if (h.range === '7d')  series = snapshot[h.series7];
    else                        series = snapshot[h.series30];
    if (!Array.isArray(series)) series = [];

    if (h.chart) { h.chart.destroy(); h.chart = null; }
    const accent = readCssVar('--accent', '#f5a623');
    h.chart = renderBars(`chart-histogram-${name}`, series, h.range, accent);
}

async function loadStats()
{
    try
    {
        const resp = await fetch('/api/stats', { credentials: 'omit' });
        if (!resp.ok)
        {
            console.warn('stats endpoint returned', resp.status);
            return;
        }
        snapshot = await resp.json();
    }
    catch (err)
    {
        console.warn('stats fetch failed', err);
        return;
    }

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

    Object.keys(histograms).forEach(applyHistogram);

    const refreshEl = document.getElementById('stats-fetched-at');
    if (refreshEl && snapshot.fetched_at_unix)
    {
        const d = new Date(snapshot.fetched_at_unix * 1000);
        try { refreshEl.textContent = d.toLocaleString(); }
        catch (_) { refreshEl.textContent = d.toISOString(); }
    }
}

//Wire each histogram section's tab cluster.
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
