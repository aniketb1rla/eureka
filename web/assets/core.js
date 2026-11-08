// Shared helpers. Everything on screen is built with textContent and DOM nodes, never innerHTML,
// so text from users or from a model can't turn into markup. There is no secret anywhere in this code:
// the browser only ever talks to this app's own server, using an httpOnly session cookie.

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export async function api(path, { method = "GET", json, form } = {}) {
  const headers = { "X-Requested-With": "studio" };
  let body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (form) {
    body = form;
  }
  let response;
  try {
    response = await fetch(path, { method, headers, body, credentials: "same-origin" });
  } catch {
    throw new ApiError(0, "network", "Couldn't reach the studio. Check your connection and try again.");
  }
  let data = null;
  try { data = await response.json(); } catch { /* an empty or non-JSON reply is handled below */ }
  if (!response.ok) {
    throw new ApiError(response.status, data?.error?.code || "error", data?.error?.message || "Something went wrong. Please try again.");
  }
  return data;
}

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "vars") for (const [k, v] of Object.entries(value)) el.style.setProperty(k, v);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "value") el.value = value;
    else if (key === "checked" || key === "disabled" || key === "hidden" || key === "required" || key === "multiple" || key === "controls" || key === "muted" || key === "loop") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el, ...children) {
  el.replaceChildren(...children.flat().filter(Boolean));
  return el;
}

export function ratio(aspect) {
  const [w, h2] = String(aspect || "4:5").split(":").map(Number);
  return w && h2 ? w / h2 : 0.8;
}

export function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

export function costLine(left, word) {
  if (left <= 0) return `You have no ${word}s left`;
  return left === 1 ? `Uses your last ${word}` : `Uses 1 of your ${left} ${word}s left`;
}

export function when(iso) {
  const d = new Date(iso);
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)} h ago`;
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

// Polls one generation until it finishes. Stops as soon as `signal` says the page moved on.
export async function watchGeneration(id, signal, onUpdate) {
  let delay = 1500;
  for (;;) {
    if (signal.aborted) return null;
    let g;
    try {
      g = await api(`/api/generations/${id}`);
    } catch (err) {
      if (err.status === 401 || err.status === 404) throw err;
      g = null; // a blip while polling isn't a failed generation
    }
    if (g) {
      onUpdate?.(g);
      if (g.status === "succeeded" || g.status === "failed") return g;
    }
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * 1.25, 6000);
  }
}

export function segmented(options, value, onChange, label) {
  const root = h("div", { class: "segmented", role: "group", "aria-label": label });
  const buttons = options.map((opt) => h("button", {
    type: "button",
    "aria-pressed": String(opt.value === value),
    onclick: () => {
      buttons.forEach((b) => b.setAttribute("aria-pressed", "false"));
      buttons[options.indexOf(opt)].setAttribute("aria-pressed", "true");
      onChange(opt.value);
    },
  }, opt.label));
  root.append(...buttons);
  return root;
}

export function field(label, control, hint) {
  const id = control.id || `f${Math.random().toString(36).slice(2, 9)}`;
  control.id = id;
  return h("div", { class: "field" }, h("label", { for: id }, label), control, hint ? h("span", { class: "hint" }, hint) : null);
}

export function dropzone({ title, hint, small = false, accept = "image/jpeg,image/png,image/webp", onFile }) {
  const thumb = h("img", { class: "drop-thumb", alt: "", hidden: true });
  const name = h("span", { class: "filename" }, title);
  const sub = h("span", { class: "faint small" }, hint);
  let objectUrl = null;
  const input = h("input", {
    type: "file", accept, "aria-label": title,
    onchange: () => {
      const file = input.files[0] || null;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      if (file) {
        objectUrl = URL.createObjectURL(file);
        thumb.src = objectUrl;
        thumb.hidden = false;
        name.textContent = file.name;
        sub.textContent = "Tap to change";
      }
      onFile(file);
    },
  });
  const root = h("label", {
    class: `drop${small ? " small-drop" : ""}`,
    ondragover: (e) => { e.preventDefault(); root.classList.add("over"); },
    ondragleave: () => root.classList.remove("over"),
    ondrop: () => root.classList.remove("over"),
  }, thumb, h("span", { class: "drop-text" }, name, sub), input);
  return root;
}

export function developing(title, subtitle) {
  const elapsed = h("span", { class: "faint small" }, "0s");
  const started = Date.now();
  const timer = setInterval(() => {
    if (!elapsed.isConnected) return clearInterval(timer);
    const s = Math.round((Date.now() - started) / 1000);
    elapsed.textContent = s < 60 ? `${s}s` : `${Math.floor(s / 60)} min ${s % 60}s`;
  }, 1000);
  return h("div", { class: "develop", role: "status" },
    h("div", { class: "develop-bar" }, h("i")),
    h("strong", {}, title),
    h("span", { class: "muted small" }, subtitle),
    elapsed);
}

export function mediaElement(g, { controls = true, autoplay = false } = {}) {
  if (g.kind === "video") {
    // The #t offset makes browsers paint a first frame in the thumbnail.
    return h("video", { src: controls ? g.media_url : `${g.media_url}#t=0.1`, controls, playsinline: true, preload: "metadata", muted: !controls, loop: autoplay });
  }
  return h("img", { src: g.media_url, alt: g.title || "Generated image", loading: "lazy" });
}
