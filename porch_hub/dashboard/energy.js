// The energy panel the twin pages share: <porch-energy> shows what every plug in the home draws
// now, its own energy counter, and how its power moved since the hub started (GET /home/energy).
//
// It shows readings, never estimates. A plug that reports no power (one reached through Google
// Home, which says only on or off) is listed with its switch, and the panel says why.
//
// layout="tiles" lays the meters out side by side (under a floor plan); layout="list" stacks
// them (in a narrow side panel). Its styles live in a shadow root and are sized in em, so it
// follows the page it sits in. Clicking a meter sends a "porch:select-device" event on document
// with {id}, so the page can open that device. Names from the hub go in as text, never markup.
(() => {
  const SVG = "http://www.w3.org/2000/svg";
  const ROUTES = { matter: "Matter", lg_thinq: "LG ThinQ", google_home: "Google Home", ring: "Ring" };

  const watts = (w) => (w >= 1000 ? `${(w / 1000).toFixed(2)} kW` : w >= 100 ? `${Math.round(w)} W` : `${w.toFixed(1)} W`);
  const kwh = (k) => `${k.toFixed(k >= 10 ? 1 : k >= 1 ? 2 : 3)} kWh`;
  const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

  const CSS = `
    :host { display: block; font: inherit; color: inherit; }
    * { box-sizing: border-box; }
    .head { display: flex; flex-wrap: wrap; align-items: baseline; gap: .3em .8em; }
    .title { font-size: .78em; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; color: #94a3b8; }
    .total { margin-left: auto; font-weight: 700; font-variant-numeric: tabular-nums; color: #fbbf24; }
    .total.none { color: #94a3b8; font-weight: 600; font-size: .9em; }
    .count { color: #64748b; font-size: .82em; }
    .meters { display: grid; gap: .55em; margin-top: .6em; }
    :host([layout="tiles"]) .meters { grid-template-columns: repeat(auto-fill, minmax(min(100%, 11.5em), 1fr)); }
    .meter { display: block; width: 100%; text-align: left; cursor: pointer; font: inherit; color: inherit;
             padding: .55em .7em; border: 1px solid #1e293b; border-radius: .6em; background: rgba(15, 23, 42, .6); }
    .meter:hover { border-color: #334155; background: rgba(30, 41, 59, .6); }
    .meter:focus-visible { outline: 2px solid #5eead4; outline-offset: 2px; }
    .meter.offline { opacity: .55; }
    .row { display: flex; align-items: baseline; gap: .5em; }
    .name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .room { color: #64748b; font-size: .8em; white-space: nowrap; }
    .value { margin-left: auto; font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; }
    .value.drawing { color: #fbbf24; }
    .value.idle { color: #94a3b8; }
    .value.on { color: #38bdf8; }
    .sub { margin-top: .15em; color: #94a3b8; font-size: .8em; font-variant-numeric: tabular-nums; }
    .sub.quiet { color: #64748b; }
    .sub.stale { color: #fbbf24; }
    svg { display: block; width: 100%; height: 1.9em; margin-top: .35em; }
    .line { fill: none; stroke: #fbbf24; stroke-width: 1.5; vector-effect: non-scaling-stroke; stroke-linejoin: round; }
    .area { fill: rgba(251, 191, 36, .14); stroke: none; }
    .span { display: flex; justify-content: space-between; color: #64748b; font-size: .72em; }
    .note { margin: .7em 0 0; color: #94a3b8; font-size: .82em; line-height: 1.45; }
    .note.problem { color: #fb7185; }
    .empty { margin: .6em 0 0; color: #94a3b8; font-size: .9em; }
  `;

  class PorchEnergy extends HTMLElement {
    connectedCallback() {
      if (this.started) return;  // a page may move this element; it keeps running as it is
      this.started = true;
      const root = this.attachShadow({ mode: "open" });
      const style = document.createElement("style");
      style.textContent = CSS;
      this.body = document.createElement("section");
      this.body.setAttribute("aria-label", "Energy");
      root.append(style, this.body);
      this.render();
      this.load();
      // The clock moves the lines along even when nothing changes; events bring changes at once.
      this.timer = setInterval(() => this.load(), 30000);
      this.follow();
    }

    make(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    }

    async load() {
      try {
        const response = await fetch("/home/energy", { cache: "no-store" });
        if (!response.ok) throw new Error(String(response.status));
        this.energy = await response.json();
        this.problem = null;
      } catch (error) {
        // A hub started before it could serve this answers 404: say so instead of showing nothing.
        this.problem = error.message === "404" ? "This hub cannot serve energy readings yet. Restart it."
                                               : "The hub did not answer.";
      }
      this.render();
      document.dispatchEvent(new CustomEvent("porch:energy", { detail: this.energy || null }));
    }

    // Reload soon after a device changes, once for a burst of changes.
    soon() {
      clearTimeout(this.pending);
      this.pending = setTimeout(() => this.load(), 350);
    }

    follow() {
      let retry = 1000;
      const open = () => {
        const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/events`);
        socket.addEventListener("open", () => { retry = 1000; });
        socket.addEventListener("message", (message) => {
          let event;
          try { event = JSON.parse(message.data); } catch { return; }
          if (["state_changed", "online_changed", "mode_changed", "resync_required"].includes(event.type)) this.soon();
        });
        socket.addEventListener("close", () => {
          setTimeout(open, retry);
          retry = Math.min(retry * 2, 15000);
        });
      };
      open();
    }

    // How a meter's power moved: steps between its readings, up to now.
    chart(meter, now) {
      const points = meter.history.map(([at, value]) => [new Date(at).getTime(), value]);
      if (!points.length) return null;
      const end = Math.max(new Date(now).getTime(), points[points.length - 1][0] + 1);
      const start = points[0][0];
      const top = Math.max(...points.map(([, value]) => value), 1);
      const x = (t) => ((t - start) / Math.max(end - start, 1)) * 100;
      const y = (value) => 22 - (value / top) * 20;
      let path = `M0 ${y(points[0][1]).toFixed(2)}`;
      points.forEach(([t, value], i) => {
        if (i) path += ` H${x(t).toFixed(2)} V${y(value).toFixed(2)}`;
      });
      path += " H100";
      const svg = document.createElementNS(SVG, "svg");
      svg.setAttribute("viewBox", "0 0 100 24");
      svg.setAttribute("preserveAspectRatio", "none");
      svg.setAttribute("aria-hidden", "true");
      const area = document.createElementNS(SVG, "path");
      area.setAttribute("class", "area");
      area.setAttribute("d", `${path} V24 H0 Z`);
      const line = document.createElementNS(SVG, "path");
      line.setAttribute("class", "line");
      line.setAttribute("d", path);
      svg.append(area, line);
      const span = this.make("div", "span");
      // Under the line: when it starts, and what the plug used since then (or its peak, if nothing yet).
      const used = meter.since_kwh !== null && meter.since_kwh > 0;
      span.append(this.make("span", "", `since ${clock(meter.history[0][0])}`),
                  this.make("span", "", used ? `+${kwh(meter.since_kwh)} used` : `peak ${watts(top)}`));
      const wrap = this.make("div");
      wrap.append(svg, span);
      return wrap;
    }

    meter(meter, now) {
      const button = this.make("button", `meter${meter.online ? "" : " offline"}`);
      button.type = "button";
      // The name gets the whole first line beside the reading; the room goes on the line under it.
      const row = this.make("div", "row");
      row.append(this.make("span", "name", meter.name));
      const reports = meter.power_w !== null;
      const state = !meter.online ? "offline" : meter.on === true ? "on" : meter.on === false ? "off" : "";
      let value, tone, sub, quiet = false;
      if (reports) {
        value = watts(meter.power_w);
        // A reading that has not moved for a long time while drawing power is the last one heard,
        // not what the plug draws now: it is shown greyed, and the line under it says since when.
        tone = meter.stale ? "idle" : meter.power_w > 0 ? "drawing" : "idle";
        // The plug's own running counter. What was used while the hub watched is under the line.
        sub = [meter.room, state, meter.energy_kwh !== null ? `counter ${kwh(meter.energy_kwh)}` : ""]
          .filter(Boolean).join(" · ");
      } else {
        value = state ? state.toUpperCase() : "—";
        tone = meter.on === true && meter.online ? "on" : "idle";
        sub = [meter.room, meter.energy_kwh !== null ? `counter ${kwh(meter.energy_kwh)}`
          : `no power reading through ${ROUTES[meter.path] || meter.path}`].filter(Boolean).join(" · ");
        quiet = meter.energy_kwh === null;
      }
      row.append(this.make("span", `value ${tone}`, value));
      button.append(row, this.make("div", `sub${quiet ? " quiet" : ""}`, sub));
      // A plug that measures them also says the voltage it sees and the current it passes.
      const detail = [typeof meter.voltage_v === "number" ? `${meter.voltage_v.toFixed(1)} V` : "",
                      typeof meter.current_a === "number" ? `${meter.current_a.toFixed(2)} A` : ""].filter(Boolean).join(" · ");
      if (detail) button.append(this.make("div", "sub quiet", detail));
      if (meter.stale) button.append(this.make("div", "sub stale", `No new reading since ${clock(meter.changed_at)}`));
      const chart = reports ? this.chart(meter, now) : null;
      if (chart) button.append(chart);
      button.setAttribute("aria-label", `${meter.name}, ${meter.room || "no room"}: ${value}. ${sub}${detail ? `. ${detail}` : ""}`);
      button.addEventListener("click", () => {
        document.dispatchEvent(new CustomEvent("porch:select-device", { detail: { id: meter.id } }));
      });
      return button;
    }

    render() {
      const head = this.make("div", "head");
      head.append(this.make("span", "title", "Energy"));
      const parts = [head];
      const energy = this.energy;
      if (energy) {
        const meters = energy.meters;
        if (energy.total_w !== null) {
          head.append(this.make("span", "count", `${energy.reporting} of ${meters.length} report power`),
                      this.make("span", "total", `${watts(energy.total_w)} now`));
        } else if (meters.length) {
          head.append(this.make("span", "total none", "No power readings"));
        }
        if (meters.length) {
          const grid = this.make("div", "meters");
          grid.append(...meters.map((meter) => this.meter(meter, energy.at)));
          parts.push(grid);
          const silent = meters.filter((meter) => meter.power_w === null);
          if (silent.length) {
            // Say why, in terms of the route each silent plug is on.
            const google = silent.some((meter) => meter.path === "google_home");
            parts.push(this.make("p", "note", (google
              ? "Google Home reports only whether a plug is on. "
              : "These plugs do not report power on the route the hub reaches them by. ")
              + "An energy-monitoring plug paired with Porch over Matter also reports watts and kWh, and can be switched."));
          }
        } else {
          parts.push(this.make("p", "empty", "This home has no plug and no device that reports power."));
        }
      } else if (!this.problem) {
        parts.push(this.make("p", "empty", "Reading…"));
      }
      if (this.problem) parts.push(this.make("p", "note problem", this.problem));
      this.body.replaceChildren(...parts);
    }
  }

  window.PorchEnergy = { watts, kwh };
  if (!customElements.get("porch-energy")) customElements.define("porch-energy", PorchEnergy);
})();
