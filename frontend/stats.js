//Visitor-stats dashboard. Fetches /api/stats once on load, renders
//four doughnut charts (country / browser / OS / device) and a bar
//histogram with three time-range tabs (24 h, 7 d, 30 d). Same
//theme variables drive both Chart.js datasets and the surrounding
//chrome, so a light <-> dark switch from the main page persists
//across the dashboard too.

const KPI_FMT = (n) =>
{
    if (!Number.isFinite(n)) return ',';
    try { return n.toLocaleString(); }
    catch (_) { return String(n); }
};

const THEME_STORAGE_KEY = 'helios-lidar-site-theme';

//Read the saved theme + apply it before any chart is mounted so
//the very first paint matches the user's preference.
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

//Palette pulled at runtime from the page's CSS variables so the
//charts stay in sync with the chosen theme. Re-read on every
//render so a theme flip cascades through both static UI + canvases.
function readCssVar(name, fallback)
{
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
}

//Doughnut slice palette: rotates through theme-friendly hues that
//read distinctly in both palettes without going circus.
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

//Default Chart.js options shared by every doughnut: legend
//bottom-aligned with the current theme's ink colour, no animation
//bounce on hover so the page reads as a calm dashboard.
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

//Render a doughnut chart from a [{label, count}] series.
function renderDoughnut(canvasId, series, opts = {})
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
        options: { ...doughnutOptions(), ...opts },
    });
}

//Render a bar chart from a [{label, count}] series with an
//optional tooltip label formatter so the X-axis can stay compact
//while the hover shows the full date / hour.
function renderBars(canvasId, series, accent, labelFmt = (l) => l)
{
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    return new Chart(ctx, {
        type: 'bar',
        data: {
            labels:   series.map((s) => formatXLabel(s.label)),
            datasets: [{
                data: series.map((s) => s.count),
                backgroundColor: accent,
                borderRadius: 3,
                borderSkipped: false,
                maxBarThickness: 24,
            }],
        },
        options: barOptions(labelFmt),
    });
}

//Compact X-axis label for an hourly or daily ISO bucket key.
//"2026-05-24T13" -> "13h"; "2026-05-24" -> "05-24".
function formatXLabel(key)
{
    if (key.length === 13) return key.slice(11) + 'h';
    return key.slice(5);
}

function fullTooltipLabel(key)
{
    if (key.length === 13) return key.replace('T', ' ') + ':00 UTC';
    return key + ' UTC';
}

//Module state: keep references to the rendered Chart instances so
//a tab switch / theme flip can destroy + re-mount cleanly.
let histogramChart = null;
let activeRange    = '24h';
let snapshot       = null;

function applyHistogram(range)
{
    if (!snapshot) return;
    activeRange = range;
    document.querySelectorAll('.hist-tab').forEach((t) =>
    {
        const isActive = t.dataset.range === range;
        t.classList.toggle('is-active', isActive);
        t.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });

    let series, label;
    if (range === '24h') { series = snapshot.hourly_24h; label = 'visits / hour (last 24 h)'; }
    else if (range === '7d')  { series = snapshot.hourly_7d;  label = 'visits / hour (last 7 days)'; }
    else                      { series = snapshot.daily_30d;  label = 'visits / day (last 30 days)'; }

    if (histogramChart) { histogramChart.destroy(); histogramChart = null; }
    const accent = readCssVar('--accent', '#f5a623');
    histogramChart = renderBars('chart-histogram', series, accent, fullTooltipLabel);
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

    //KPIs
    document.querySelectorAll('[data-kpi]').forEach((el) =>
    {
        const key = el.getAttribute('data-kpi');
        el.textContent = KPI_FMT(snapshot[key]);
    });

    //Doughnut charts
    renderDoughnut('chart-countries', snapshot.countries);
    renderDoughnut('chart-browsers',  snapshot.browsers);
    renderDoughnut('chart-os',        snapshot.operating_systems);
    renderDoughnut('chart-devices',   snapshot.devices);

    //Histogram (defaults to 24h)
    applyHistogram(activeRange);

    //Refresh stamp
    const refreshEl = document.getElementById('stats-fetched-at');
    if (refreshEl && snapshot.fetched_at_unix)
    {
        const d = new Date(snapshot.fetched_at_unix * 1000);
        try { refreshEl.textContent = d.toLocaleString(); }
        catch (_) { refreshEl.textContent = d.toISOString(); }
    }
}

//Wire the histogram tabs once on script load.
document.querySelectorAll('.hist-tab').forEach((tab) =>
{
    tab.addEventListener('click', () => applyHistogram(tab.dataset.range));
});

//Theme flip cascade: any change to the html data-theme attribute
//(from a future toggle on this page, or because the user changed
//it on the main page and came back) re-renders the charts so the
//slice borders / grid colour / ink colour follow.
new MutationObserver(() =>
{
    if (snapshot) loadStats();
}).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

loadStats();
