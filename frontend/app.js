//Helios-Lidar upload + polling UI.
//
//Upload goes through XHR (not fetch) because we need upload.progress
//events for the upload progress bar; fetch doesn't expose those.
//Server-side processing is reported via job.progress_pct in the
//polled status JSON, which we map to the processing bar fill. The
//countdown to the COG deletion is driven by job.cog_expires_at, a
//Unix timestamp the server commits when the job lands done.

//Register the HA custom-element shims (<ha-card>, <ha-icon>)
//BEFORE the Helios bundle gets a chance to construct anything via
//the embedded demo (mountHeliosDemo() below). No-op when somehow
//run inside a real Home Assistant.
import '/static/ha-shims.js';

import { mountViewer } from '/static/viewer.js';
import { mountHeliosDemo } from '/static/demo-mount.js';
import {
    SUPPORTED_LANGS,
    LANG_FLAGS,
    LANG_LABELS,
    TRANSLATIONS,
    applyTranslations,
    detectInitialLang,
    persistLang,
} from '/static/i18n.js';

const POLL_INTERVAL_MS = 1500;

const els = {
    form:             document.getElementById('upload-form'),
    modeTabs:         Array.from(document.querySelectorAll('.mode-tab')),
    rasterPanel:      document.getElementById('raster-panel'),
    lazPanel:         document.getElementById('laz-panel'),
    dsmInput:         document.getElementById('dsm-input'),
    dtmInput:         document.getElementById('dtm-input'),
    lazInput:         document.getElementById('laz-input'),
    dsmSelected:      document.getElementById('dsm-selected'),
    dtmSelected:      document.getElementById('dtm-selected'),
    lazSelected:      document.getElementById('laz-selected'),
    submitButton:     document.getElementById('submit-button'),

    statusSection:    document.getElementById('status-section'),
    uploadFill:       document.getElementById('upload-fill'),
    uploadPct:        document.getElementById('upload-pct'),
    processFill:      document.getElementById('process-fill'),
    processPct:       document.getElementById('process-pct'),
    statusMessage:    document.getElementById('status-message'),
    statusJobId:      document.getElementById('status-job-id'),

    resultSection:    document.getElementById('result-section'),
    downloadButton:   document.getElementById('download-button'),
    resultMeta:       document.getElementById('result-meta'),
    resultSnippet:    document.getElementById('result-snippet'),
    copyButton:       document.getElementById('copy-snippet'),
    countdown:        document.getElementById('countdown'),

    errorSection:     document.getElementById('error-section'),
    errorMessage:     document.getElementById('error-message'),
    errorRestart:     document.getElementById('error-restart'),
};

//i18n bootstrap. Apply the saved/detected language to every
//[data-i18n] element, then build the flag switcher in the brand
//strip and wire click handlers that swap language + persist choice.
let activeLang = detectInitialLang();
applyTranslations(activeLang);

//Fetch the LiDAR data sources list (rendered from LIDAR_SOURCES.md)
//and inject it into the help block. Source of truth is the markdown
//file in the repo so the community can contribute new countries via
//pull request without touching the page itself.
fetchAndInjectLidarSources();
async function fetchAndInjectLidarSources()
{
    const slot = document.getElementById('data-sources-content');
    if (!slot) return;
    try
    {
        const resp = await fetch('/api/lidar-sources', { headers: { 'Accept': 'application/json' } });
        const data = await resp.json();
        if (resp.ok && data.html)
        {
            slot.innerHTML = data.html;
            slot.removeAttribute('data-i18n');
        }
    }
    catch (err)
    {
        //Stay on the translated "Loading..." fallback rather than
        //surfacing a JS error to the user; if /api/lidar-sources is
        //down the rest of the upload page still works.
        console.warn('[helios-lidar] lidar-sources fetch failed:', err);
    }
}

const langSwitcher = document.getElementById('lang-switcher');
if (langSwitcher)
{
    SUPPORTED_LANGS.forEach((lang) =>
    {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'lang-flag';
        btn.dataset.lang = lang;
        btn.title = LANG_LABELS[lang];
        btn.setAttribute('aria-label', LANG_LABELS[lang]);
        btn.textContent = LANG_FLAGS[lang];
        if (lang === activeLang) btn.classList.add('active');
        btn.addEventListener('click', () => switchLang(lang));
        langSwitcher.appendChild(btn);
    });
}

function switchLang(lang)
{
    if (!SUPPORTED_LANGS.includes(lang)) return;
    activeLang = lang;
    persistLang(lang);
    applyTranslations(lang);
    document.querySelectorAll('.lang-flag').forEach((btn) =>
    {
        btn.classList.toggle('active', btn.dataset.lang === lang);
    });
    //Refresh the filename slots since their text was probably
    //"no file selected" in the previous locale; reapply now so the
    //translated placeholder shows up instead of the stale one.
    refreshFilenameSlots();
    //Forward the locale change to the embedded Helios card so its
    //own i18n tree picks up the new language for chip labels +
    //tooltip strings.
    if (heliosDemoHandle) heliosDemoHandle.setLanguage(lang);
}

//Mount the embedded Helios card inside the About section, right
//above the community counters. Replaces the static trailer +
//screenshot gallery the page used to ship. Skipped silently if
//the host slot isn't on the page (defensive : the same app.js
//might be reused on a different layout later).
let heliosDemoHandle = null;
const demoWrap = document.getElementById('demo-card-wrap');
if (demoWrap)
{
    heliosDemoHandle = mountHeliosDemo({ hostEl: demoWrap, initialLang: activeLang });
}

function tFilename(input)
{
    return input.files[0]?.name || TRANSLATIONS[activeLang]?.noFile || 'no file selected';
}

function refreshFilenameSlots()
{
    if (els.dsmSelected) els.dsmSelected.textContent = tFilename(els.dsmInput);
    if (els.dtmSelected) els.dtmSelected.textContent = tFilename(els.dtmInput);
    if (els.lazSelected) els.lazSelected.textContent = tFilename(els.lazInput);
}

//Wire the filename display next to each "Choose file" custom button.
//The native <input type="file"> stays in the DOM (hidden by CSS) so
//form submission and accessibility behave exactly like default, but
//the visible UI is the styled button + plain filename text.
function wireFileInput(input, display)
{
    if (!input || !display) return;
    input.addEventListener('change', () =>
    {
        display.textContent = tFilename(input);
    });
}
wireFileInput(els.dsmInput, els.dsmSelected);
wireFileInput(els.dtmInput, els.dtmSelected);
wireFileInput(els.lazInput, els.lazSelected);

let activeMode = 'raster';
let countdownTimer = null;

function show(section)
{
    section.classList.remove('hidden');
}
function hide(section)
{
    section.classList.add('hidden');
}

function setMode(mode)
{
    activeMode = mode;
    els.modeTabs.forEach((tab) =>
    {
        tab.classList.toggle('active', tab.dataset.mode === mode);
    });
    if (mode === 'raster')
    {
        show(els.rasterPanel);
        hide(els.lazPanel);
    }
    else
    {
        hide(els.rasterPanel);
        show(els.lazPanel);
    }
}

els.modeTabs.forEach((tab) =>
{
    tab.addEventListener('click', () =>
    {
        setMode(tab.dataset.mode);
    });
});

function resetUi()
{
    hide(els.statusSection);
    hide(els.resultSection);
    hide(els.errorSection);
    els.form.reset();
    els.submitButton.disabled = false;
    setUploadProgress(0);
    setProcessProgress(0, 'Queued...');
    refreshFilenameSlots();
    if (countdownTimer)
    {
        clearInterval(countdownTimer);
        countdownTimer = null;
    }
    setMode(activeMode);
}

els.errorRestart.addEventListener('click', resetUi);

els.copyButton.addEventListener('click', async () =>
{
    const text = els.resultSnippet.textContent;
    try
    {
        await navigator.clipboard.writeText(text);
        els.copyButton.textContent = 'Copied!';
        setTimeout(() =>
        {
            els.copyButton.textContent = 'Copy to clipboard';
        }, 1500);
    }
    catch (_err)
    {
        els.copyButton.textContent = 'Copy failed, select manually';
    }
});

els.form.addEventListener('submit', async (event) =>
{
    event.preventDefault();

    let body;
    if (activeMode === 'raster')
    {
        const dsm = els.dsmInput.files[0];
        const dtm = els.dtmInput.files[0];
        if (!dsm || !dtm)
        {
            showError('Pick both a DSM and a DTM GeoTIFF before processing.');
            return;
        }
        body = new FormData();
        body.append('dsm', dsm);
        body.append('dtm', dtm);
    }
    else
    {
        const laz = els.lazInput.files[0];
        if (!laz)
        {
            showError('Pick a LAS / LAZ point cloud file before processing.');
            return;
        }
        body = new FormData();
        body.append('laz', laz);
    }

    els.submitButton.disabled = true;
    hide(els.resultSection);
    hide(els.errorSection);
    show(els.statusSection);
    setUploadProgress(0);
    setProcessProgress(0, 'Uploading...');
    els.statusJobId.textContent = '';

    try
    {
        const job = await postJob(body);
        els.statusJobId.textContent = `Job ${job.job_id}`;
        await pollJob(job.job_id);
    }
    catch (err)
    {
        showError(err.message || String(err));
    }
});

function postJob(body)
{
    return new Promise((resolve, reject) =>
    {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/jobs', true);

        xhr.upload.onprogress = (e) =>
        {
            if (e.lengthComputable)
            {
                setUploadProgress((e.loaded / e.total) * 100);
            }
        };
        xhr.upload.onload = () =>
        {
            //Upload finished hitting the wire; server's still
            //writing it to disk + dispatching the background task.
            //Show 100 % on the upload bar; the processing bar takes
            //over from here.
            setUploadProgress(100);
            setProcessProgress(0, 'Queued, waiting for the worker...');
        };

        xhr.onload = () =>
        {
            if (xhr.status >= 200 && xhr.status < 300)
            {
                try { resolve(JSON.parse(xhr.responseText)); }
                catch (_e) { reject(new Error('Server returned a malformed JSON response.')); }
            }
            else
            {
                let detail;
                try { detail = JSON.parse(xhr.responseText).detail; } catch (_e) { detail = null; }
                reject(new Error(detail || `Upload failed (HTTP ${xhr.status})`));
            }
        };
        xhr.onerror = () => reject(new Error('Network error during upload.'));
        xhr.onabort = () => reject(new Error('Upload aborted.'));

        xhr.send(body);
    });
}

async function pollJob(jobId)
{
    while (true)
    {
        await sleep(POLL_INTERVAL_MS);

        const response = await fetch(`/jobs/${jobId}`);
        if (!response.ok)
        {
            throw new Error(`Status check failed (HTTP ${response.status})`);
        }
        const job = await response.json();

        setProcessProgress(job.progress_pct ?? 0, job.progress_message || prettyStatus(job.status));

        if (job.status === 'done')
        {
            showResult(job);
            return;
        }
        if (job.status === 'failed')
        {
            showError(job.error || 'Processing failed without an error message.');
            return;
        }
    }
}

function setUploadProgress(pct)
{
    const clamped = Math.max(0, Math.min(100, pct));
    els.uploadFill.style.width = `${clamped}%`;
    els.uploadPct.textContent = `${Math.round(clamped)} %`;
}

function setProcessProgress(pct, message)
{
    const clamped = Math.max(0, Math.min(100, pct));
    els.processFill.style.width = `${clamped}%`;
    els.processPct.textContent = `${Math.round(clamped)} %`;
    els.statusMessage.textContent = message;
}

function showResult(job)
{
    hide(els.statusSection);
    show(els.resultSection);

    const filename = job.download_filename || `helios-ndsm-${job.job_id}.tif`;
    els.downloadButton.textContent = `Download ${filename}`;
    els.resultSnippet.textContent = job.yaml_snippet || '';
    els.resultMeta.textContent = formatMeta(job);
    els.submitButton.disabled = false;

    //Auto-trigger the file save dialog so the user has a local copy
    //before the VPS-side TTL wipes the server file.
    triggerDownload(job.cog_url, filename);
    els.downloadButton.onclick = () => triggerDownload(job.cog_url, filename);

    //Live countdown to the COG deletion. cog_expires_at is a Unix
    //timestamp in seconds, frontend ticks once per second.
    startCountdown(job.cog_expires_at);

    //Spin up the 3D preview against the freshly hosted COG. The
    //mountViewer call is fire-and-forget: any decode / WebGL error
    //surfaces inside the viewer-loading div, not as a top-level
    //error since the rest of the result (download + snippet) stays
    //usable.
    mountViewer(job.cog_url).catch((err) =>
    {
        console.warn('[helios-lidar] viewer failed:', err);
    });
}

function triggerDownload(url, filename)
{
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
}

function startCountdown(expiresAt)
{
    if (countdownTimer)
    {
        clearInterval(countdownTimer);
    }
    if (!expiresAt)
    {
        els.countdown.textContent = '--:--';
        return;
    }
    const tick = () =>
    {
        const remaining = Math.max(0, expiresAt - (Date.now() / 1000));
        if (remaining <= 0)
        {
            els.countdown.textContent = 'expired';
            els.countdown.classList.add('countdown-expired');
            //Disable the download button so a stale click doesn't
            //save the 404 page nginx returns once the COG is gone.
            els.downloadButton.disabled = true;
            els.downloadButton.textContent = 'File expired on server';
            clearInterval(countdownTimer);
            countdownTimer = null;
            return;
        }
        const mins = Math.floor(remaining / 60);
        const secs = Math.floor(remaining % 60);
        els.countdown.textContent = `${mins}:${secs.toString().padStart(2, '0')}`;
        if (remaining <= 30)
        {
            els.countdown.classList.add('countdown-warning');
        }
    };
    els.downloadButton.disabled = false;
    els.countdown.classList.remove('countdown-expired', 'countdown-warning');
    tick();
    countdownTimer = setInterval(tick, 1000);
}

function showError(message)
{
    hide(els.statusSection);
    show(els.errorSection);
    els.errorMessage.textContent = message;
    els.submitButton.disabled = false;
}

function formatMeta(job)
{
    const parts = [];
    if (job.bounds_wgs84)
    {
        const [minLon, minLat, maxLon, maxLat] = job.bounds_wgs84;
        parts.push(`bbox lat ${minLat.toFixed(4)} to ${maxLat.toFixed(4)}, lon ${minLon.toFixed(4)} to ${maxLon.toFixed(4)}`);
    }
    if (job.pixel_size_meters)
    {
        parts.push(`${job.pixel_size_meters} m pitch`);
    }
    if (job.epsg)
    {
        parts.push(`source CRS EPSG:${job.epsg}`);
    }
    return parts.join(' / ');
}

function prettyStatus(status)
{
    const map = {
        'queued':     'Queued',
        'validating': 'Validating',
        'processing': 'Processing',
        'cogging':    'Cloud-Optimizing',
        'done':       'Done',
        'failed':     'Failed',
    };
    return map[status] || status;
}

function sleep(ms)
{
    return new Promise((r) => setTimeout(r, ms));
}


//Community-counter loader. Two public totals fed by the VPS:
//cumulative GitHub downloads of the latest Helios card release
//(with a hover-revealed per-version breakdown sourced from the
//GitHub Releases API proxy), and the all-time count of successful
//pipeline conversions. The HTML reserves each slot with a ","
//placeholder; we swap in the real value once the fetch returns.
//A failure is silent: the placeholder stays + we don't surface an
//error, the counters are a flavour element, not a critical UI
//piece.
async function loadCommunityStats()
{
    const fmt = (n) =>
    {
        if (!Number.isFinite(n)) return ',';
        try { return n.toLocaleString(); }
        catch (_) { return String(n); }
    };
    const setStat = (key, value) =>
    {
        const el = document.querySelector('[data-stat="' + key + '"]');
        if (el) el.textContent = fmt(value);
    };
    try
    {
        const [downloadsResp, conversionsResp] = await Promise.all([
            fetch('/api/helios-downloads',  { credentials: 'omit' }),
            fetch('/api/conversions-count', { credentials: 'omit' }),
        ]);
        if (downloadsResp.ok)
        {
            const data = await downloadsResp.json();
            setStat('downloads', data.latest_downloads);
            renderDownloadsTooltip(data);
        }
        if (conversionsResp.ok)
        {
            const data = await conversionsResp.json();
            setStat('conversions', data.count);
        }
    }
    catch (_) { /* silent: counters are flavour, not critical */ }
}

//Populate the per-version download tooltip from the API payload.
//Stays hidden when the API returned an empty `by_version` array,
//so the tooltip never appears as a blank box on first load when
//the cache is cold and GitHub is unreachable.
function renderDownloadsTooltip(data)
{
    const tip = document.getElementById('downloads-tooltip');
    if (!tip || !data || !Array.isArray(data.by_version) || data.by_version.length === 0) return;
    const fmt = (n) =>
    {
        if (!Number.isFinite(n)) return '-';
        try { return n.toLocaleString(); }
        catch (_) { return String(n); }
    };
    const titleKey = 'downloadsTooltipTitle';
    const title = (TRANSLATIONS[activeLang] && TRANSLATIONS[activeLang][titleKey])
        || 'Downloads per version';

    const rows = data.by_version.map((r) =>
    {
        const tag      = String(r.tag || '').replace(/[<>&]/g, '');
        const count    = fmt(r.downloads);
        return `<div class="about-community-tooltip-row"><span>${tag}</span><span>${count}</span></div>`;
    }).join('');

    tip.innerHTML = `<div class="about-community-tooltip-title">${title}</div>${rows}`;
    tip.hidden = false;
}

//Demo card theme toggle. Two-button pill below the embedded
//<helios-card>. The Dark button starts active (matches the demo's
//initial `card-theme: dark`); clicking either button flips both
//the visual active state on the buttons and the live card theme
//via the demo handle's setTheme().
function wireDemoThemeToggle()
{
    const toggle = document.getElementById('about-theme-toggle');
    if (!toggle || !heliosDemoHandle) return;
    const buttons = Array.from(toggle.querySelectorAll('.theme-btn'));
    buttons.forEach((btn) =>
    {
        btn.addEventListener('click', () =>
        {
            const theme = btn.dataset.theme;
            if (theme !== 'light' && theme !== 'dark') return;
            buttons.forEach((b) =>
            {
                const isActive = (b === btn);
                b.classList.toggle('is-active', isActive);
                b.setAttribute('aria-checked', isActive ? 'true' : 'false');
            });
            heliosDemoHandle.setTheme(theme);
        });
    });
}

//Fire as soon as the DOM is parsed. The fetch piggy-backs on the
//main script's module-load, no extra HTTP round-trip.
loadCommunityStats();
wireDemoThemeToggle();
