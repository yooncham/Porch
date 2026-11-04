// The menu shared by every page the hub serves: <porch-nav> renders a "Pages" button that opens a
// list of all pages. Its styles live in a shadow root, so no page's CSS reaches it and it reaches
// no page. The button sizes itself in em, so it follows the header it sits in (the stage scales up).
(() => {
  const REPO = "https://github.com/yooncham/porch-device-hub-yc/blob/main";

  const PAGES = [
    { href: "/", name: "Home", desc: "Every page in one place", use: "Finding your way around" },
    { href: "/dashboard", name: "Dashboard", desc: "Every device, its state and commands, with read-back",
      use: "Sending commands and checking what the hub sees" },
    { href: "/digital-twin", name: "Digital twin", desc: "2D floor plan: the hardware beside its digital copy",
      use: "Showing offline mode and the digital copy" },
    { href: "/home-3d", name: "3D home", desc: "The home and its devices in 3D, live from the hub",
      use: "Walking someone through the home" },
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

  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  class PorchNav extends HTMLElement {
    connectedCallback() {
      if (this.shadowRoot) return;  // moved within the page: the menu is already built
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
        </button>`;
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
        </nav>`;
      document.body.append(this.layer);
      this.button = root.querySelector(".open-button");
      this.drawer = layer.querySelector(".drawer");
      this.backdrop = layer.querySelector(".backdrop");
      this.button.addEventListener("click", () => this.toggle(true));
      layer.querySelector(".close").addEventListener("click", () => this.toggle(false));
      this.backdrop.addEventListener("click", () => this.toggle(false));
      this.onKey = (event) => { if (event.key === "Escape" && !this.drawer.hidden) this.toggle(false); };
      document.addEventListener("keydown", this.onKey);
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
