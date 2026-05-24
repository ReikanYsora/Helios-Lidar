//Embed the real Helios card in a host element with a synthetic
//`hass` object. Used by the landing page to replace the static
//screenshot gallery with the live card. No real Home Assistant
//is involved; the card pulls its own basemap (OpenFreeMap) and
//weather (Open-Meteo) over the public CDNs.
//
//Two responsibilities :
//
//  1. Build a mock `hass` object that implements the small subset
//     of HA's frontend API the card actually reads:
//       * states[entity_id]                          for live values
//       * config.{latitude, longitude, ...}          for the home
//       * language                                   page locale
//       * callWS({type: 'history/history_during_period'})
//       * callWS({type: 'frontend/get|set_user_data'})
//       * localize, formatEntityState, formatEntityAttributeValue
//       * connection.subscribeEvents / subscribeMessage
//     A 5-second tick refreshes the live PV / battery readings
//     against a clear-sky synthetic curve so the card breathes.
//
//  2. Dynamic-import the Helios bundle from jsdelivr (pinned to
//     the version banner on the site so the demo can never
//     disagree with what users see in HACS), then mount one
//     <helios-card> inside the supplied host element.
//
//Exports a single `mountHeliosDemo({ hostEl, initialLang })` helper
//that returns a handle `{ setLanguage, setTheme }` the host page
//uses to forward language + theme changes from its switchers to
//the embedded card.

const HELIOS_BUNDLE_URL = 'https://cdn.jsdelivr.net/gh/ReikanYsora/Helios@v1.6.4/dist/helios.js';

//Fictional demo home: a residential address near Montpellier,
//well inside IGN HD France's LiDAR coverage so the LiDAR layer
//can render real cast shadows from the surrounding roofs and
//trees on the demo.
const DEMO_HOME        = { latitude: 43.567121976352816, longitude: 3.9376832711342176, elevation: 30 };
const DEMO_PEAK_KWP    = 6.4;

const state = {
    pvPower:       0,
    batterySoc:   55,
    batteryPower:  0,
};

function syntheticPvPower(date, peakKw)
{
    //Simplified clear-sky-ish curve: a half-period cosine across
    //daylight hours, peak shifted slightly past solar noon, with
    //a touch of noise so the chip ticks between renders.
    const h             = date.getHours() + date.getMinutes() / 60;
    const solarNoon     = 13.4;
    const halfDay       = 6.5;
    const x             = (h - solarNoon) / halfDay;
    if (Math.abs(x) >= 1) return 0;
    const baseline = Math.cos(x * Math.PI / 2);
    const shape    = Math.max(0, baseline) * baseline;
    const jitter   = 1 + (Math.random() - 0.5) * 0.08;
    return Math.round(peakKw * 1000 * shape * jitter);
}

function syntheticBattery(date, currentSoc)
{
    //Trapezoid behaviour : charges through the morning, parks at
    //or near full during the sunny window, discharges in the
    //evening. Power sign mirrors the SoC trajectory so the chip
    //arrow direction reads naturally.
    const h = date.getHours() + date.getMinutes() / 60;
    let target;
    if      (h <  6) target = Math.max(10, currentSoc - 0.5);
    else if (h < 12) target = Math.min(95, currentSoc + 1.2);
    else if (h < 17) target = 95;
    else if (h < 22) target = Math.max(40, currentSoc - 1.5);
    else             target = Math.max(25, currentSoc - 0.4);
    const soc     = currentSoc + (target - currentSoc) * 0.08;
    const powerKw = (soc - currentSoc) * 2.5;
    return { soc, powerKw };
}

function refreshSyntheticState()
{
    const now = new Date();
    state.pvPower = syntheticPvPower(now, DEMO_PEAK_KWP);
    const { soc, powerKw } = syntheticBattery(now, state.batterySoc);
    state.batterySoc   = soc;
    state.batteryPower = powerKw * 1000;
}

function makeStateObject(entityId, value, unit, deviceClass)
{
    return {
        entity_id: entityId,
        state:     String(Math.round(value * 100) / 100),
        attributes: {
            unit_of_measurement: unit,
            device_class:        deviceClass,
            friendly_name:       entityId,
        },
        last_changed: new Date().toISOString(),
        last_updated: new Date().toISOString(),
        context: { id: 'demo', parent_id: null, user_id: null },
    };
}

function currentStates()
{
    return {
        'sensor.demo_pv_power':      makeStateObject('sensor.demo_pv_power',     state.pvPower,     'W', 'power'),
        'sensor.demo_battery_soc':   makeStateObject('sensor.demo_battery_soc',  state.batterySoc,  '%', 'battery'),
        'sensor.demo_battery_power': makeStateObject('sensor.demo_battery_power',state.batteryPower,'W', 'power'),
    };
}

//Mock WebSocket dispatcher. The card calls callWS for two things:
//historical PV (~2 days back) and user-data read / write. We hand
//back synthetic history for the former and accept the latter as
//no-ops, both as resolved promises so the await sites just flow.
function mockCallWS(msg)
{
    if (msg && msg.type === 'history/history_during_period')
    {
        const ids     = msg.entity_ids || [];
        const out     = {};
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
                if      (id === 'sensor.demo_pv_power')      v = syntheticPvPower(d, DEMO_PEAK_KWP);
                else if (id === 'sensor.demo_battery_soc')   v = 55;
                else if (id === 'sensor.demo_battery_power') v = 0;
                samples.push({ s: String(v), lu: Math.floor(t / 1000) });
            }
            out[id] = samples;
        }
        return Promise.resolve(out);
    }
    if (msg && msg.type === 'frontend/get_user_data') return Promise.resolve({ value: null });
    if (msg && msg.type === 'frontend/set_user_data') return Promise.resolve(null);
    return Promise.resolve(null);
}

function buildMockHass(initialLang)
{
    return {
        states: currentStates(),
        config: {
            latitude:    DEMO_HOME.latitude,
            longitude:   DEMO_HOME.longitude,
            elevation:   DEMO_HOME.elevation,
            time_zone:   Intl.DateTimeFormat().resolvedOptions().timeZone || 'Europe/Paris',
            unit_system: { length: 'km', mass: 'kg', temperature: '°C', volume: 'L' },
        },
        themes:   { darkMode: false, default_theme: 'default', themes: {} },
        language: initialLang,
        locale:   { language: initialLang, number_format: 'language', time_format: '24', date_format: 'DMY', first_weekday: 'language' },
        localize: (k) => k,
        formatEntityState:           (so) => so?.state ?? '',
        formatEntityAttributeValue:  (_so, attr) => attr,
        callWS:  mockCallWS,
        callApi: () => Promise.resolve(null),
        connection: {
            subscribeEvents:  () => () => {},
            subscribeMessage: () => () => {},
        },
        user: { name: 'Demo', is_admin: false, is_owner: false },
    };
}

//Mount one <helios-card> inside `hostEl`. Returns a small handle
//`{ setLanguage(lang) }` the caller can use to forward locale
//changes from its language switcher to the embedded card. The
//handle's setLanguage is a no-op until the bundle finishes
//loading; calls made earlier are coalesced into the initial mount.
export function mountHeliosDemo({ hostEl, initialLang })
{
    refreshSyntheticState();
    const mockHass = buildMockHass(initialLang || 'en');
    let card = null;
    let pendingLang  = mockHass.language;
    let pendingTheme = 'dark';
    const cfg = {
        type: 'custom:helios-card',
        'auto-rotate-enabled':     false,
        'pv-power-entity':         'sensor.demo_pv_power',
        'pv-peak-kwp':             DEMO_PEAK_KWP,
        'battery-soc-entity':      'sensor.demo_battery_soc',
        'battery-power-entity':    'sensor.demo_battery_power',
        'map-style':               'streets',
        'show-labels':             false,
        'building-radius':         250,
        'lidar-precision':         'medium',
        'building-opacity':        0.25,
        'building-cluster-radius': 10,
        'shadow-opacity':          0.45,
        'card-theme':              pendingTheme,
        'timeline-enabled':        true,
        'timeline-width-pct':      100,
    };

    const handle = {
        setLanguage(lang)
        {
            if (!lang) return;
            pendingLang = lang;
            mockHass.language = lang;
            mockHass.locale   = { ...mockHass.locale, language: lang };
            if (card) card.hass = { ...mockHass, states: currentStates() };
        },
        setTheme(theme)
        {
            if (theme !== 'light' && theme !== 'dark') return;
            if (theme === pendingTheme) return;
            pendingTheme = theme;
            cfg['card-theme'] = theme;
            //Re-applying the config triggers a Lit re-render and the
            //card swaps its CSS variable stack + basemap style. The
            //live `hass` reassignment refreshes the readouts in step
            //so chips don't flicker stale on theme flip.
            if (card)
            {
                card.setConfig({ ...cfg });
                card.hass = { ...mockHass, states: currentStates() };
            }
        },
    };

    import(HELIOS_BUNDLE_URL).then(() =>
    {
        card = document.createElement('helios-card');
        //Auto-rotate stays off so the initial composition is stable
        //while readers explore the chips; they can still drag /
        //pinch manually.
        card.setConfig({ ...cfg });
        card.hass = { ...mockHass, language: pendingLang };
        hostEl.appendChild(card);

        //Tick the synthetic state every 5 s and reassign hass so
        //the card re-renders with the new readings. Pause when the
        //page is hidden so a backgrounded tab doesn't waste CPU.
        setInterval(() =>
        {
            refreshSyntheticState();
            if (!document.hidden && card)
            {
                card.hass = { ...mockHass, states: currentStates() };
            }
        }, 5000);
    }).catch((err) =>
    {
        console.error('[helios-lidar] demo bundle load failed:', err);
        hostEl.classList.add('demo-error');
        hostEl.textContent = 'Could not load the Helios card bundle from the CDN. Try a hard reload, or visit the repository directly: https://github.com/ReikanYsora/Helios';
    });

    return handle;
}
