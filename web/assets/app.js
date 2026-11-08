import { ApiError, api, clear, costLine, developing, dropzone, field, h, mediaElement, plural, ratio, segmented, watchGeneration, when } from "./core.js";

const state = { config: null, templates: [], me: null, nav: null };
const bar = document.getElementById("bar");
const view = document.getElementById("view");
const lightbox = document.getElementById("lightbox");

// ---------- routing ----------

function go(path) {
  history.pushState({}, "", path);
  route();
}

function link(path, props, ...children) {
  return h("a", {
    href: path, ...props,
    onclick: (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey) return;
      e.preventDefault();
      go(path);
    },
  }, ...children);
}

function signIn() {
  go(`/login?next=${encodeURIComponent(location.pathname)}`);
}

async function route() {
  state.nav?.abort();
  state.nav = new AbortController();
  const signal = state.nav.signal;
  if (lightbox.open) lightbox.close();
  window.scrollTo(0, 0);
  const path = location.pathname;
  renderBar(path);
  const match = path.match(/^\/templates\/([a-z0-9-]+)$/);
  if (path === "/") renderTemplates();
  else if (match) renderTemplate(match[1], signal);
  else if (path === "/studio") renderStudio(signal);
  else if (path === "/library") renderLibrary(signal);
  else if (path === "/login") renderLogin();
  else clear(view, h("div", { class: "title-row" }, h("h1", {}, "That page doesn't exist")), link("/", { class: "btn" }, "Back to templates"));
}

addEventListener("popstate", route);

// ---------- top bar ----------

async function refreshMe() {
  try {
    state.me = await api("/api/me");
  } catch (err) {
    if (err.status !== 401) throw err;
    state.me = null;
  }
  renderBar(location.pathname);
}

function renderBar(path) {
  const current = (p) => (p === "/" ? path === "/" || path.startsWith("/templates") : path === p) ? "page" : null;
  const me = state.me;
  clear(bar,
    link("/", { class: "wordmark", "aria-label": "Eureka Studio home" }, h("img", { src: "/assets/mark.svg", alt: "" }), "Eureka Studio"),
    h("nav", { class: "nav", "aria-label": "Main" },
      link("/", { "aria-current": current("/") }, "Templates"),
      link("/studio", { "aria-current": current("/studio") }, "Studio"),
      link("/library", { "aria-current": current("/library") }, "Library")),
    h("div", { class: "bar-right" },
      state.config?.test_mode ? h("span", { class: "notice", title: "No model is called and nothing is billed" }, "Test mode: placeholder results") : null,
      me ? h("span", { class: "allowance" }, h("b", {}, plural(me.images_left, "image")), " and ", h("b", {}, plural(me.videos_left, "video")), " left") : null,
      me
        ? h("button", { class: "btn quiet", onclick: logout }, "Sign out")
        : path === "/login" ? null : link(`/login?next=${encodeURIComponent(path)}`, { class: "btn primary" }, "Sign in")));
}

async function logout() {
  await api("/auth/logout", { method: "POST" });
  state.me = null;
  go("/");
}

// ---------- templates ----------

function sketchOrSample(t) {
  return t.sample_url
    ? h("img", { src: t.sample_url, alt: `Sample ad in the ${t.name} style` })
    : h("img", { src: `/assets/sketches/${t.id}.svg`, alt: `Layout sketch of ${t.name}` });
}

function modelLabel(key) {
  return state.config?.image_models.find((m) => m.key === key)?.label || key;
}

function renderTemplates() {
  let filter = "all";
  const strip = h("div", { class: "strip" });
  const industries = [...new Map(state.templates.map((t) => [t.industry, t.industry_label])).entries()];

  const draw = () => {
    clear(strip, state.templates.filter((t) => filter === "all" || t.industry === filter).map((t, i) =>
      link(`/templates/${t.id}`, { class: "frame rise", vars: { "--ar": String(ratio(t.aspect)), "animation-delay": `${i * 60}ms` } },
        h("div", { class: "frame-media" }, sketchOrSample(t), t.sample_url ? null : h("span", { class: "sketch-note" }, "Layout sketch")),
        h("div", { class: "frame-name" }, t.name),
        h("div", { class: "frame-meta" }, `${t.industry_label} · ${t.aspect}`),
        h("p", { class: "frame-summary" }, t.summary))));
  };

  const filters = h("div", { class: "filters", role: "group", "aria-label": "Filter by industry" });
  const options = [["all", "All industries"], ...industries];
  const buttons = options.map(([value, label]) => h("button", {
    type: "button", "aria-pressed": String(value === filter),
    onclick: () => {
      filter = value;
      buttons.forEach((b, i) => b.setAttribute("aria-pressed", String(options[i][0] === value)));
      draw();
    },
  }, label));
  filters.append(...buttons);

  draw();
  clear(view,
    h("div", { class: "title-row" },
      h("div", {}, h("h1", {}, "Templates"), h("p", {}, "Pick a layout, add your product, and get a finished ad.")),
      filters),
    strip);
}

// ---------- make an ad from a template ----------

function renderTemplate(id, signal) {
  const t = state.templates.find((x) => x.id === id);
  if (!t) return clear(view, h("div", { class: "title-row" }, h("h1", {}, "That template doesn't exist")), link("/", { class: "btn" }, "Back to templates"));

  const files = { product: null, logo: null };
  const input = (name, placeholder, extra = {}) => h("input", { type: "text", name, placeholder, autocomplete: "off", ...extra });
  const brand = input("brand", "Northwind Coffee", { maxlength: 60 });
  const product = input("product", "Cold brew concentrate, 500 ml", { maxlength: 100 });
  const category = input("category", "Coffee", { maxlength: 60 });
  const colors = input("colors", "Forest green and cream", { maxlength: 60 });
  const headline = input("headline", "The line people read first", { maxlength: 80 });
  const subhead = input("subhead", "Optional", { maxlength: 160 });
  const cta = input("cta", "Optional, like Shop now", { maxlength: 30 });
  const formError = h("div", { class: "form-error", role: "alert", hidden: true });

  const showError = (message) => { formError.textContent = message; formError.hidden = !message; };
  const preview = h("div", { class: "make-preview" });
  const frame = h("div", { class: "result-frame", vars: { "--ar": String(ratio(t.aspect)) } }, sketchOrSample(t));
  const under = h("div", {});
  preview.append(frame, under);

  const copyBtn = h("button", {
    type: "button", class: "btn",
    onclick: async () => {
      showError("");
      if (!state.me) return signIn();
      if (!brand.value.trim() || !product.value.trim()) return showError("Add your brand and product first, so the copy can be about them.");
      copyBtn.disabled = true;
      copyBtn.textContent = "Writing...";
      try {
        const copy = await api("/api/copy", { method: "POST", json: { template_id: t.id, brand: brand.value, product: product.value, category: category.value } });
        headline.value = copy.headline;
        subhead.value = copy.subhead;
        cta.value = copy.cta;
      } catch (err) {
        showError(err.message);
      } finally {
        copyBtn.disabled = false;
        copyBtn.textContent = "Write copy for me";
      }
    },
  }, "Write copy for me");

  const generateBtn = h("button", { type: "submit", class: "btn primary large" }, "Generate image");
  const cost = h("span", { class: "muted small" }, state.me ? costLine(state.me.images_left, "image") : "Sign in to generate");

  const showResult = (g) => {
    clear(under);
    if (g.status === "failed") {
      clear(frame, sketchOrSample(t));
      clear(under, h("div", { class: "form-error", role: "alert" }, g.error));
      return;
    }
    clear(frame, h("img", { src: g.media_url, alt: g.title, class: "rise" }));
    const animateSlot = h("div", {});
    clear(under,
      h("div", { class: "result-actions" },
        h("a", { class: "btn primary", href: `${g.media_url}?download=1` }, "Download"),
        h("button", { class: "btn", type: "button", onclick: () => clear(animateSlot, animatePanel(g, signal, (video) => {
          clear(frame, h("video", { src: video.media_url, controls: true, playsinline: true, class: "rise" }));
        })) }, "Animate into video"),
        link("/library", { class: "btn quiet" }, "Open Library")),
      animateSlot);
  };

  const form = h("form", {
    class: "form", novalidate: true,
    onsubmit: async (e) => {
      e.preventDefault();
      showError("");
      if (!state.me) return signIn();
      if (!files.product) return showError("Add a photo of your product.");
      if (!brand.value.trim() || !product.value.trim()) return showError("Add your brand and product.");
      if (!headline.value.trim()) return showError("Add a headline, or use Write copy for me.");
      const data = new FormData();
      data.set("template_id", t.id);
      for (const el of [brand, product, category, headline, subhead, cta, colors]) data.set(el.name, el.value);
      data.set("product_photo", files.product);
      if (files.logo) data.set("logo", files.logo);
      generateBtn.disabled = true;
      generateBtn.textContent = "Starting...";
      clear(under);
      try {
        const { id: genId } = await api("/api/generations/template", { method: "POST", form: data });
        await refreshMe();
        cost.textContent = costLine(state.me.images_left, "image");
        clear(frame, developing("Making your ad", "This can take a minute. It'll also be saved in your Library."));
        window.scrollTo({ top: 0, behavior: "smooth" });
        const g = await watchGeneration(genId, signal);
        if (g) {
          showResult(g);
          await refreshMe();
          cost.textContent = costLine(state.me.images_left, "image");
        }
      } catch (err) {
        if (err.status === 401) return signIn();
        showError(err.message);
      } finally {
        generateBtn.disabled = false;
        generateBtn.textContent = "Generate image";
      }
    },
  },
    h("section", { class: "form-section" },
      h("h2", {}, "Your product"),
      dropzone({ title: "Add a product photo", hint: "JPG, PNG or WebP, up to 12 MB. A clear, well-lit shot works best.", onFile: (f) => { files.product = f; } }),
      h("div", { class: "form-row" }, field("Brand", brand), field("Product", product)),
      h("div", { class: "form-row" }, field("Category", category, "Helps fit the scene to what you sell."), field("Brand colours", colors, "Optional. Leave blank to keep this template's colours.")),
      h("div", { class: "field" }, h("span", { class: "field-label" }, "Logo"),
        dropzone({ title: "Add your logo", hint: "Optional. A transparent PNG works best.", small: true, onFile: (f) => { files.logo = f; } }))),
    h("section", { class: "form-section" },
      h("div", { class: "section-head" }, h("h2", {}, "The words on your ad"), copyBtn),
      field("Headline", headline),
      field("Supporting line", subhead),
      field("Button text", cta)),
    formError,
    h("div", { class: "make-actions" }, generateBtn, cost));

  clear(view,
    link("/", { class: "back" }, "Back to templates"),
    h("div", { class: "make" },
      preview,
      h("div", {},
        h("div", { class: "make-head" },
          h("h1", {}, t.name),
          h("p", {}, t.summary),
          h("p", { class: "faint small" }, `${t.works_for} ${t.aspect} · Made with ${modelLabel(t.model)}`)),
        form)));
}

function animatePanel(parent, signal, onDone) {
  const cfg = state.config;
  const options = { aspect: "9:16", duration: 8, resolution: "720p" };
  const prompt = h("textarea", { rows: 2, maxlength: 2000, placeholder: "Optional: describe the motion. Leave blank to use a move chosen for this design." });
  const error = h("div", { class: "form-error", role: "alert", hidden: true });
  const status = h("div", {});
  const left = state.me?.videos_left ?? 0;
  const start = h("button", {
    type: "button", class: "btn primary",
    onclick: async () => {
      error.hidden = true;
      start.disabled = true;
      try {
        const { id } = await api(`/api/generations/${parent.id}/animate`, { method: "POST", json: { ...options, prompt: prompt.value } });
        await refreshMe();
        clear(status, h("div", { class: "result-frame", vars: { "--ar": "1.8" } },
          developing("Making your video", "Videos can take several minutes. You can leave this page; it'll be in your Library.")));
        const g = await watchGeneration(id, signal);
        if (!g) return;
        await refreshMe();
        if (g.status === "failed") {
          clear(status);
          error.textContent = g.error;
          error.hidden = false;
        } else {
          clear(status);
          onDone(g);
        }
      } catch (err) {
        if (err.status === 401) return signIn();
        error.textContent = err.message;
        error.hidden = false;
      } finally {
        start.disabled = false;
      }
    },
  }, "Make video");
  return h("div", { class: "animate-panel" },
    h("h2", {}, "Animate this image"),
    prompt,
    h("div", { class: "control-row" },
      segmented(cfg.video_aspects.map((v) => ({ value: v, label: v })), options.aspect, (v) => { options.aspect = v; }, "Shape"),
      segmented(cfg.video_durations.map((v) => ({ value: v, label: `${v}s` })), options.duration, (v) => { options.duration = v; }, "Length"),
      segmented(cfg.video_resolutions.map((v) => ({ value: v, label: v })), options.resolution, (v) => { options.resolution = v; }, "Quality")),
    error, status,
    h("div", { class: "control-row" }, start, h("span", { class: "muted small" }, `${costLine(left, "video")} · ${cfg.video_model.label}`)));
}

// ---------- tiles and lightbox ----------

function tile(g, onChange) {
  const media = h("div", { class: "tile-media" });
  if (g.status === "succeeded") media.append(mediaElement(g, { controls: false }));
  else if (g.status === "failed") media.append(h("p", { class: "tile-failed" }, g.error || "This one didn't work."));
  else media.append(h("div", { class: "tile-working" }, h("i")), h("span", { class: "muted small" }, g.kind === "video" ? "Making video" : "Making image"));
  if (g.kind === "video" && g.status === "succeeded") media.append(h("span", { class: "pill corner" }, "Video"));
  if (g.status === "failed") media.append(h("span", { class: "pill failed corner" }, "Didn't work"));
  return h("button", { type: "button", class: "tile", onclick: () => openLightbox(g, onChange), "aria-label": `Open ${g.title || "generation"}` },
    media,
    h("div", { class: "tile-title" }, g.title || g.template_name || "Untitled"),
    h("div", { class: "tile-meta" }, `${g.model_label} · ${when(g.created_at)}`));
}

function openLightbox(g, onChange) {
  const error = h("div", { class: "form-error", role: "alert", hidden: true });
  const fail = (err) => {
    if (err.status === 401) { lightbox.close(); return signIn(); }
    error.textContent = err.message;
    error.hidden = false;
  };
  const animateSlot = h("div", {});
  let deleteArmed = false;
  const deleteBtn = h("button", { class: "btn danger", type: "button", onclick: async () => {
    // Deleting can't be undone, so it takes a second, deliberate click.
    if (!deleteArmed) {
      deleteArmed = true;
      deleteBtn.textContent = "Confirm delete";
      return;
    }
    try {
      await api(`/api/generations/${g.id}`, { method: "DELETE" });
      lightbox.close();
      onChange?.();
    } catch (err) { fail(err); }
  } }, "Delete");
  const controller = new AbortController();
  lightbox.addEventListener("close", () => controller.abort(), { once: true });
  const media = h("div", { class: "lightbox-media" });
  if (g.status === "succeeded") media.append(g.kind === "video" ? h("video", { src: g.media_url, controls: true, playsinline: true }) : h("img", { src: g.media_url, alt: g.title }));
  else media.append(h("p", { class: "muted" }, g.status === "failed" ? "Nothing was made." : "Still working on this one."));

  clear(lightbox, h("div", { class: "lightbox-grid" },
    media,
    h("div", { class: "lightbox-info" },
      h("h2", {}, g.title || g.template_name || "Untitled"),
      g.error ? h("div", { class: "form-error" }, g.error) : null,
      h("dl", { class: "facts" },
        h("dt", {}, "Model"), h("dd", {}, g.model_label),
        g.template_name ? [h("dt", {}, "Template"), h("dd", {}, g.template_name)] : null,
        g.aspect ? [h("dt", {}, "Shape"), h("dd", {}, g.aspect)] : null,
        h("dt", {}, "Made"), h("dd", {}, when(g.created_at)),
        g.user_prompt ? [h("dt", {}, "Prompt"), h("dd", {}, g.user_prompt)] : null),
      h("div", { class: "lightbox-actions" },
        g.status === "succeeded" ? h("a", { class: "btn primary", href: `${g.media_url}?download=1` }, "Download") : null,
        g.status === "succeeded" && g.kind === "image" ? h("button", { class: "btn", type: "button", onclick: () => clear(animateSlot, animatePanel(g, controller.signal, (video) => {
          clear(media, h("video", { src: video.media_url, controls: true, playsinline: true }));
          onChange?.();
        })) }, "Animate into video") : null,
        g.status === "succeeded" || g.status === "failed" ? h("button", { class: "btn", type: "button", onclick: async () => {
          try {
            await api(`/api/generations/${g.id}/rerun`, { method: "POST" });
            await refreshMe();
            lightbox.close();
            onChange?.();
          } catch (err) { fail(err); }
        } }, "Run again") : null,
        g.status === "succeeded" || g.status === "failed" ? deleteBtn : null,
        h("button", { class: "btn quiet", type: "button", onclick: () => lightbox.close() }, "Close")),
      error,
      animateSlot)));
  lightbox.showModal();
}

lightbox.addEventListener("click", (e) => { if (e.target === lightbox) lightbox.close(); });

function needSignIn(title, line) {
  clear(view, h("div", { class: "title-row" }, h("h1", {}, title)),
    h("div", { class: "empty" }, h("p", {}, line), link(`/login?next=${encodeURIComponent(location.pathname)}`, { class: "btn primary" }, "Sign in")));
}

// Watches every unfinished item and redraws when one completes.
function watchActive(items, signal, onSettled) {
  for (const g of items.filter((x) => x.status === "queued" || x.status === "running")) {
    watchGeneration(g.id, signal).then((done) => { if (done) { refreshMe(); onSettled(); } }).catch(() => {});
  }
}

// ---------- library ----------

async function renderLibrary(signal) {
  if (!state.me) return needSignIn("Library", "Sign in to see everything you've made.");
  let filter = "all";
  let items = [];
  const sheet = h("div", { class: "sheet" });
  const body = h("div", {});
  const more = h("div", { class: "load-more" });

  const draw = () => {
    const shown = items.filter((g) => filter === "all" || g.kind === filter);
    if (!shown.length) {
      clear(body, h("div", { class: "empty" },
        h("p", {}, items.length ? `No ${filter}s yet.` : "Nothing here yet. Start from a template, and every ad you make is saved here."),
        link("/", { class: "btn primary" }, "Browse templates")));
    } else {
      clear(sheet, shown.map((g) => tile(g, load)));
      clear(body, sheet);
    }
  };

  async function load() {
    if (signal.aborted) return;
    try {
      const data = await api("/api/generations?limit=30");
      items = data.generations;
      clear(more, items.length === 30 ? h("button", { class: "btn", type: "button", onclick: loadMore }, "Show more") : null);
      draw();
      watchActive(items, signal, load);
    } catch (err) {
      if (err.status === 401) return signIn();
      clear(body, h("div", { class: "form-error", role: "alert" }, err.message));
    }
  }

  async function loadMore() {
    const data = await api(`/api/generations?limit=30&before=${encodeURIComponent(items[items.length - 1].id)}`);
    items = items.concat(data.generations);
    if (data.generations.length < 30) clear(more);
    draw();
  }

  clear(view,
    h("div", { class: "title-row" },
      h("div", {}, h("h1", {}, "Library"), h("p", {}, "Everything you've made, newest first.")),
      segmented([{ value: "all", label: "All" }, { value: "image", label: "Images" }, { value: "video", label: "Videos" }], filter, (v) => { filter = v; draw(); }, "Filter by type")),
    body, more);
  await load();
}

// ---------- studio ----------

function renderStudio(signal) {
  if (!state.me) return needSignIn("Studio", "Sign in to generate from your own prompt with Nano Banana or Omni Flash.");
  const cfg = state.config;
  const opts = { kind: "image", model: "nano-banana-2.1", aspect: "4:5", resolution: "1K", duration: 8 };
  const refs = [];
  const textarea = h("textarea", { rows: 3, maxlength: 2000, placeholder: "Describe the image you want to make", "aria-label": "Prompt" });
  const controls = h("div", { class: "composer-row" });
  const refRow = h("div", { class: "ref-row" });
  const error = h("div", { class: "form-error", role: "alert", hidden: true });
  const cost = h("span", { class: "muted small" });
  const send = h("button", { type: "button", class: "btn primary large" }, "Generate");
  const sheet = h("div", { class: "sheet" });
  const canvas = h("div", {});

  const select = (label, values, value, onChange) => h("select", { "aria-label": label, onchange: (e) => onChange(e.target.value) },
    values.map((v) => h("option", { value: v.value, selected: v.value === value }, v.label)));

  function maxRefs() {
    return opts.kind === "video" ? 6 : cfg.image_models.find((m) => m.key === opts.model).max_refs;
  }

  function drawCost() {
    const me = state.me;
    cost.textContent = opts.kind === "image" ? costLine(me.images_left, "image") : costLine(me.videos_left, "video");
  }

  function drawRefs() {
    const add = h("span", { class: "ref-add" },
      h("span", { class: "btn" }, refs.length ? "Add another" : "Add reference images"),
      h("input", {
        type: "file", accept: "image/jpeg,image/png,image/webp", multiple: true, "aria-label": "Add reference images",
        onchange: (e) => {
          for (const file of e.target.files) if (refs.length < maxRefs()) refs.push({ file, url: URL.createObjectURL(file) });
          drawRefs();
        },
      }));
    clear(refRow,
      refs.map((r, i) => h("div", { class: "ref-thumb" }, h("img", { src: r.url, alt: `Reference ${i + 1}` }),
        h("button", { type: "button", "aria-label": `Remove reference ${i + 1}`, onclick: () => { URL.revokeObjectURL(r.url); refs.splice(i, 1); drawRefs(); } }, "×"))),
      refs.length < maxRefs() ? add : h("span", { class: "faint small" }, `Up to ${maxRefs()} references`));
  }

  function drawControls() {
    textarea.placeholder = opts.kind === "image" ? "Describe the image you want to make" : "Describe the video you want to make";
    const list = [segmented([{ value: "image", label: "Image" }, { value: "video", label: "Video" }], opts.kind, (v) => {
      opts.kind = v;
      opts.aspect = v === "video" ? "9:16" : "4:5";
      opts.resolution = v === "video" ? "720p" : "1K";
      drawControls();
    }, "Type")];
    if (opts.kind === "image") {
      list.push(
        select("Model", cfg.image_models.map((m) => ({ value: m.key, label: m.label })), opts.model, (v) => { opts.model = v; while (refs.length > maxRefs()) refs.pop(); drawRefs(); }),
        select("Shape", cfg.image_aspects.map((v) => ({ value: v, label: v })), opts.aspect, (v) => { opts.aspect = v; }),
        select("Quality", cfg.image_resolutions.map((v) => ({ value: v, label: v })), opts.resolution, (v) => { opts.resolution = v; }));
    } else {
      list.push(
        h("span", { class: "muted" }, cfg.video_model.label),
        select("Shape", cfg.video_aspects.map((v) => ({ value: v, label: v })), opts.aspect, (v) => { opts.aspect = v; }),
        select("Length", cfg.video_durations.map((v) => ({ value: String(v), label: `${v} seconds` })), String(opts.duration), (v) => { opts.duration = Number(v); }),
        select("Quality", cfg.video_resolutions.map((v) => ({ value: v, label: v })), opts.resolution, (v) => { opts.resolution = v; }));
    }
    list.push(h("div", { class: "composer-send" }, cost, send));
    clear(controls, list);
    drawCost();
    drawRefs();
  }

  async function load() {
    if (signal.aborted) return;
    try {
      const data = await api("/api/generations?limit=24");
      const items = data.generations.filter((g) => g.source === "free");
      if (items.length) {
        clear(sheet, items.map((g) => tile(g, load)));
        clear(canvas, sheet);
      } else {
        clear(canvas, h("div", { class: "empty" }, h("p", {}, "What you make here appears below your prompt. To start from a proven layout instead, use a template."),
          link("/", { class: "btn" }, "Browse templates")));
      }
      watchActive(items, signal, load);
    } catch (err) {
      if (err.status === 401) return signIn();
      clear(canvas, h("div", { class: "form-error", role: "alert" }, err.message));
    }
  }

  send.addEventListener("click", async () => {
    error.hidden = true;
    if (textarea.value.trim().length < 3) {
      error.textContent = `Describe the ${opts.kind} you want to make.`;
      error.hidden = false;
      return;
    }
    const form = new FormData();
    form.set("kind", opts.kind);
    form.set("prompt", textarea.value);
    form.set("aspect", opts.aspect);
    form.set("resolution", opts.resolution);
    if (opts.kind === "image") form.set("model", opts.model);
    else form.set("duration", String(opts.duration));
    for (const r of refs) form.append("references", r.file);
    send.disabled = true;
    try {
      await api("/api/generations/free", { method: "POST", form });
      textarea.value = "";
      refs.splice(0).forEach((r) => URL.revokeObjectURL(r.url));
      drawRefs();
      await refreshMe();
      drawCost();
      await load();
    } catch (err) {
      if (err.status === 401) return signIn();
      error.textContent = err.message;
      error.hidden = false;
    } finally {
      send.disabled = false;
    }
  });

  drawControls();
  clear(view,
    h("div", { class: "studio" },
      h("h1", {}, "Studio"),
      h("div", { class: "composer" }, textarea, refRow, error, controls),
      canvas));
  load();
}

// ---------- sign-in ----------

function loadTurnstile(slot, onToken) {
  const key = state.config.turnstile_site_key;
  if (!key) return;
  const render = () => window.turnstile.render(slot, { sitekey: key, theme: "dark", callback: onToken });
  if (window.turnstile) return render();
  document.head.append(h("script", { src: "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit", onload: render }));
}

function nextPath() {
  const next = new URLSearchParams(location.search).get("next") || "/";
  // Only ever follow a path on this site.
  return /^\/(?!\/)/.test(next) ? next : "/";
}

function renderLogin() {
  if (state.me) return go(nextPath());
  if (state.config.auth === "dev") return renderDevLogin();
  let mode = "signin";
  let captcha = "";
  const confirmed = new URLSearchParams(location.search).get("confirmed");
  const email = h("input", { type: "email", name: "email", autocomplete: "email", required: true });
  const password = h("input", { type: "password", name: "password", required: true, minlength: 10 });
  const error = h("div", { class: "form-error", role: "alert", hidden: true });
  const ok = h("div", { class: "form-ok", role: "status", hidden: !confirmed }, "Your email is confirmed. Sign in to continue.");
  const heading = h("h1", {});
  const submit = h("button", { type: "submit", class: "btn primary full" });
  const passwordField = field("Password", password);
  const captchaSlot = h("div", {});
  const switchLine = h("p", { class: "auth-switch" });

  const draw = () => {
    heading.textContent = mode === "signin" ? "Sign in" : "Create your account";
    submit.textContent = mode === "signin" ? "Sign in" : "Create account";
    password.autocomplete = mode === "signin" ? "current-password" : "new-password";
    clear(switchLine,
      mode === "signin" ? "New here? " : "Already have an account? ",
      h("button", { type: "button", onclick: () => { mode = mode === "signin" ? "signup" : "signin"; error.hidden = true; draw(); } }, mode === "signin" ? "Create an account" : "Sign in"));
    captchaSlot.hidden = mode !== "signup";
    if (mode === "signup" && !captchaSlot.childElementCount) loadTurnstile(captchaSlot, (t) => { captcha = t; });
  };

  const form = h("form", {
    class: "form", novalidate: true,
    onsubmit: async (e) => {
      e.preventDefault();
      error.hidden = true;
      ok.hidden = true;
      submit.disabled = true;
      try {
        if (mode === "signin") {
          await api("/auth/login", { method: "POST", json: { email: email.value, password: password.value } });
          password.value = "";
          await refreshMe();
          go(nextPath());
        } else {
          await api("/auth/signup", { method: "POST", json: { email: email.value, password: password.value, captcha } });
          password.value = "";
          ok.textContent = "Check your email for a link to confirm your account. Your free allowance starts once it's confirmed.";
          ok.hidden = false;
        }
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
      } finally {
        submit.disabled = false;
      }
    },
  }, field("Email", email), passwordField, captchaSlot, error, ok, submit);

  draw();
  clear(view, h("div", { class: "auth" }, heading,
    h("a", { class: "btn full", href: "/auth/google" }, "Continue with Google"),
    form, switchLine));
}

function renderDevLogin() {
  const email = h("input", { type: "email", value: "owner@eureka.test", autocomplete: "off" });
  const error = h("div", { class: "form-error", role: "alert", hidden: true });
  clear(view, h("div", { class: "auth" },
    h("h1", {}, "Sign in"),
    h("p", { class: "muted" }, "Local test sign-in. It only works on this computer and doesn't exist on the live site."),
    h("form", {
      class: "form", novalidate: true,
      onsubmit: async (e) => {
        e.preventDefault();
        try {
          await api("/auth/dev-login", { method: "POST", json: { email: email.value } });
          await refreshMe();
          go(nextPath());
        } catch (err) {
          error.textContent = err.message;
          error.hidden = false;
        }
      },
    }, field("Email", email), error, h("button", { type: "submit", class: "btn primary full" }, "Continue"))));
}

// ---------- start ----------

(async function start() {
  try {
    const [config, templates] = await Promise.all([api("/api/config"), api("/api/templates")]);
    state.config = config;
    state.templates = templates.templates;
    await refreshMe();
    await route();
  } catch (err) {
    clear(view, h("div", { class: "title-row" }, h("h1", {}, "The studio couldn't load")),
      h("p", { class: "muted" }, err instanceof ApiError ? err.message : "Please refresh the page."));
  }
})();
