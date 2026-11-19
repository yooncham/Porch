// <porch-details device-id="home-1/air-purifier">: everything each platform reports about one device,
// read when the viewer asks (GET /devices/{id}/details), grouped by platform and by topic.
//
// It reads on demand because a full read asks the cloud platforms again. Its styles live in a
// shadow root and are sized in em, so it follows the panel it sits in. Text from the hub goes in
// as text, never as markup.
(() => {
  const PATHS = { matter: "Matter · local", lg_thinq: "LG ThinQ · cloud", google_home: "Google Home · cloud",
                  ring: "Ring · cloud", digital_copy: "The hub's digital copy" };
  const CSS = `
    :host { display: block; margin-top: .8em; font: inherit; color: inherit; }
    * { box-sizing: border-box; }
    button { font: inherit; color: inherit; cursor: pointer; padding: .35em .8em; border: 1px solid #243049;
             border-radius: .5em; background: #111a2e; font-weight: 650; }
    button:hover { border-color: #5eead4; }
    button:focus-visible { outline: 2px solid #5eead4; outline-offset: 2px; }
    .note { margin: .4em 0 0; color: #8a97b0; font-size: .82em; }
    .problem { color: #fb7185; }
    section { margin-top: .7em; padding: .55em .7em; border: 1px solid #243049; border-radius: .6em; background: rgba(13, 20, 36, .6); }
    h4 { margin: 0; font-size: .78em; letter-spacing: .1em; text-transform: uppercase; color: #8a97b0; }
    h4 .off { color: #fbbf24; letter-spacing: 0; text-transform: none; font-weight: 600; margin-left: .4em; }
    h5 { margin: .6em 0 .15em; font-size: .8em; color: #5eead4; font-weight: 650; }
    dl { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: .15em .8em; margin: 0; font-size: .86em; }
    dt { color: #8a97b0; overflow-wrap: anywhere; }
    dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
  `;

  class PorchDetails extends HTMLElement {
    static get observedAttributes() { return ["device-id"]; }

    connectedCallback() {
      if (!this.shadowRoot) {
        const root = this.attachShadow({ mode: "open" });
        const style = document.createElement("style");
        style.textContent = CSS;
        this.body = document.createElement("div");
        root.append(style, this.body);
      }
      this.render(null);
    }

    attributeChangedCallback() {
      if (this.body) this.render(null);
    }

    make(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined && text !== null) node.textContent = text;
      return node;
    }

    async load() {
      const id = this.getAttribute("device-id");
      this.render({ loading: true });
      try {
        const path = id.split("/").map(encodeURIComponent).join("/");
        const response = await fetch(`/devices/${path}/details`, { cache: "no-store" });
        const data = await response.json().catch(() => ({}));
        if (response.status === 404 && !data.sections) throw new Error("This hub cannot show full details yet. Restart it.");
        if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${response.status}`);
        if (this.getAttribute("device-id") === id) this.render({ data, at: new Date() });
      } catch (error) {
        if (this.getAttribute("device-id") === id) this.render({ error: error.message });
      }
    }

    render(state) {
      const button = this.make("button", "", state && state.data ? "Read everything again" : "Everything the platform reports");
      button.type = "button";
      button.disabled = Boolean(state && state.loading);
      button.addEventListener("click", () => this.load());
      const parts = [button];
      if (!state) {
        parts.push(this.make("p", "note", "Asks each platform for its full record of this device. Serial numbers and network addresses are never shown."));
      } else if (state.loading) {
        parts.push(this.make("p", "note", "Asking the platforms…"));
      } else if (state.error) {
        parts.push(this.make("p", "note problem", state.error));
      } else {
        parts.push(this.make("p", "note", `Read at ${state.at.toLocaleTimeString([], { hour12: false })}.`));
        for (const section of state.data.sections) {
          const box = this.make("section");
          const title = this.make("h4", "", PATHS[section.path] || section.path);
          if (!section.online) title.append(this.make("span", "off", "not reachable now"));
          box.append(title);
          if (section.problem) box.append(this.make("p", "note problem", section.problem));
          let group = null, list = null;
          for (const row of section.rows) {
            if (row.group !== group) {
              group = row.group;
              box.append(this.make("h5", "", group));
              list = this.make("dl");
              box.append(list);
            }
            list.append(this.make("dt", "", row.name), this.make("dd", "", row.value));
          }
          if (!section.rows.length && !section.problem) box.append(this.make("p", "note", "Nothing reported."));
          parts.push(box);
        }
      }
      this.body.replaceChildren(...parts);
    }
  }

  if (!customElements.get("porch-details")) customElements.define("porch-details", PorchDetails);
})();
