// The menu shared by every page the hub serves: <porch-nav> renders a "Pages" button that opens a
// list of all pages. Its styles live in a shadow root, so no page's CSS reaches it and it reaches
// no page. The button sizes itself in em, so it follows the header it sits in (the stage scales up).
//
// Next to the button it shows family protection, when the hub enforces it (GET /protection):
// "Family protection on" while real devices take no command, or the open control window with
// the time it has left. A hub that does not enforce it shows nothing. The label is itself a
// button: it opens a small panel where the owner opens a control window (devices, minutes and the
// owner's PIN) or closes the open one, which needs no PIN.
//
// When Porch holds an agent's request for someone (GET /household/approvals), a "waiting" label
// shows next to it on every page, and its panel answers the request with that person's PIN. A yes
// makes Porch carry the request out at once; the owner's yes also lets that one command through
// family protection, once.
(() => {
  const REPO = "https://github.com/yooncham/porch-device-hub-yc/blob/main";

  const PAGES = [
    { href: "/", name: "Home", desc: "Every page in one place", use: "Finding your way around" },
    { href: "/dashboard", name: "Dashboard", desc: "Every device, its state and commands, with read-back",
      use: "Sending commands and checking what the hub sees" },
    { href: "/digital-twin", name: "Digital twin", desc: "This home's floor plan with every device on it, live from the hub",
      use: "Showing where each device is and switching it from the plan" },
    { href: "/home-3d", name: "3D home", desc: "The same home in 3D: rooms, devices and what each one is doing",
      use: "Walking someone through the home" },
    { href: "/porch", name: "How Porch works", desc: "Agents, Porch's rules and the people at home, and the devices, live",
      use: "Showing someone how the whole of Porch fits together" },
    { href: "/flow", name: "Live flow", desc: "Each agent request, drawn check by check as it moves through Porch",
      use: "Recording or presenting how a request travels" },
    { href: "/live", name: "Live flow + 3D", desc: "The Live flow and the 3D home side by side, both live",
      use: "Showing what Porch decides and what it does in the home, at once" },
    { href: "/stage", name: "Stage", desc: "Projector display: agents, safety alerts, hardware, audit",
      use: "The big screen during a live demo" },
    { href: "/docs", name: "API", desc: "Interface v0: every endpoint, with a form to try it",
      use: "Building an agent or engine against the hub" },
  ];

  const DOCS = [
    { href: `${REPO}/docs/FIELD_GUIDE.md`, name: "Field guide", desc: "How the hub works, in plain words" },
    { href: `${REPO}/docs/HW7_BENCHMARK.md`, name: "Benchmark write-up", desc: "HW7: Porch against direct device APIs" },
    { href: `${REPO}/README.md`, name: "README", desc: "Setup, every route, how to run each page" },
  ];

  const here = location.pathname.replace(/\/+$/, "") || "/";
  // Inside another hub page (the side-by-side Live page frames two pages): that page shows the one
  // menu, so this one stays hidden and hands its requests to it.
  const outer = () => {
    try {
      return window.parent !== window ? window.parent.document.querySelector("porch-nav") : null;
    } catch {
      return null;  // framed by some other site: act as a page of its own
    }
  };

  const CSS = `
    :host { display: inline-flex; font: inherit; color: inherit; }
    * { box-sizing: border-box; }
    .open-button {
      display: inline-flex; align-items: center; gap: .45em; padding: .35em .75em; cursor: pointer;
      border: 1px solid rgba(148, 163, 184, .35); border-radius: .5em; background: rgba(15, 23, 42, .85);
      color: #e2e8f0; font: 600 .8em/1.2 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    }
    .open-button:hover { border-color: #5eead4; }
    .open-button svg { width: 1.1em; height: 1.1em; }
    .protection {
      display: inline-flex; align-items: center; gap: .45em; margin-left: .6em; padding: .35em .8em;
      border: 1px solid; border-radius: 999px; white-space: nowrap; cursor: pointer;
      font: 700 .74em/1.2 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; letter-spacing: .05em;
    }
    .protection:hover { filter: brightness(1.2); }
    .panel-backdrop { position: fixed; inset: 0; z-index: 2147483000; }
    .panel {
      position: fixed; z-index: 2147483001; width: min(24em, 100vw - 24px); max-height: calc(100vh - 24px);
      overflow-y: auto; padding: 1em 1.1em 1.1em; border: 1px solid #334155; border-radius: .8em;
      background: #0b1220; color: #e2e8f0; box-shadow: 0 16px 48px rgba(0, 0, 0, .65);
      font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, "Apple SD Gothic Neo", sans-serif;
    }
    .panel h3 { margin: 0 0 .3em; font-size: 1.1em; }
    .panel p { margin: 0 0 .8em; color: #94a3b8; font-size: .9em; }
    .panel fieldset { margin: 0 0 .8em; padding: .4em .7em .5em; border: 1px solid #1e293b; border-radius: .5em; }
    .panel legend { padding: 0 .3em; color: #94a3b8; font-size: .75em; letter-spacing: .08em; text-transform: uppercase; }
    .panel .device { display: flex; align-items: center; gap: .5em; padding: .2em 0; cursor: pointer; }
    .panel .device small { margin-left: auto; color: #64748b; }
    .panel .row { display: flex; gap: .6em; margin-bottom: .8em; }
    .panel .row label { flex: 1; display: grid; gap: .25em; color: #94a3b8; font-size: .8em; }
    .panel select, .panel input[type="password"] {
      width: 100%; padding: .45em .6em; border: 1px solid #334155; border-radius: .45em;
      background: #020617; color: #e2e8f0; font: inherit; font-size: 1.2em;
    }
    .panel .act { width: 100%; padding: .6em .8em; border: 0; border-radius: .5em; cursor: pointer; font: 700 .95em/1.2 inherit; font-family: inherit; }
    .panel .act.open { background: #34d399; color: #022c22; }
    .panel .act.close { background: #fbbf24; color: #1c1917; }
    .panel .act.change { margin-top: .5em; background: transparent; color: #cbd5e1; border: 1px solid rgba(148, 163, 184, .45); font-weight: 600; }
    .panel .act.change:hover { background: rgba(148, 163, 184, .12); }
    .panel .act:disabled { opacity: .5; cursor: not-allowed; }
    .panel .error { min-height: 1.3em; margin: .6em 0 0; color: #fda4af; font-size: .9em; }
    .panel .hint { padding: .5em .7em; border: 1px solid rgba(251, 191, 36, .4); border-radius: .5em; color: #fde68a; }
    .panel .always { padding: .5em .7em; border: 1px solid rgba(251, 191, 36, .4); border-radius: .5em; color: #fde68a; }
    .panel code { font: .92em ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; user-select: all; }
    .protection .dot { width: .6em; height: .6em; border-radius: 50%; background: currentColor; }
    .protection.on { color: #fbbf24; border-color: rgba(251, 191, 36, .55); background: rgba(251, 191, 36, .12); }
    .protection.open { color: #6ee7b7; border-color: rgba(110, 231, 183, .55); background: rgba(110, 231, 183, .12); }
    .protection .clock { font-variant-numeric: tabular-nums; }
    .waiting {
      display: inline-flex; align-items: center; gap: .45em; margin-left: .6em; padding: .35em .8em;
      border: 1px solid rgba(251, 191, 36, .85); border-radius: 999px; white-space: nowrap; cursor: pointer;
      background: rgba(251, 191, 36, .22); color: #fde68a;
      font: 700 .74em/1.2 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; letter-spacing: .05em;
      animation: porch-waiting 1.8s ease-in-out infinite;
    }
    .waiting:hover { filter: brightness(1.2); }
    @keyframes porch-waiting { 50% { box-shadow: 0 0 0 .35em rgba(251, 191, 36, .25); } }
    @media (prefers-reduced-motion: reduce) { .waiting { animation: none; } }
    .ask { margin: 0 0 .9em; padding: .7em .8em; border: 1px solid rgba(251, 191, 36, .45); border-radius: .6em; }
    .ask .what { margin: 0 0 .25em; color: #e2e8f0; font-weight: 700; font-size: 1em; }
    .ask .said { color: #cbd5e1; font-style: italic; }
    .ask .note { padding: 0; color: #fde68a; font-size: .85em; }
    .ask .buttons { display: flex; gap: .5em; }
    .panel .act.yes { background: #34d399; color: #022c22; }
    .panel .act.no { background: transparent; color: #fda4af; border: 1px solid rgba(251, 113, 133, .6); }
    .ask .result { margin: .6em 0 0; color: #6ee7b7; font-size: .9em; }
    .ask .result.bad { color: #fda4af; }
    button:focus-visible, a:focus-visible { outline: 2px solid #5eead4; outline-offset: 2px; }
    .backdrop { position: fixed; inset: 0; z-index: 2147483000; background: rgba(2, 6, 23, .6); }
    .drawer {
      position: fixed; top: 0; left: 0; bottom: 0; z-index: 2147483001; width: min(340px, 100vw - 32px);
      overflow-y: auto; padding: 18px 16px 24px; background: #0b1220; color: #e2e8f0;
      border-right: 1px solid #1e293b; box-shadow: 0 0 48px rgba(0, 0, 0, .6);
      font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, "Apple SD Gothic Neo", sans-serif;
    }
    [hidden] { display: none !important; }
    .head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
    .brand { font-weight: 800; letter-spacing: .14em; font-size: 15px; }
    .close {
      border: 1px solid #1e293b; background: transparent; color: #94a3b8; border-radius: 6px;
      width: 32px; height: 32px; font-size: 18px; line-height: 1; cursor: pointer;
    }
    .close:hover { color: #e2e8f0; border-color: #334155; }
    h2 { margin: 18px 4px 6px; font-size: 11px; letter-spacing: .12em; text-transform: uppercase; color: #94a3b8; }
    ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 2px; }
    a { display: block; padding: 8px 10px; border-radius: 8px; color: inherit; text-decoration: none; border: 1px solid transparent; }
    a:hover { background: #111a2e; border-color: #1e293b; }
    a[aria-current="page"] { background: rgba(94, 234, 212, .1); border-color: rgba(94, 234, 212, .35); }
    .name { display: flex; align-items: center; gap: 8px; font-weight: 600; }
    .name code { margin-left: auto; color: #64748b; font: 11px ui-monospace, SFMono-Regular, Menlo, monospace; }
    .desc { display: block; color: #94a3b8; font-size: 12.5px; margin-top: 1px; }
    .ext { color: #64748b; font-size: 11px; font-weight: 500; }
    .note { margin: 12px 4px 0; color: #64748b; font-size: 11.5px; }
  `;

  // ["On", "Off", "Play"] -> "On, Off and Play"
  const listed = (words) => (words.length > 1 ? `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}` : words.join(""));
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  class PorchNav extends HTMLElement {
    connectedCallback() {
      if (outer()) {
        this.hidden = true;
        this.style.display = "none";
        return;
      }
      if (this.shadowRoot) {  // moved within the page: the menu is already built
        this.watchProtection();
        this.watchApprovals();
        return;
      }
      const root = this.attachShadow({ mode: "open" });
      const pageItems = PAGES.map((p) => `
        <li><a href="${p.href}"${p.href === here ? ' aria-current="page"' : ""}>
          <span class="name">${esc(p.name)}<code>${esc(p.href)}</code></span>
          <span class="desc">${esc(p.desc)}</span></a></li>`).join("");
      const docItems = DOCS.map((d) => `
        <li><a href="${d.href}" target="_blank" rel="noopener">
          <span class="name">${esc(d.name)} <span class="ext">GitHub ↗</span></span>
          <span class="desc">${esc(d.desc)}</span></a></li>`).join("");
      root.innerHTML = `
        <style>${CSS}</style>
        <button class="open-button" type="button" aria-haspopup="dialog" aria-expanded="false">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 4h12M2 8h12M2 12h12" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
          Pages
        </button>
        <button class="protection" type="button" aria-haspopup="dialog" aria-expanded="false" hidden></button>
        <button class="waiting" type="button" aria-haspopup="dialog" aria-expanded="false" hidden></button>`;
      this.protection = root.querySelector(".protection");
      this.waiting = root.querySelector(".waiting");
      // The drawer lives under <body>, not in the header: a header with a blur or transform would
      // otherwise become the box that position: fixed is measured from, and trap the drawer inside it.
      this.layer = document.createElement("div");
      this.layer.className = "porch-nav-layer";
      const layer = this.layer.attachShadow({ mode: "open" });
      layer.innerHTML = `
        <style>${CSS}</style>
        <div class="backdrop" hidden></div>
        <nav class="drawer" role="dialog" aria-modal="true" aria-label="Porch pages" hidden>
          <div class="head"><span class="brand">PORCH</span>
            <button class="close" type="button" aria-label="Close menu">×</button></div>
          <h2>Pages on this hub</h2><ul>${pageItems}</ul>
          <h2>Docs</h2><ul>${docItems}</ul>
          <p class="note">Docs open on GitHub and need access to the private repo.</p>
        </nav>
        <div class="panel-backdrop" hidden></div>
        <section class="panel" role="dialog" aria-label="Family protection" hidden></section>
        <section class="panel approvals" role="dialog" aria-label="Requests waiting for an answer" hidden></section>`;
      document.body.append(this.layer);
      this.button = root.querySelector(".open-button");
      // Recording (?record): the menu button would only show up in the video; the labels stay. Device
      // response times stay out too: they change as Porch improves.
      this.recording = new URLSearchParams(location.search).has("record");
      if (this.recording) this.button.hidden = true;
      this.drawer = layer.querySelector(".drawer");
      this.backdrop = layer.querySelector(".backdrop");
      this.button.addEventListener("click", () => this.toggle(true));
      layer.querySelector(".close").addEventListener("click", () => this.toggle(false));
      this.backdrop.addEventListener("click", () => this.toggle(false));
      this.panel = layer.querySelector(".panel");
      this.panelBackdrop = layer.querySelector(".panel-backdrop");
      this.protection.addEventListener("click", () => this.togglePanel(this.panel.hidden));
      this.askPanel = layer.querySelector(".approvals");
      this.waiting.addEventListener("click", () => this.toggleApprovals(this.askPanel.hidden));
      this.panelBackdrop.addEventListener("click", () => { this.togglePanel(false); this.toggleApprovals(false); });
      this.onKey = (event) => {
        if (event.key !== "Escape") return;
        if (!this.askPanel.hidden) this.toggleApprovals(false);
        else if (!this.panel.hidden) this.togglePanel(false);
        else if (!this.drawer.hidden) this.toggle(false);
      };
      document.addEventListener("keydown", this.onKey);
      this.watchProtection();
      this.watchApprovals();
    }

    disconnectedCallback() {
      clearInterval(this.protectionPoll);
      clearInterval(this.protectionTick);
      clearInterval(this.approvalsPoll);
    }

    // Family protection: ask the hub every few seconds, and count the open window down in between.
    watchProtection() {
      const read = async () => {
        try {
          const response = await fetch("/protection", { cache: "no-store" });
          this.protectionState = response.ok ? await response.json() : null;
        } catch {
          this.protectionState = null;  // hub unreachable: say nothing rather than something stale
        }
        // Names for the devices the label mentions: the open window's and the always-allowed ones.
        const state = this.protectionState;
        const mentioned = [...(state && state.live_writes_enabled ? state.device_ids : []), ...this.alwaysIds()];
        if (mentioned.length) await this.nameWindowDevices(mentioned);
        this.announceProtection();
      };
      clearInterval(this.protectionPoll);  // never two watchers on one menu
      clearInterval(this.protectionTick);
      read();
      this.protectionPoll = setInterval(read, 3000);
      this.protectionTick = setInterval(() => this.renderProtection(), 1000);
    }

    // Show the new state and let the page react, for example the stage announces a window opening.
    announceProtection() {
      this.renderProtection();
      document.dispatchEvent(new CustomEvent("porch:protection", { detail: this.protectionState }));
    }

    secondsLeft() {
      const state = this.protectionState;
      return state && state.expires_at ? Math.floor((new Date(state.expires_at) - Date.now()) / 1000) : 0;
    }

    windowIsOpen() {
      return Boolean(this.protectionState && this.protectionState.live_writes_enabled) && this.secondsLeft() > 0;
    }

    nameOf(id) {
      return (this.deviceNames && this.deviceNames.get(id)) || id.split("/").pop();
    }

    windowNames() {
      return this.protectionState.device_ids.map((id) => this.nameOf(id));
    }

    // The devices the owner always allows, with no window: a standing choice made on the hub's computer.
    alwaysIds() {
      const always = this.protectionState && this.protectionState.always_allowed;
      return (always && always.device_ids) || [];
    }

    // One sentence about them for the panel, or null when there are none.
    alwaysNote() {
      const ids = this.alwaysIds();
      if (!ids.length) return null;
      const note = document.createElement("p");
      note.className = "always";
      note.textContent = `Always allowed, with no window: ${listed(ids.map((id) => this.nameOf(id)))} `
        + `(${listed(this.protectionState.always_allowed.commands)}). The owner sets this on the hub's computer.`;
      return note;
    }

    async nameWindowDevices(ids) {
      const key = ids.join("\n");
      if (key === this.namedFor) return;
      this.namedFor = key;
      this.deviceNames = new Map();
      try {
        const devices = await (await fetch("/devices", { cache: "no-store" })).json();
        for (const device of devices) this.deviceNames.set(device.id, device.name);
      } catch { /* the ids are shown instead */ }
    }

    renderProtection() {
      const pill = this.protection;
      const state = this.protectionState;
      if (!state || !state.enforced) {
        pill.hidden = true;
        return;
      }
      const left = this.secondsLeft();
      const open = this.windowIsOpen();
      pill.hidden = false;
      pill.className = `protection ${open ? "open" : "on"}`;
      const dot = document.createElement("span");
      dot.className = "dot";
      const always = this.alwaysIds();
      if (!open) {
        // Said on the label itself, so nobody at home takes "protection on" to cover every device.
        pill.replaceChildren(dot, always.length ? `FAMILY PROTECTION ON · ${always.length} ALWAYS ALLOWED` : "FAMILY PROTECTION ON");
        pill.title = always.length
          ? `Real devices take no command, except ${listed(always.map((id) => this.nameOf(id)))}, which the owner always allows. Click to open a control window.`
          : "Real devices take no command. Click to open a control window.";
      } else {
        // Device names come from the hub, so they go in as text, never as markup.
        const names = this.windowNames();
        const clock = document.createElement("span");
        clock.className = "clock";
        clock.textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
        pill.replaceChildren(dot, "CONTROL WINDOW OPEN ", clock, ` · ${names.join(", ")}`);
        pill.title = `The owner allows ${listed(state.commands)} on ${names.join(", ")}. Click to close the window.`;
      }
      this.updatePanel();
    }

    // The panel under the label: open a control window, or close the open one.

    // For a page: open the panel with these devices already ticked. While a window is open that
    // does not cover them, the panel goes straight to changing it.
    openProtectionPanel(deviceIds = []) {
      const around = outer();
      if (around && around !== this && around.openProtectionPanel) return around.openProtectionPanel(deviceIds);
      this.preselect = new Set(deviceIds);
      this.opener = document.activeElement;  // where the reader was: focus goes back there
      const state = this.protectionState;
      this.changing = this.windowIsOpen() && deviceIds.some((id) => !state.device_ids.includes(id));
      this.togglePanel(true);
    }

    togglePanel(open) {
      this.panel.hidden = this.panelBackdrop.hidden = !open;
      this.protection.setAttribute("aria-expanded", String(open));
      if (!open) {
        this.preselect = null;
        this.changing = false;
        // Back to where the reader was, without jumping a scrolled page to the label at its top.
        const opener = this.opener && this.opener.isConnected ? this.opener : this.protection;
        this.opener = null;
        opener.focus({ preventScroll: true });
        return;
      }
      // The panel sits under <body>, so it takes its size from the menu button it belongs to.
      this.panel.style.fontSize = `${Math.max(13, parseFloat(getComputedStyle(this.button).fontSize) * 1.05)}px`;
      // Under the label, or at the top of the window when the page is scrolled past the label.
      const at = this.protection.getBoundingClientRect();
      this.panel.style.top = `${Math.round(Math.max(12, at.bottom + 8))}px`;
      this.panel.style.left = `${Math.round(Math.max(12, Math.min(at.left, innerWidth - this.panel.offsetWidth - 12)))}px`;
      this.buildPanel();
      this.loadSwitchable().then(() => this.buildPanel());
    }

    // The devices a window can be opened for: the ones that take any command.
    async loadSwitchable() {
      try {
        const devices = await (await fetch("/devices", { cache: "no-store" })).json();
        this.deviceNames = new Map(devices.map((d) => [d.id, d.name]));
        this.switchable = devices
          .filter((d) => Object.values(d.clusters).some((cluster) => (cluster.commands || []).length))
          .map((d) => ({ id: d.id, name: d.name, room: d.room }));
      } catch {
        this.switchable = [];
      }
    }

    buildPanel() {
      const state = this.protectionState;
      if (this.panel.hidden || !state) return;
      const make = (tag, className, text) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
      };
      const windowOpen = this.windowIsOpen();
      this.panelKind = this.kindOfPanel();
      if (this.panelKind === "open") {
        this.panelCountdown = make("p");
        const close = make("button", "act close", "Close now and protect");
        close.type = "button";
        close.addEventListener("click", () => this.closeWindow());
        // A new window replaces the open one, so adding a device or more time goes through the
        // same form, and the same PIN.
        const change = make("button", "act change", "Change devices or time");
        change.type = "button";
        change.addEventListener("click", () => { this.changing = true; this.buildPanel(); });
        this.panelError = make("p", "error");
        this.panelError.setAttribute("role", "alert");
        this.panel.replaceChildren(...[make("h3", "", "Control window open"), this.panelCountdown, this.alwaysNote(),
                                       close, change, this.panelError].filter(Boolean));
        this.updatePanel();
        close.focus();
        return;
      }
      const form = make("form");
      form.noValidate = true;
      const devices = make("fieldset");
      devices.append(make("legend", "", "Devices"));
      const switchable = this.switchable || [];
      if (!switchable.length) devices.append(make("p", "", this.switchable ? "No device here takes commands." : "Loading devices…"));
      for (const device of switchable) {
        const label = make("label", "device");
        const box = make("input");
        box.type = "checkbox";
        box.name = "device";
        box.value = device.id;
        box.checked = Boolean(this.preselect && this.preselect.has(device.id))
          || (windowOpen && state.device_ids.includes(device.id));
        label.append(box, device.name, make("small", "", device.room));
        devices.append(label);
      }
      const row = make("div", "row");
      const minutesLabel = make("label", "", "Minutes");
      const minutes = make("select");
      for (const choice of [5, 10, 15, 30].filter((m) => m <= (state.max_minutes || 30))) {
        const option = make("option", "", String(choice));
        option.value = String(choice);
        minutes.append(option);
      }
      minutesLabel.append(minutes);
      const pinLabel = make("label", "", "Owner PIN");
      const pin = make("input");
      pin.type = "password";
      pin.autocomplete = "off";
      pin.disabled = !state.pin_set;
      pinLabel.append(pin);
      row.append(minutesLabel, pinLabel);
      const submit = make("button", "act open", windowOpen ? "Open the new window" : "Open control window");
      submit.type = "submit";
      submit.disabled = !state.pin_set || !switchable.length;
      this.panelError = make("p", "error");
      this.panelError.setAttribute("role", "alert");
      form.append(devices, row, submit, this.panelError);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const chosen = [...form.querySelectorAll('input[name="device"]:checked')].map((box) => box.value);
        this.openWindow(chosen, Number(minutes.value), pin);
      });
      const parts = windowOpen
        ? [make("h3", "", "Change the control window"),
           make("p", "", "The new window replaces the open one. Untick a device to protect it again.")]
        : [make("h3", "", "Family protection is on"),
           make("p", "", "Real devices take no command. As the owner, you can let chosen devices be switched for a few minutes.")];
      const standing = this.alwaysNote();
      if (standing) parts.push(standing);
      if (!state.pin_set) {
        const hint = make("p", "hint");
        hint.append("Choose an owner PIN first, in Terminal on the hub's computer: ",
                    make("code", "", state.set_pin_command || "deploy/home/owner.sh set-pin"));
        parts.push(hint);
      }
      this.panel.replaceChildren(...parts, form);
    }

    // What the panel shows: the form to open a window ("closed"), the open window ("open"), or
    // the form to replace the open window ("change").
    kindOfPanel() {
      return this.windowIsOpen() ? (this.changing ? "change" : "open") : "closed";
    }

    // Called every second: the countdown, and a rebuild when the window opens or closes meanwhile.
    updatePanel() {
      if (!this.panel || this.panel.hidden || !this.protectionState) return;
      const kind = this.kindOfPanel();
      if (kind !== this.panelKind) {
        this.buildPanel();
        return;
      }
      if (kind !== "open") return;
      const left = this.secondsLeft();
      this.panelCountdown.textContent = `${this.windowNames().join(", ")} can take ${listed(this.protectionState.commands)} `
        + `for ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} more. Everything else stays protected.`;
    }

    async sendProtection(path, body) {
      try {
        const response = await fetch(path, {
          method: "POST",
          headers: body ? { "content-type": "application/json" } : {},
          body: body ? JSON.stringify(body) : undefined,
        });
        const data = await response.json().catch(() => ({}));
        if (response.ok) return { state: data };
        return { error: typeof data.detail === "string" ? data.detail : "The hub refused the request." };
      } catch {
        return { error: "The hub did not answer." };
      }
    }

    async openWindow(deviceIds, minutes, pinInput) {
      if (!deviceIds.length) {
        this.panelError.textContent = "Choose at least one device.";
        return;
      }
      const pin = pinInput.value;
      pinInput.value = "";  // never left in the page
      if (!pin) {
        this.panelError.textContent = "Enter the owner PIN.";
        pinInput.focus();
        return;
      }
      const answer = await this.sendProtection("/protection/window", { device_ids: deviceIds, minutes, pin });
      if (answer.error) {
        this.panelError.textContent = answer.error;
        pinInput.focus();
        return;
      }
      this.protectionState = answer.state;
      this.announceProtection();
      this.togglePanel(false);
    }

    async closeWindow() {
      const answer = await this.sendProtection("/protection/close");
      if (answer.error) {
        this.panelError.textContent = answer.error;
        return;
      }
      this.protectionState = answer.state;
      this.announceProtection();
      this.togglePanel(false);
    }

    // ---- Requests waiting for someone's yes (the gateway holds them; GET /household/approvals)

    watchApprovals() {
      const read = async () => {
        try {
          const response = await fetch("/household/approvals", { cache: "no-store" });
          this.approvals = response.ok ? await response.json() : [];
        } catch {
          this.approvals = [];  // no gateway: nothing to answer
        }
        if (!Array.isArray(this.approvals)) this.approvals = [];
        this.renderWaiting();
      };
      clearInterval(this.approvalsPoll);
      read();
      this.approvalsPoll = setInterval(read, 4000);
    }

    renderWaiting() {
      const count = this.approvals.length;
      this.waiting.hidden = count === 0;
      this.waiting.textContent = count === 1 ? "1 REQUEST WAITING FOR A YES" : `${count} REQUESTS WAITING FOR A YES`;
      this.waiting.title = "An agent's request is held until the right person says yes. Click to answer it.";
      // Rebuild an open panel when the requests change, but never under someone typing a PIN.
      const ids = this.approvals.map((a) => a.id).join(",");
      const typing = this.askPanel && this.askPanel.contains(this.askPanel.getRootNode().activeElement);
      if (this.askPanel && !this.askPanel.hidden && ids !== this.shownIds && !typing && !this.answering) this.buildApprovals();
    }

    async toggleApprovals(open) {
      if (!this.askPanel) return;
      this.askPanel.hidden = this.panelBackdrop.hidden = !open;
      this.waiting.setAttribute("aria-expanded", String(open));
      if (!open) {
        this.waiting.focus({ preventScroll: true });
        return;
      }
      this.askPanel.style.fontSize = `${Math.max(13, parseFloat(getComputedStyle(this.button).fontSize) * 1.05)}px`;
      const at = this.waiting.getBoundingClientRect();
      this.askPanel.style.width = "min(28em, 100vw - 24px)";
      this.askPanel.style.top = `${Math.round(Math.max(12, at.bottom + 8))}px`;
      this.askPanel.style.left = `${Math.round(Math.max(12, Math.min(at.left, innerWidth - this.askPanel.offsetWidth - 12)))}px`;
      try {  // who may answer: the people named, or the owner for a request held for the owner
        this.people = (await (await fetch("/household", { cache: "no-store" })).json()).people || [];
      } catch {
        this.people = [];
      }
      this.buildApprovals();
    }

    buildApprovals() {
      const make = (tag, className, text) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
      };
      this.shownIds = this.approvals.map((a) => a.id).join(",");
      const owner = (this.people || []).find((p) => p.owner);
      const enforced = Boolean(this.protectionState && this.protectionState.enforced);
      const parts = [make("h3", "", "Waiting for a yes"),
                     make("p", "", "Porch holds these until the right person answers with their PIN. A yes makes Porch carry the request out at once.")];
      if (!this.approvals.length) parts.push(make("p", "", "Nothing is waiting now."));
      for (const a of this.approvals) {
        // Everything here comes from agents and the hub: set as text only.
        const card = make("div", "ask");
        card.append(make("p", "what", `${a.agent_name || a.agent} wants ${a.command} on ${a.device_name || a.device}`),
                    make("p", "", a.why_held || ""));
        if (a.reason) card.append(make("p", "said", `The agent said: “${a.reason}”`));
        const who = (a.approvers || []).length
          ? a.approvers.map((id, i) => ({ id, name: (a.approver_names || [])[i] || id }))
          : (owner ? [{ id: owner.id, name: `${owner.name} (the owner)` }] : []);
        const row = make("div", "row");
        const personLabel = make("label", "", "Who answers");
        const person = make("select");
        for (const p of who) {
          const option = make("option", "", p.name);
          option.value = p.id;
          person.append(option);
        }
        personLabel.append(person);
        const pinLabel = make("label", "", "Their PIN");
        const pin = make("input");
        pin.type = "password";
        pin.autocomplete = "off";
        pinLabel.append(pin);
        row.append(personLabel, pinLabel);
        const note = make("p", "note", "");
        const explain = () => {
          const ownerAnswers = owner && person.value === owner.id;
          note.textContent = !enforced ? ""
            : ownerAnswers ? "As the owner, your yes also lets this one command through family protection, once."
            : "Family protection still decides: only the owner's yes, or a control window, lets it reach the device.";
        };
        person.addEventListener("change", explain);
        explain();
        const yes = make("button", "act yes", "Approve");
        const no = make("button", "act no", "Decline");
        yes.type = no.type = "button";
        const buttons = make("div", "buttons");
        buttons.append(yes, no);
        const result = make("p", "result");
        result.setAttribute("role", "status");
        const answer = async (verdict) => {
          const code = pin.value;
          pin.value = "";  // never left in the page
          if (!code) {
            result.className = "result bad";
            result.textContent = "Enter the PIN of the person answering.";
            pin.focus();
            return;
          }
          yes.disabled = no.disabled = true;
          this.answering = true;
          result.className = "result";
          result.textContent = verdict === "approve" ? "Porch is carrying it out…" : "Declining…";
          try {
            const response = await fetch(`/household/approvals/${encodeURIComponent(a.id)}/${verdict}`, {
              method: "POST", headers: { "content-type": "application/json" },
              body: JSON.stringify({ person: person.value, pin: code }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : (data.error || "The hub refused."));
            result.textContent = this.outcomeWords(verdict, data.outcome, a);
            result.className = `result${verdict === "approve" && !(data.outcome && data.outcome.executed) ? " bad" : ""}`;
          } catch (problem) {
            result.className = "result bad";
            result.textContent = problem.message;
            yes.disabled = no.disabled = false;
          } finally {
            this.answering = false;
          }
        };
        yes.addEventListener("click", () => answer("approve"));
        no.addEventListener("click", () => answer("decline"));
        card.append(row, note, buttons, result);
        parts.push(card);
      }
      this.askPanel.replaceChildren(...parts);
    }

    // What happened after an answer, in one sentence.
    outcomeWords(verdict, outcome, a) {
      const what = `${a.command} on ${a.device_name || a.device}`;
      if (verdict !== "approve") return `Declined. Nothing happens to ${a.device_name || "the device"}.`;
      if (!outcome) return "Approved.";
      const result = outcome.decision === "already_carried_out" ? outcome.outcome || {} : outcome;
      if (result.executed && result.verified) return `Done: ${what}, read back${this.recording ? "" : ` in ${result.latency_ms} ms`}.`;
      if (result.executed) return `Sent, but the read-back did not match: ${result.hub_error || result.error || "not confirmed"}.`;
      return `Approved, but not done: ${result.error || result.reason || "Porch did not send it"}`;
    }

    toggle(open) {
      this.drawer.hidden = this.backdrop.hidden = !open;
      this.button.setAttribute("aria-expanded", String(open));
      (open ? (this.drawer.querySelector('[aria-current="page"]') || this.drawer.querySelector("a")) : this.button).focus();
    }
  }

  window.PORCH_PAGES = PAGES;
  window.PORCH_DOCS = DOCS;
  if (!customElements.get("porch-nav")) customElements.define("porch-nav", PorchNav);
})();
