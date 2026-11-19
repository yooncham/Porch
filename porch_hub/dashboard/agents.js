// The household's agents and what each asks Porch, live, for the digital twin and the 3D home.
//
// PorchAgents.follow({ onAgents, onStep }) loads the agents (GET /household/agents, which names
// each one's app and when it last called Porch), follows the gateway's stage feed, and turns each
// request (the gateway's "flow" messages, porch_gateway/flow.py) into a few steps, played at a
// pace a viewer can follow:
//   ask       an agent asks Porch                 (agent -> Porch)
//   decide    Porch's checks and its verdict      (allowed, held for someone, refused)
//   stopped   the hub refused it at the dry run   (family protection, for one)
//   send      the hub carries it to the device    (Porch -> device, through a platform)
//   done      Porch read the device back
//   read      what a read covered                 (Porch -> those devices)
//   answered  a person answered a held request
//   error     the request stopped on an error
// Only agents with an app (their "client") are shown. Text from the hub and the gateway is only
// ever set as text, never as markup.
(() => {
  const APPS = {
    "Claude Desktop": { short: "Claude Desktop", tone: "claude" },
    "Claude on the web": { short: "Claude web", tone: "claude" },
    "Claude phone app": { short: "Claude phone", tone: "claude" },
    ChatGPT: { short: "ChatGPT", tone: "chatgpt" },
  };
  const PLATFORMS = { matter: "Matter, on the home network", google_home: "Google Home, in the cloud",
                      lg_thinq: "LG ThinQ, in the cloud", ring: "Ring, in the cloud", digital_copy: "the hub's digital copy" };
  const VERBS = { On: "switch on", Off: "switch off", Toggle: "toggle", Play: "play", Pause: "pause", Start: "start",
                  Stop: "stop", GoHome: "send home", LockDoor: "lock", UnlockDoor: "unlock", Open: "open", Close: "close",
                  SetDelayStart: "delay the start of" };
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  // Recording (?record): a video shows what happened, not how long a device took to answer. Those
  // times change as Porch improves, so they stay out of anything recorded.
  const SHOW_TIMES = !new URLSearchParams(location.search).has("record");
  const inMs = (ms) => (SHOW_TIMES && ms != null ? ` in ${ms} ms` : "");

  // The hub's refusals end with a command for the owner's terminal, with its full path: on a page
  // people watch or record, the sentence before it says enough.
  function plain(text) {
    const cut = String(text || "").split(/,? or with: /)[0].trim();
    return cut.replace(/(?:\/(?:Users|home)\/[^\s"']+\/)([\w.-]+)/g, "$1");
  }
  function ago(iso) {
    if (!iso) return "not connected yet";
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 90) return "active now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }
  function stateWords(after) {
    const [key, value] = Object.entries(after || {})[0] || [];
    if (key === "OnOff.OnOff") return value ? "on" : "off";
    if (typeof value === "string") return value.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();
    return key ? `${key.split(".").pop()} ${value}` : "as predicted";
  }

  function follow({ onAgents, onStep, pace }) {
    const params = new URLSearchParams(location.search);
    const url = params.get("engine") || `ws://${location.hostname}:8000/ws/stage`;
    const speed = Math.min(4, Math.max(0.25, Number(params.get("pace")) || pace || 1));
    const agents = new Map();
    const flows = new Map();
    const order = [];
    const queue = [];
    let playing = false;

    const shown = () => [...agents.values()].filter((a) => a.client);
    async function load() {
      try {
        const response = await fetch("/household/agents", { cache: "no-store" });
        if (response.ok) for (const agent of await response.json()) agents.set(agent.id, { ...agents.get(agent.id), ...agent });
      } catch { /* the gateway is away: keep what we have */ }
      onAgents(shown());
    }
    function push(step, dwell) {
      queue.push({ step, dwell });
      play();
    }
    async function play() {
      if (playing) return;
      playing = true;
      while (queue.length) {
        const { step, dwell } = queue.shift();
        try { onStep(step); } catch (error) { console.error(error); }
        const hurry = queue.length > 8 ? 0.35 : queue.length > 3 ? 0.65 : 1;  // catch up when requests pile up
        await sleep(dwell * 1000 * speed * hurry);
      }
      playing = false;
    }
    const name = (f) => (f.agent || {}).name || "An agent";
    const what = (f) => (f.device || {}).name || "the device";

    function onFlow(m) {
      if (!flows.has(m.id) && m.phase !== "ask" && m.phase !== "approval") return;  // under way before the page opened
      let f = flows.get(m.id);
      if (!f) {
        f = { id: m.id };
        flows.set(m.id, f);
        order.push(m.id);
        while (order.length > 60) flows.delete(order.shift());
      }
      switch (m.phase) {
        case "ask": {
          Object.assign(f, { agent: m.agent, tool: m.tool, device: m.device, command: m.command, reason: m.reason });
          const known = agents.get(m.agent.id) || { id: m.agent.id, name: m.agent.name };
          agents.set(known.id, { ...known, last_seen: m.at || new Date().toISOString() });
          onAgents(shown());
          const text = m.tool === "request_action" ? `${name(f)} asks Porch to ${VERBS[m.command] || m.command} ${what(f)}.`
            : m.tool === "preview_action" ? `${name(f)} asks Porch what would happen: ${VERBS[m.command] || m.command} ${what(f)}.`
            : m.tool === "get_energy" ? `${name(f)} asks Porch for the home's energy.`
            : m.tool === "get_state" ? `${name(f)} asks Porch to read ${what(f)}.`
            : `${name(f)} asks Porch for the whole home.`;
          push({ kind: "ask", flow: f, text, sub: m.reason ? `“${m.reason}”` : "" }, 1.5);
          break;
        }
        case "rules": {
          f.decision = m.decision;
          const who = m.waiting_for || "the owner";
          const text = m.decision === "allow" ? (m.preview ? "Porch would allow it." : "Porch allows it.")
            : m.decision === "deny" ? "Porch refuses it. Nothing reaches the device."
            : m.preview ? `Porch would hold it for ${who}.` : `Porch holds it: ${who} must say yes.`;
          push({ kind: "decide", flow: f, decision: m.decision, steps: m.steps || [], text, sub: plain(m.why) }, m.decision === "allow" ? 1.6 : 3);
          break;
        }
        case "dry_run":
          if (!m.ok) push({ kind: "stopped", flow: f, text: /^Family protection/.test(m.reason || "")
            ? `Family protection stops it: no control window is open for ${what(f)}.` : "The digital copy says it would not work.",
          sub: plain(m.reason) }, 3);
          else if (m.preview) push({ kind: "preview", flow: f, text: "A preview: nothing was sent to the device.", sub: "" }, 2.4);
          break;
        case "sent":
          push({ kind: "send", flow: f, path: m.path, ok: m.status === "done",
                 text: m.status === "done" ? `The hub sends it through ${PLATFORMS[m.path] || m.path}.` : `${PLATFORMS[m.path] || "The platform"} did not confirm it.`,
                 sub: m.status === "done" ? `${name(f)} never signs in to ${(PLATFORMS[m.path] || "it").split(",")[0]}: Porch does it for the home.` : plain(m.error) }, 1.6);
          break;
        case "verified":
          push({ kind: "done", flow: f, verified: m.verified, after: m.after, latency: m.latency_ms,
                 text: m.verified ? `Porch read ${what(f)} back: ${stateWords(m.after)}. Verified${inMs(m.latency_ms)}.`
                                  : `Porch read ${what(f)} back, and it does not match. Not done.`,
                 sub: m.verified ? `${name(f)} can now tell ${(f.agent || {}).acts_for || "its person"} it is done.` : "" }, 2.6);
          break;
        case "read":
          push({ kind: "read", flow: f, devices: m.devices || [], paths: m.paths || [], steps: m.steps || [], text: m.what,
                 sub: `${name(f)} never signs in to Google, LG, Ring or Matter: Porch reads them for it.` }, 2.6);
          break;
        case "approval":
          if (!f.agent) Object.assign(f, { agent: agents.get(m.agent_id) || { id: m.agent_id, name: m.agent_id },
                                           device: { id: m.device_id, name: m.device_id }, command: m.command });
          push({ kind: "answered", flow: f, approved: m.approved, by: m.by,
                 text: m.approved ? `${m.by} said yes, with their PIN.` : `${m.by} said no.`,
                 sub: m.approved ? `${name(f)} may now ask again, for this exact request, once.` : "Nothing happens to the device." }, 2.6);
          break;
        case "error":
          push({ kind: "error", flow: f, text: "Porch could not finish this request.", sub: plain(m.text) }, 2.6);
          break;
      }
    }

    let retry = 1000;
    const open = () => {
      let socket;
      try { socket = new WebSocket(url); } catch { return; }
      socket.addEventListener("open", () => { retry = 1000; load(); });
      socket.addEventListener("message", (event) => {
        let message;
        try { message = JSON.parse(event.data); } catch { return; }
        if (message.type === "flow") onFlow(message);
      });
      socket.addEventListener("close", () => { setTimeout(open, retry); retry = Math.min(retry * 2, 15000); });
    };
    load();
    open();
    setInterval(load, 30000);
    setInterval(() => onAgents(shown()), 20000);  // "3 min ago" moves on
    return { agents: shown };
  }

  window.PorchAgents = { follow, APPS, ago, plain };
})();
