//Live demo of the Helios HA card embedded on helios-lidar.org.
//
//Loads the actual Helios bundle from jsdelivr (pinned to the version
//matching the site, so the demo can never disagree with what users
//see in HACS), then hands the custom element a synthetic `hass`
//object built around a fictional Paris home with one PV sensor + a
//small battery pair. No real Home Assistant is involved.
//
//The mock implements only what the card actually reads from `hass`:
//  * states[entity_id]   for live values
//  * config.{lat,lon,elevation,timezone}   for the home location
//  * language            mirrored from the page locale
//  * localize(k)         returns the key (HA-side strings aren't used
//                        anywhere user-visible by the card itself,
//                        the card has its own i18n tree)
//  * callWS({type})      a tiny dispatcher: 'history/history_during_period'
//                        returns a synthetic 2-day curve, the user_data
//                        endpoints are accepted as no-ops
//
//A 5-second tick refreshes the live PV / battery readings against a
//clear-sky curve so the card visibly breathes.

import {
    SUPPORTED_LANGS,
    LANG_FLAGS,
    LANG_LABELS,
    applyTranslations,
    detectInitialLang,
    persistLang,
} from '/static/i18n.js';

const HELIOS_BUNDLE_URL = 'https://cdn.jsdelivr.net/gh/ReikanYsora/Helios@v1.6.3/dist/helios.js';

//Fictional demo home: a residential address near Montpellier,
//well inside IGN HD France's LiDAR coverage so the LiDAR layer
//can render real cast shadows from the surrounding roofs and
//trees.
const DEMO_HOME = { latitude: 43.567121976352816, longitude: 3.9376832711342176, elevation: 30 };
const DEMO_PEAK_KWP = 6.4;
const DEMO_BATTERY_KWH = 10;

//Boot shared chrome (lang switcher) before the card so the page
//never looks unstyled while jsdelivr fetches.
let activeLang = detectInitialLang();
applyTranslations(activeLang);
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
        btn.addEventListener('click', () =>
        {
            if (!SUPPORTED_LANGS.includes(lang)) return;
            activeLang = lang;
            persistLang(lang);
            applyTranslations(lang);
            document.querySelectorAll('.lang-flag').forEach((b) =>
                b.classList.toggle('active', b.dataset.lang === lang));
            //Keep the embedded card in sync with the page locale.
            if (heliosCard) heliosCard.hass = { ...mockHass, language: lang };
        });
        langSwitcher.appendChild(btn);
    });
}

//Live entity state model. The card re-reads through `hass.states[id]`
//on every render tick so the values below get picked up automatically
//once we reassign `card.hass = { ...mockHass }` from the tick loop.
const state = {
    pvPower:        0,
    batterySoc:    55,
    batteryPower:   0,
};

function syntheticPvPower(date, peakKw)
{
    //Simplified clear-sky-ish curve: a half-period cosine across
    //daylight hours, with a slight peak shift to mid-afternoon and
    //a touch of noise so the chip ticks visibly between renders.
    const h = date.getHours() + date.getMinutes() / 60;
    const solarNoonHours = 13.4;
    const halfDayHours = 6.5;
    const x = (h - solarNoonHours) / halfDayHours;
    if (Math.abs(x) >= 1) return 0;
    const baseline = Math.cos(x * Math.PI / 2);
    const shape = Math.max(0, baseline) * baseline;
    const jitter = 1 + (Math.random() - 0.5) * 0.08;
    return Math.round(peakKw * 1000 * shape * jitter);
}

function syntheticBattery(date, currentSoc)
{
    //Trapezoid behaviour: charges through the morning, parked at
    //or near full during the sunny window, discharges in the
    //evening. Power sign mirrors the SoC trajectory so the chip
    //arrow direction reads naturally.
    const h = date.getHours() + date.getMinutes() / 60;
    let target;
    if (h < 6)        target = Math.max(10, currentSoc - 0.5);
    else if (h < 12)  target = Math.min(95, currentSoc + 1.2);
    else if (h < 17)  target = 95;
    else if (h < 22)  target = Math.max(40, currentSoc - 1.5);
    else              target = Math.max(25, currentSoc - 0.4);
    const soc = currentSoc + (target - currentSoc) * 0.08;
    const powerKw = (soc - currentSoc) * 2.5;
    return { soc, powerKw };
}

function refreshSyntheticState()
{
    const now = new Date();
    state.pvPower = syntheticPvPower(now, DEMO_PEAK_KWP);
    const { soc, powerKw } = syntheticBattery(now, state.batterySoc);
    state.batterySoc = soc;
    state.batteryPower = powerKw * 1000;
}
refreshSyntheticState();

function makeStateObject(entityId, value, unit, deviceClass)
{
    return {
        entity_id:    entityId,
        state:        String(Math.round(value * 100) / 100),
        attributes: {
            unit_of_measurement: unit,
            device_class:        deviceClass,
            friendly_name:       entityId,
        },
        last_changed: new Date().toISOString(),
        last_updated: new Date().toISOString(),
        context:      { id: 'demo', parent_id: null, user_id: null },
    };
}

function currentStates()
{
    return {
        'sensor.demo_pv_power':       makeStateObject('sensor.demo_pv_power',     state.pvPower,     'W',  'power'),
        'sensor.demo_battery_soc':    makeStateObject('sensor.demo_battery_soc',  state.batterySoc,  '%',  'battery'),
        'sensor.demo_battery_power':  makeStateObject('sensor.demo_battery_power',state.batteryPower,'W',  'power'),
    };
}

//Mock WebSocket dispatcher. The card calls callWS for two things:
//historical PV (~2 days back) and user-data read/write. We hand
//back synthetic history for the former and accept the latter as
//no-ops, both as resolved promises so the await sites just flow.
function mockCallWS(msg)
{
    if (msg && msg.type === 'history/history_during_period')
    {
        const ids = msg.entity_ids || [];
        const out = {};
        const startMs = new Date(msg.start_time).getTime();
        const endMs   = new Date(msg.end_time).getTime();
        const stepMs  = 5 * 60 * 1000;
        for (const id of ids)
        {
            const samples = [];
            for (let t = startMs; t < endMs; t += stepMs)
            {
                const d = new Date(t);
                let v = 0;
                if (id === 'sensor.demo_pv_power')      v = syntheticPvPower(d, DEMO_PEAK_KWP);
                else if (id === 'sensor.demo_battery_soc')   v = 55;
                else if (id === 'sensor.demo_battery_power') v = 0;
                samples.push({ s: String(v), lu: Math.floor(t / 1000) });
            }
            out[id] = samples;
        }
        return Promise.resolve(out);
    }
    //frontend/{get,set}_user_data: accept silently. The card uses
    //these to persist calibration windows across page loads; in
    //demo mode we don't care if those round-trip.
    if (msg && msg.type === 'frontend/get_user_data') return Promise.resolve({ value: null });
    if (msg && msg.type === 'frontend/set_user_data') return Promise.resolve(null);
    return Promise.resolve(null);
}

let heliosCard = null;

const mockHass = {
    states:    currentStates(),
    config:    {
        latitude:  DEMO_HOME.latitude,
        longitude: DEMO_HOME.longitude,
        elevation: DEMO_HOME.elevation,
        time_zone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Europe/Paris',
        unit_system: { length: 'km', mass: 'kg', temperature: '°C', volume: 'L' },
    },
    themes:    { darkMode: false, default_theme: 'default', themes: {} },
    language:  activeLang,
    locale:    { language: activeLang, number_format: 'language', time_format: '24', date_format: 'DMY', first_weekday: 'language' },
    localize:  (k) => k,
    formatEntityState: (stateObj) => stateObj?.state ?? '',
    formatEntityAttributeValue: (_stateObj, attr) => attr,
    callWS:    mockCallWS,
    callApi:   () => Promise.resolve(null),
    connection: {
        subscribeEvents:  () => () => {},
        subscribeMessage: () => () => {},
    },
    user: { name: 'Demo', is_admin: false, is_owner: false },
};

const status = document.getElementById('demo-status');
const wrap = document.getElementById('demo-card-wrap');

import(HELIOS_BUNDLE_URL).then(() =>
{
    heliosCard = document.createElement('helios-card');
    heliosCard.setConfig({
        type: 'custom:helios-card',
        //Auto-rotate off in the demo so the initial composition
        //stays stable while readers explore the chips. They can
        //still drag / pinch the map manually.
        'auto-rotate-enabled':  false,
        'pv-power-entity':      'sensor.demo_pv_power',
        'pv-peak-kwp':          DEMO_PEAK_KWP,
        'battery-soc-entity':   'sensor.demo_battery_soc',
        'battery-power-entity': 'sensor.demo_battery_power',
        'map-style':            'streets',
        'show-labels':          true,
        'building-radius':      250,
        'lidar-precision':      'medium',
        'building-opacity':     0.7,
        'shadow-opacity':       0.45,
        'card-theme':           'dark',
        'timeline-enabled':     true,
        'timeline-width-pct':   100,
    });
    heliosCard.hass = { ...mockHass };
    wrap.appendChild(heliosCard);
    if (status) status.remove();

    //Tick the synthetic state every 5 s and reassign the hass
    //property so the card re-renders with the new readings. The
    //reassignment (instead of mutation in place) matches how HA
    //hands the card a fresh hass object on every state change.
    setInterval(() =>
    {
        refreshSyntheticState();
        if (!document.hidden && heliosCard)
        {
            heliosCard.hass = { ...mockHass, states: currentStates() };
        }
    }, 5000);
}).catch((err) =>
{
    console.error('[helios-lidar] demo bundle load failed:', err);
    if (status)
    {
        status.textContent = 'Could not load the Helios card bundle from the CDN. Try a hard reload, or visit the repository directly: https://github.com/ReikanYsora/Helios';
        status.classList.add('demo-error');
    }
});
