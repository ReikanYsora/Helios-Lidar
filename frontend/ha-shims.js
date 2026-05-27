//Minimal Home Assistant custom-element shims so the Helios card
//can render on the demo page (and on any non-HA host) the same
//way it does inside a real Home Assistant dashboard.
//
//Two custom elements get defined :
//
//  * <ha-card>  , block-level frame around the card. HA's real
//                 implementation does much more (theming, ripple,
//                 hover lift, etc.) but the only behaviour the
//                 Helios card actually depends on is that the
//                 element renders as a block with width / height
//                 honoring its parent. The shim's :host CSS gives
//                 it that, plus a reasonable rounded plate so the
//                 card has visual boundaries even without HA's
//                 theme cascading in. Helios then overrides the
//                 background to black via its own shadow-DOM rule
//                 (helios-card-css.ts targets `ha-card { ... }`).
//
//  * <ha-icon>  , MDI glyph by name. HA's real implementation
//                 looks up icons in an in-app iconset registry;
//                 we delegate to the Iconify CDN-fetched
//                 `<iconify-icon>` web component instead, which
//                 lazy-loads each requested icon from
//                 api.iconify.design (cached forever once first
//                 fetched). Color comes from `color: inherit`,
//                 size from `--mdc-icon-size` (same custom prop
//                 the real HA implementation reads), so any
//                 inline `style="color: ..."` or `--mdc-icon-size`
//                 the card sets keeps working.
//
//The Iconify web component itself is loaded from the same CDN
//via a side-effect import below. Total network cost: one ~ 15 KB
//gzipped script + one ~ 1 KB JSON per distinct icon used (the
//Helios card uses ~ 15 icons; ~ 15 KB extra).
//
//No-op when run inside Home Assistant: `customElements.define`
//throws if the name is already registered, which means HA's own
//ha-card / ha-icon take precedence and this shim quietly does
//nothing.

//Side-effect import. The Iconify icon web component registers
//<iconify-icon> with the same global customElements registry.
import 'https://cdn.jsdelivr.net/npm/iconify-icon@2/dist/iconify-icon.min.js';


function defineSafely(name, ctor)
{
    if (customElements.get(name)) return;
    try { customElements.define(name, ctor); }
    catch (_) { /* swallow: already defined by host environment */ }
}


class HeliosShimHaCard extends HTMLElement
{
    constructor()
    {
        super();
        const root = this.attachShadow({ mode: 'open' });
        const style = document.createElement('style');
        style.textContent = `
            :host {
                display: block;
                position: relative;
                background: var(--ha-card-background, var(--card-background-color, #1f1f1f));
                color:      var(--primary-text-color, #e6e6e6);
                border-radius: var(--ha-card-border-radius, 12px);
                box-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0, 0, 0, 0.35));
                overflow: hidden;
                box-sizing: border-box;
            }
        `;
        root.append(style, document.createElement('slot'));
    }
}
defineSafely('ha-card', HeliosShimHaCard);


class HeliosShimHaIcon extends HTMLElement
{
    static get observedAttributes() { return ['icon']; }

    constructor()
    {
        super();
        const root = this.attachShadow({ mode: 'open' });
        const style = document.createElement('style');
        style.textContent = `
            :host {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                width:      var(--mdc-icon-size, 24px);
                height:     var(--mdc-icon-size, 24px);
                /*  iconify-icon@2 sizes its inner SVG off the
                    computed font-size, not CSS width/height. Force
                    font-size to match --mdc-icon-size so the SVG
                    fills the host box instead of falling back to
                    the inherited body font-size (typically 14 px
                    on Helios-Lidar, which shrinks the icon to
                    ~60 % of the host's reserved square).            */
                font-size:  var(--mdc-icon-size, 24px);
                line-height: 1;
                color: inherit;
                vertical-align: middle;
                flex-shrink: 0;
            }
            iconify-icon {
                width:  1em;
                height: 1em;
                color: inherit;
                display: block;
            }
        `;
        this._inner = document.createElement('iconify-icon');
        this._inner.setAttribute('icon', this.getAttribute('icon') || '');
        root.append(style, this._inner);
    }

    attributeChangedCallback(name, _oldVal, newVal)
    {
        if (name === 'icon' && this._inner)
        {
            this._inner.setAttribute('icon', newVal || '');
        }
    }
}
defineSafely('ha-icon', HeliosShimHaIcon);
