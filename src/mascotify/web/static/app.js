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

  const needsKey = (c.needs_key || []).includes(c.provider);
  const cost = c.cost[c.provider] ?? 0;
  $("cost-hint").textContent = !needsKey
    ? c.keyed[c.provider]
      ? "Drawn by your coding agent — no API key, nothing charged. Slower than a provider: a grid can take a couple of minutes."
      : "No coding agent found on PATH. Install and sign in to codex, or pick a provider with a key in settings."
    : c.keyed[c.provider]
      ? `Each generation calls ${c.provider} on your key — roughly $${cost.toFixed(2)} an image.`
      : `Add a ${c.provider} key in settings before generating.`;

  // From the server, not typed into the markup. Two numbers describing how
  // many things the tool supports are exactly the kind that go quietly stale
  // the first time one is added.
  const fact = (id, n) => { const e = $(id); if (e) e.textContent = n; };
  fact("fact-motions", Object.keys(c.motions).length);
  fact("fact-targets", (c.targets || []).length);

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
  resetResult();
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

/** Clear the previous run before starting another.

    The preview image and its caption used to survive a reset, so a run that
    failed validation showed the *last* successful mascot and its frame count
    sitting directly above "that sheet did not pass" — the numbers on screen
    describing a different generation than the error did. */
function resetResult() {
  $("stats").innerHTML = "";
  $("snips").innerHTML = "";
  $("export-row").hidden = true;
  $("preview-img").hidden = true;
  $("preview-empty").hidden = false;
  $("preview-cap").textContent = "";
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
  resetResult();
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

/* ── the cast ───────────────────────────────────────────────

   Every character on the masthead is a live instance of what
   `mascotify pose-ingest` exports: two 3x3 sheets, one sampled by the cursor's
   angle and one by a click. They are the files the pipeline wrote.

   Kept to `background-position` on a 300% sheet rather than nine <img> tags per
   character, which is what lets a dozen of them turn at once — every pose is
   already decoded, so a turn is one style write and no network. */
function wireCast() {
  const nodes = [...document.querySelectorAll("[data-directions]:not([data-wired])")];
  if (!nodes.length) return;
  for (const n of nodes) n.dataset.wired = "1";

  const REACT_MS = 620;
  // How far the cursor has to be before the gaze is fully committed, in
  // character widths. A fixed pixel reach was tuned against one 300px mascot
  // and broke as soon as there were six: clustered 140px apart, every offset
  // that distinguishes them fell inside the dead zone, so the whole row stared
  // straight ahead at a cursor plainly off to one side. Measuring in the
  // character's own width keeps a big solo mascot and a small crowd both
  // committing at the distance that looks right for their size.
  const REACH = 2.1;
  const IDLE_AFTER = 2400;         // stillness before they start looking around
  const fine = matchMedia("(pointer: fine)").matches;
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;

  let lastMove = 0;
  const jitter = (base, spread) => base + Math.random() * spread;

  function make(el) {
    const sheets = { dir: el.dataset.directions, rea: el.dataset.reactions };
    // Which reactions cell to borrow for a blink. Told by the server rather
    // than hardcoded, since that sheet's cell order is free — though the
    // larger uncertainty is what the model actually drew there, which no
    // index can protect against. See BLINK_POSE.
    const blinkCell = Number(el.dataset.blink ?? 4);
    el.style.backgroundImage = `url("${sheets.dir}")`;
    let cell = 4, reacting = 0;

    const at = (i) => {
      cell = i;
      el.style.backgroundPosition = `${(i % 3) * 50}% ${((i / 3) | 0) * 50}%`;
    };
    const sheet = (which) => {
      el.style.backgroundImage = `url("${sheets[which]}")`;
    };
    at(4);

    const track = (e) => {
      if (reacting) return;
      const b = el.getBoundingClientRect();
      // A dead zone in the middle band: without it a head twitches between
      // neighbouring cells whenever the cursor sits near a boundary.
      const z = (v) => (v < -0.34 ? 0 : v > 0.34 ? 2 : 1);
      const rx = b.width * REACH, ry = b.height * REACH;
      const nx = z(Math.max(-1, Math.min(1, (e.clientX - (b.left + b.width / 2)) / rx)));
      const ny = z(Math.max(-1, Math.min(1, (e.clientY - (b.top + b.height / 2)) / ry)));
      at(ny * 3 + nx);
    };

    /* Idle. A character that only moves when moved at is a control, not a
       cast — it sits dead until the pointer happens to cross it, which on a
       page you arrived at and have not touched is most of the time. Each one
       keeps its own jittered timers, so they glance around independently
       rather than turning in unison like a chorus line. */
    const wander = () => {
      if (!reacting && Date.now() - lastMove > IDLE_AFTER) {
        const pick = [0, 1, 2, 3, 4, 4, 4, 5, 6, 7, 8];
        at(pick[(Math.random() * pick.length) | 0]);
      }
      setTimeout(wander, jitter(1500, 2600));
    };

    /* Blink. The reactions sheet has no blink of its own — its nine cells are
       the exported contract — so an idle character borrows the half-lidded one
       for 120ms. Cheap, and close enough for a character this size; it is not
       a real blink, and on art where that pose came back carrying Zzz it reads
       as a flicker rather than an eyelid. */
    let blinking = 0;
    const blink = () => {
      if (!reacting) {
        const back = cell;
        sheet("rea");
        at(blinkCell);
        // Guarded on the way out: a click landing inside these 120ms used to
        // have its reaction overwritten by this restore, leaving the mascot
        // stuck on a stale direction cell until the click's own timer fired.
        blinking = setTimeout(() => {
          blinking = 0;
          if (reacting) return;
          sheet("dir");
          at(back);
        }, 120);
      }
      setTimeout(blink, jitter(3400, 5000));
    };

    el.addEventListener("click", () => {
      clearTimeout(reacting);
      clearTimeout(blinking);
      blinking = 0;
      sheet("rea");
      at(Math.floor(Math.random() * 9));
      if (!still) {
        el.animate(
          [{ transform: "scale(1)" }, { transform: "scale(.9, 1.08)" }, { transform: "scale(1)" }],
          { duration: 340, easing: "cubic-bezier(.34,1.56,.64,1)" },
        );
      }
      reacting = setTimeout(() => {
        sheet("dir");
        reacting = 0;
        lastMove = Date.now();
        at(4);
      }, REACT_MS);
    });

    if (!still) {
      setTimeout(wander, jitter(IDLE_AFTER, 1200));
      setTimeout(blink, jitter(1500, 4000));
    }
    return { track };
  }

  const cast = nodes.map(make);

  // One listener for the whole cast rather than one each: a dozen handlers on
  // pointermove is a dozen layout reads per mouse move.
  if (fine) {
    addEventListener("pointermove", (e) => {
      lastMove = Date.now();
      for (const m of cast) m.track(e);
    }, { passive: true });
  }

  const cap = document.getElementById("demo-cap");
  if (cap) cap.textContent = fine ? "move your cursor — then poke one" : "tap one";
}

// The cast is fetched, so the markup does not exist when this file runs. Wire
// on the event the loader fires, and once now in case it is ever inlined.
addEventListener("cast-ready", wireCast);
wireCast();

/* ── loading the cast ───────────────────────────────────────

   The characters come from the server's own pose jobs, so the masthead fills
   with the mascots this project made. A fresh project has none, and falls back
   to the one bundled with the package — a page with one character rather than
   an empty hole. */
(async () => {
  const host = document.getElementById("cast");
  if (!host) return;

  const BUNDLED = [{ id: "", alt: "A teal and cream robot that watches your cursor",
                     dir: "/mascot-directions.webp", rea: "/mascot-reactions.webp",
                     blink: 4 }];
  let list = BUNDLED;
  try {
    const found = await (await fetch("/api/cast")).json();
    if (found.length) {
      list = found.map((c) => ({
        ...c, dir: `/api/cast/${c.id}/directions`, rea: `/api/cast/${c.id}/reactions`,
      }));
    }
  } catch { /* the bundled one is a fine page on its own */ }

  host.classList.toggle("solo", list.length === 1);
  host.innerHTML = list
    .map(
      (c, i) => `<figure>
        <div class="bob" style="animation-delay:${(-i * 0.7).toFixed(2)}s">
          <div class="demo-mascot" role="img" aria-label="${c.alt.replace(/"/g, "&quot;")}"
               data-directions="${c.dir}" data-reactions="${c.rea}"
               data-blink="${c.blink ?? 4}"></div>
        </div></figure>`,
    )
    .join("");
  dispatchEvent(new CustomEvent("cast-ready"));
})();

/* ── theme ──────────────────────────────────────────────────

   The attribute is already set by the inline script in <head>; this only
   handles the click. The choice is remembered, and until one is made the
   system preference wins. */
(() => {
  const btn = document.getElementById("toggle-theme");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("mascotify-theme", next);
  });
})();
