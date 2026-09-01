// mascotify — local web UI
//
// Thin on purpose: every decision that affects the output already lives in the
// Python pipeline, so this file only moves data and reflects state.

const $ = (id) => document.getElementById(id);
const api = async (path, opts = {}) => {
  const r = await fetch(path, opts);
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(body?.detail || body || `${r.status} ${r.statusText}`);
  return body;
};

const state = { config: null, action: "wave", job: null, busy: false };

// ── chrome ────────────────────────────────────────────────

let toastTimer;
function toast(msg, bad = false) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.toggle("bad", bad);
  el.classList.add("show");
  clearTimeout(toastTimer);
  // Errors from a provider are long and worth reading; successes are not.
  toastTimer = setTimeout(() => el.classList.remove("show"), bad ? 8000 : 3000);
}

function busy(on, el) {
  state.busy = on;
  document.querySelectorAll("button").forEach((b) => {
    if (b.dataset.keep === undefined) b.disabled = on;
  });
  if (el) el.classList.toggle("busy", on);
  if (!on) reflect();
}

function unlock(id, yes) {
  $(id).classList.toggle("locked", !yes);
}

function reflect() {
  const hasAnchor = !$("anchor-img").hidden;
  // Step 2 is never locked: the upload route inside it works without an anchor
  // and without a key, which is the whole point of having it.
  unlock("s2", true);
  unlock("s3", !!state.job);
  $("btn-animate").disabled = !hasAnchor || state.busy;
  $("btn-animate").title = hasAnchor ? "" : "Make or upload an anchor first";
}

// ── settings ──────────────────────────────────────────────

async function loadConfig() {
  const c = await api("/api/config");
  state.config = c;
  $("ver").textContent = "v" + c.version;
  $("provider-label").textContent = c.provider;
  $("key-dot").classList.toggle("ready", !!c.keyed[c.provider]);

  const sel = $("set-provider");
  sel.innerHTML = c.providers.map((p) => `<option value="${p}">${p}</option>`).join("");
  sel.value = c.provider;
  $("set-model").value = c.model;
  $("set-model").placeholder = c.default_models[c.provider] || "";

  const cost = c.cost[c.provider];
  $("cost-hint").textContent = c.keyed[c.provider]
    ? `Each generation calls ${c.provider} on your key — roughly $${cost.toFixed(2)} an image.`
    : `Add a ${c.provider} key in settings before generating.`;

  $("motions").innerHTML = Object.entries(c.motions)
    .map(
      ([k, v]) =>
        `<button class="motion" data-m="${k}" title="${v}" aria-pressed="${k === state.action}">${k}</button>`,
    )
    .join("");
  $("motions").querySelectorAll(".motion").forEach((b) =>
    b.addEventListener("click", () => {
      state.action = b.dataset.m;
      $("motions")
        .querySelectorAll(".motion")
        .forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    }),
  );
}

$("open-settings").dataset.keep = "1";
$("open-settings").addEventListener("click", () => $("settings").showModal());

$("set-provider").addEventListener("change", (e) => {
  const c = state.config;
  $("set-model").value = "";
  $("set-model").placeholder = c.default_models[e.target.value] || "";
});

$("settings").addEventListener("close", async (e) => {
  if ($("settings").returnValue !== "save") return;
  try {
    await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: $("set-provider").value,
        key: $("set-key").value,
        model: $("set-model").value,
      }),
    });
    $("set-key").value = ""; // never keep a key sitting in the DOM
    await loadConfig();
    toast("Settings saved");
  } catch (err) {
    toast(err.message, true);
  }
});

// ── step 1: anchor ────────────────────────────────────────

function showAnchor(bust = true) {
  const img = $("anchor-img");
  // Keyed, not raw: the user should see what the pipeline sees.
  img.src = "/api/ref?cut=1" + (bust ? "&t=" + Date.now() : "");
  img.hidden = false;
  $("anchor-empty").hidden = true;
  reflect();
}

$("btn-anchor").addEventListener("click", async () => {
  const character = $("character").value.trim();
  if (!character) return toast("Describe the character first", true);
  busy(true, $("anchor-frame"));
  try {
    const r = await api("/api/anchor", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ character }),
    });
    showAnchor();
    toast(`Anchor ready — ${r.model}`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false, $("anchor-frame"));
  }
});

$("upload").addEventListener("change", async (e) => {
  const file = e.target.files?.[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  busy(true, $("anchor-frame"));
  try {
    await api("/api/anchor/upload", { method: "POST", body: fd });
    showAnchor();
    toast("Anchor uploaded");
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false, $("anchor-frame"));
    e.target.value = "";
  }
});

// ── step 2: animate ───────────────────────────────────────

$("btn-animate").addEventListener("click", async () => {
  const [rows, cols] = $("grid").value.split("x").map(Number);
  const fps = Number($("fps").value);

  busy(true, document.querySelector(".stage"));
  $("stats").innerHTML = "";
  $("snips").innerHTML = "";
  $("export-row").hidden = true;
  unlock("s3", true);

  try {
    const r = await api("/api/animate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: state.action, rows, cols, fps }),
    });

    if (!r.ok) {
      // A failed sheet is an ordinary outcome, not an error. Say what went
      // wrong in the validator's own words and invite another roll.
      state.job = null;
      $("snips").innerHTML = `<div class="problems">
        <b>That sheet did not pass</b>
        <ul>${r.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>
        <p class="hint">Press Animate again — generation varies run to run.</p>
      </div>`;
      toast("Validation failed — try again", true);
      return;
    }

    state.job = r.job;
    renderResult(r);
    toast(`${r.frames} frames at ${r.fps} fps`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false, document.querySelector(".stage"));
  }
});

function renderResult(r) {
  const img = $("preview-img");
  img.src = `/api/preview/${r.job}?t=${Date.now()}`;
  // The mascot is the point of the page, so describe what was actually made
  // rather than leaving a generic label a screen reader would skip.
  img.alt = `Your mascot animated: ${r.job}, ${r.frames} frames at ${r.fps} fps`;
  img.hidden = false;
  $("preview-empty").hidden = true;
  $("preview-cap").textContent = `${r.job} · ${r.frames} frames · ${r.fps} fps`;

  const drift = (r.scale_spread * 100).toFixed(1);
  $("stats").innerHTML = `
    <dt>frames</dt><dd>${r.frames}${r.ping_pong ? " (ping-pong)" : ""}</dd>
    <dt>size drift</dt><dd class="${r.scale_spread > 0.1 ? "warn" : ""}">${drift}%</dd>
    <dt>loop seam</dt><dd>${r.seam}x</dd>
    <dt>model</dt><dd>${esc(r.model)}</dd>`;

  $("snips").innerHTML = r.exports
    .map((e) => {
      const line = (e.snippet || "").split("\n")[0];
      const body = line
        ? `<code>${esc(line)}</code>`
        : `<code class="muted">${e.files} files</code>`;
      return `<div class="snip"><b>${esc(e.target)}</b>${body}</div>`;
    })
    .join("");
  $("export-row").hidden = false;
}

const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );

$("sheet-upload").addEventListener("change", async (e) => {
  const file = e.target.files?.[0];
  if (!file) return;
  const [rows, cols] = $("grid").value.split("x").map(Number);
  const fd = new FormData();
  fd.append("file", file);

  busy(true, document.querySelector(".stage"));
  $("stats").innerHTML = "";
  $("snips").innerHTML = "";
  $("export-row").hidden = true;
  unlock("s3", true);

  try {
    const q = new URLSearchParams({ action: state.action, rows, cols, fps: $("fps").value });
    const r = await api(`/api/ingest?${q}`, { method: "POST", body: fd });
    if (!r.ok) {
      state.job = null;
      $("snips").innerHTML = `<div class="problems">
        <b>That sheet did not pass</b>
        <ul>${r.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>
        <p class="hint">Check the grid size matches what you generated.</p>
      </div>`;
      toast("Validation failed", true);
      return;
    }
    state.job = r.job;
    renderResult(r);
    toast(`${r.frames} frames at ${r.fps} fps`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false, document.querySelector(".stage"));
    e.target.value = "";
  }
});

// ── step 3: outputs ───────────────────────────────────────

$("btn-zip").addEventListener("click", () => {
  if (state.job) location.href = `/api/export/${state.job}`;
});

$("btn-sheet").addEventListener("click", () => {
  if (state.job) window.open(`/api/sheet/${state.job}`, "_blank");
});

$("btn-compare").addEventListener("click", () => {
  if (state.job) window.open(`/api/compare/${state.job}`, "_blank");
});

// ── boot ──────────────────────────────────────────────────

(async () => {
  try {
    await loadConfig();
    // Work from an earlier session is still on disk; pick it back up rather
    // than showing an empty page over the top of finished output.
    if (state.config.has_anchor) showAnchor(false);

    const { jobs } = await api("/api/jobs");
    for (const name of jobs.slice().reverse()) {
      try {
        const prev = await api(`/api/jobs/${name}`);
        if (prev.ok) {
          state.job = prev.job;
          state.action = prev.job;
          renderResult(prev);
          document
            .querySelectorAll(".motion")
            .forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.m === prev.job)));
          break;
        }
      } catch {
        /* unfinished job, keep looking */
      }
    }
  } catch (err) {
    toast(err.message, true);
  }
  reflect();
})();
