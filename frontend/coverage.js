//Coverage page bootstrap: wires the Leaflet world map to the embedded
//Helios demo card, draws provider bboxes coloured by Helios release,
//and forwards user clicks to the card so any point on Earth jumps the
//3D view to that location.
//
//Self-contained: doesn't import app.js (which is index.html-specific
//and would crash on missing DOM nodes here). The pieces of app.js we
//need, language switcher + persisted theme, are reimplemented inline,
//small enough that the duplication beats the risk of refactoring the
//homepage bootstrap to fit two pages.
//
//Provider list is hand-mirrored from src/engine/lidar/providers/*.ts in
//the Helios card repo. Update both when a new provider lands; the
//`firstStableVersion` field drives the legend colour.

//ha-shims is a side-effect-only import: it installs the global
//Home-Assistant compatibility shims (customElements registration
//stubs, frontend helper aliases, etc.) the Helios card expects to
//find on `window` at module load. Without it the embedded card boots
//with broken sun arc / LiDAR button / timeline / dashboard because
//its internal helpers fall through to no-op fallbacks.
import '/static/ha-shims.js';
import {
    SUPPORTED_LANGS,
    LANG_FLAGS,
    LANG_LABELS,
    applyTranslations,
    detectInitialLang,
    persistLang,
} from './i18n.js';
import { mountHeliosDemo } from './demo-mount.js';

const PROVIDERS = [
    { id: 'fr',           name: 'France , IGN LiDAR HD',                              minLat: 41.0,  maxLat: 51.5,  minLon: -5.5,    maxLon: 9.8,    firstStableVersion: '1.5.x' },
    { id: 'uk-en',        name: 'UK England , Defra LiDAR Composite',                 minLat: 49.7,  maxLat: 56.0,  minLon: -7.2,    maxLon: 2.1,    firstStableVersion: '1.5.x' },
    { id: 'es',           name: 'Spain , PNOA LiDAR',                                 minLat: 35.8,  maxLat: 44.0,  minLon: -9.6,    maxLon: 4.4,    firstStableVersion: '1.5.x' },
    { id: 'nl',           name: 'Netherlands , PDOK AHN4',                            minLat: 50.7,  maxLat: 53.8,  minLon: 3.1,     maxLon: 7.3,    firstStableVersion: '1.5.x' },
    { id: 'no',           name: 'Norway , Kartverket NHM',                            minLat: 57.5,  maxLat: 81.0,  minLon: 4.0,     maxLon: 33.0,   firstStableVersion: '1.5.x' },
    { id: 'de-nrw',       name: 'Germany , North Rhine-Westphalia nDOM',              minLat: 50.30, maxLat: 52.55, minLon: 5.85,    maxLon: 9.50,   firstStableVersion: '1.6.x' },
    { id: 'pl',           name: 'Poland , GUGiK NMPT',                                minLat: 49.00, maxLat: 54.85, minLon: 14.10,   maxLon: 24.20,  firstStableVersion: '1.6.x' },
    { id: 'ca',           name: 'Canada , HRDEM',                                     minLat: 41.5,  maxLat: 84.0,  minLon: -141.5,  maxLon: -52.0,  firstStableVersion: '1.6.x' },
    { id: 'us-vt',        name: 'United States , Vermont VCGI nDSM',                  minLat: 42.65, maxLat: 45.10, minLon: -73.50,  maxLon: -71.40, firstStableVersion: '1.6.x' },
    { id: 'de-bb-be',     name: 'Germany , Brandenburg + Berlin DOM',                 minLat: 51.36, maxLat: 53.56, minLon: 11.27,   maxLon: 14.77,  firstStableVersion: '1.6.x' },
    { id: 'at-tirol',     name: 'Austria , Tirol ALS',                                minLat: 46.65, maxLat: 47.75, minLon: 10.05,   maxLon: 12.95,  firstStableVersion: '1.7.x' },
    { id: 'at-stmk',      name: 'Austria , Steiermark ALSHöheninformation',           minLat: 46.55, maxLat: 47.85, minLon: 13.50,   maxLon: 16.20,  firstStableVersion: '1.7.x' },
    { id: 'de-bw',        name: 'Germany , Baden-Württemberg LGL DOM5 + DGM1',         minLat: 47.50, maxLat: 49.85, minLon: 7.45,    maxLon: 10.55,  firstStableVersion: '1.7.x' },
    { id: 'be-fl',        name: 'Belgium , Flanders DHMVII',                          minLat: 50.65, maxLat: 51.55, minLon: 2.50,    maxLon: 5.95,   firstStableVersion: '1.7.x' },
];

//Initial demo home: Montpellier France, same point the index demo
//uses. Lands inside the FR bbox so the card boots in coverage and
//the first impression of the page is "shadows on".
const DEFAULT_HOME = { lat: 43.567121976352816, lon: 3.9376832711342176 };
const THEME_STORAGE_KEY = 'helios-lidar-site-theme';


//----------------------------------------------------------------- helpers

function readCssVar(name)
{
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || '#888';
}

function formatLatLon(lat, lon)
{
    const ns = lat >= 0 ? 'N' : 'S';
    const ew = lon >= 0 ? 'E' : 'W';
    return `${Math.abs(lat).toFixed(4)}° ${ns}, ${Math.abs(lon).toFixed(4)}° ${ew}`;
}

function providerCovering(lat, lon)
{
    for (const p of PROVIDERS)
    {
        if (lat >= p.minLat && lat <= p.maxLat
         && lon >= p.minLon && lon <= p.maxLon) return p;
    }
    return null;
}

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


//----------------------------------------------------------------- lang switcher

function mountLangSwitcher(initialLang, onChange)
{
    const host = document.getElementById('lang-switcher');
    if (!host) return;
    const select = document.createElement('select');
    select.className = 'lang-select';
    select.setAttribute('aria-label', 'Language');
    SUPPORTED_LANGS.forEach((lang) =>
    {
        const opt = document.createElement('option');
        opt.value       = lang;
        opt.textContent = `${LANG_FLAGS[lang]}  ${LANG_LABELS[lang]}`;
        if (lang === initialLang) opt.selected = true;
        select.appendChild(opt);
    });
    select.addEventListener('change', () =>
    {
        const lang = select.value;
        if (!SUPPORTED_LANGS.includes(lang)) return;
        persistLang(lang);
        applyTranslations(lang);
        onChange(lang);
    });
    host.appendChild(select);
}


//----------------------------------------------------------------- map

function waitForLeaflet()
{
    if (typeof window.L !== 'undefined') return Promise.resolve(window.L);
    return new Promise((resolve) =>
    {
        const check = () =>
        {
            if (typeof window.L !== 'undefined') return resolve(window.L);
            window.setTimeout(check, 50);
        };
        check();
    });
}


//----------------------------------------------------------------- bootstrap

async function bootstrap()
{
    //Apply persisted theme + initial locale before anything else so
    //the page paints in the right palette + language from the first
    //frame; embedded card picks up both via its mount options.
    const theme = getSavedTheme();
    document.documentElement.setAttribute('data-theme', theme);

    const initialLang = detectInitialLang();
    applyTranslations(initialLang);

    const L = await waitForLeaflet();

    const map = L.map('coverage-map', {
        worldCopyJump:      true,
        zoomControl:        true,
        minZoom:            1,
        //Native OSM tiles top out at z19; Leaflet upscales the z19
        //tile when the user zooms further, which keeps individual
        //houses big enough on screen to click precisely (a single
        //building at z21 is ~120 px wide on a Retina display).
        maxZoom:            21,
        attributionControl: false,
    }).setView([45, 5], 3);

    //OpenStreetMap standard raster tiles. Shows building outlines from
    //~z16 upwards which is exactly what we want for a "click your
    //house" selector. Theme-independent (the OSM palette stays
    //identical light and dark), so we only need one layer.
    L.tileLayer(
        'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
        {
            maxNativeZoom: 19,
            maxZoom:       21,
            crossOrigin:   true,
        }
    ).addTo(map);
    //No-op when the site theme changes; the OSM basemap is monotonic
    //across themes so no swap is needed. Kept as a named function so
    //the MutationObserver below can still call something without a
    //branch.
    function applyTileTheme(_t) { /* OSM basemap is theme-neutral */ }

    //Geolocation control, top-right pill button. Clicking it triggers
    //the browser permission prompt, then pans + zooms the map to the
    //user's GPS coords and re-anchors the embedded demo card. Wrapped
    //in a try/catch on the navigator call because Safari throws when
    //the page is not served over HTTPS, the user is offline, or the
    //OS denied location services system-wide.
    const LocateControl = L.Control.extend({
        options: { position: 'topright' },
        onAdd: function ()
        {
            const btn = L.DomUtil.create('button', 'coverage-locate-btn leaflet-bar');
            btn.type = 'button';
            btn.title = 'Center on my location';
            btn.setAttribute('aria-label', 'Center on my location');
            //Crosshair / target glyph, same visual family as Helios's
            //own home-marker in the card chrome.
            btn.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="3" fill="currentColor"/><circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="1.6"/><line x1="12" y1="1" x2="12" y2="5" stroke="currentColor" stroke-width="1.6"/><line x1="12" y1="19" x2="12" y2="23" stroke="currentColor" stroke-width="1.6"/><line x1="1" y1="12" x2="5" y2="12" stroke="currentColor" stroke-width="1.6"/><line x1="19" y1="12" x2="23" y2="12" stroke="currentColor" stroke-width="1.6"/></svg>';
            L.DomEvent.disableClickPropagation(btn);
            L.DomEvent.on(btn, 'click', () =>
            {
                if (!navigator.geolocation)
                {
                    btn.classList.add('is-error');
                    return;
                }
                btn.classList.add('is-loading');
                try
                {
                    navigator.geolocation.getCurrentPosition(
                        (pos) =>
                        {
                            btn.classList.remove('is-loading', 'is-error');
                            const lat = pos.coords.latitude;
                            const lon = pos.coords.longitude;
                            //Zoom 18 lands on a city block, plenty of
                            //room to identify the user's roof.
                            map.setView([lat, lon], 18);
                            jumpTo(lat, lon);
                        },
                        (_err) =>
                        {
                            btn.classList.remove('is-loading');
                            btn.classList.add('is-error');
                        },
                        { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
                    );
                }
                catch (_)
                {
                    btn.classList.remove('is-loading');
                    btn.classList.add('is-error');
                }
            });
            return btn;
        },
    });
    map.addControl(new LocateControl());

    //Draw one rectangle per provider. Colours come from CSS variables
    //so a theme flip recolours them via the MutationObserver below.
    function versionColors()
    {
        return {
            '1.5.x': readCssVar('--cov-v15'),
            '1.6.x': readCssVar('--cov-v16'),
            '1.7.x': readCssVar('--cov-v17'),
        };
    }
    const rectangles = [];
    const initialColors = versionColors();
    for (const p of PROVIDERS)
    {
        const color = initialColors[p.firstStableVersion] || '#888';
        const rect  = L.rectangle(
            [[p.minLat, p.minLon], [p.maxLat, p.maxLon]],
            {
                color,
                weight:      1.5,
                opacity:     0.95,
                fillColor:   color,
                fillOpacity: 0.18,
                interactive: true,
            }
        );
        rect.bindTooltip(
            `<strong>${p.name}</strong><br/>` +
            `<span class="coverage-tt-version">Helios ${p.firstStableVersion}</span>`,
            { sticky: true, direction: 'top', className: 'coverage-tt' }
        );
        rect.addTo(map);
        rectangles.push(rect);
    }

    //Mount the embedded Helios card via the same demo-mount used on
    //the homepage. The card boots at DEFAULT_HOME and moves on every
    //click registered on the world map.
    const cardHost   = document.getElementById('coverage-card-host');
    const locReadout = document.getElementById('coverage-current-loc');
    const demoHandle = mountHeliosDemo({
        hostEl:       cardHost,
        initialLang,
        initialTheme: theme,
        initialHome:  DEFAULT_HOME,
    });

    function updateReadout(lat, lon)
    {
        const matched = providerCovering(lat, lon);
        const coordText = formatLatLon(lat, lon);
        const status = matched
            ? `✔ ${matched.name} (Helios ${matched.firstStableVersion})`
            : `✖ No native provider yet, the card falls back to OpenFreeMap building footprints (no trees)`;
        locReadout.textContent = `${coordText} , ${status}`;
    }

    function jumpTo(lat, lon)
    {
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
        if (lat < -85 || lat > 85) return;
        const wrappedLon = ((lon + 180) % 360 + 360) % 360 - 180;
        demoHandle.setLocation?.({ lat, lon: wrappedLon });
        updateReadout(lat, wrappedLon);
    }

    //All map clicks (including ones on rectangle overlays) bubble up
    //to the map; Leaflet rectangles don't stopPropagation by default
    //so a single handler covers both cases.
    map.on('click', (e) => jumpTo(e.latlng.lat, e.latlng.lng));

    updateReadout(DEFAULT_HOME.lat, DEFAULT_HOME.lon);

    //Language switcher wired AFTER the demo handle exists so its
    //onChange can forward locale changes to the card too.
    mountLangSwitcher(initialLang, (lang) =>
    {
        demoHandle.setLanguage?.(lang);
    });

    //Theme + language changes on <html> get mirrored to the card.
    //Triggers: another tab flipping the persisted theme, or the
    //(future) on-page theme toggle if we add one.
    new MutationObserver((muts) =>
    {
        for (const m of muts)
        {
            if (m.attributeName === 'data-theme')
            {
                const t = document.documentElement.getAttribute('data-theme');
                if (t === 'light' || t === 'dark')
                {
                    demoHandle.setTheme?.(t);
                    applyTileTheme(t);
                    const nv = versionColors();
                    rectangles.forEach((r, i) =>
                    {
                        const col = nv[PROVIDERS[i].firstStableVersion];
                        r.setStyle({ color: col, fillColor: col });
                    });
                }
            }
            if (m.attributeName === 'lang')
            {
                const l = document.documentElement.getAttribute('lang');
                if (l) demoHandle.setLanguage?.(l);
            }
        }
    }).observe(document.documentElement, { attributes: true });
}

bootstrap().catch((err) => console.error('[coverage] bootstrap failed:', err));
