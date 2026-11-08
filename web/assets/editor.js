// Area editor: paint over part of an image, say what should change there, and Nano Banana 2.1 changes only that part.
// The painted layer is sent as a plain black and white mask; the server re-checks it and keeps everything outside it.
import { api, clear, costLine, developing, h, segmented, watchGeneration } from "./core.js";

const MAX_MASK_SIDE = 2048;
const UNDO_LIMIT = 20;

// source: { id, url, title } for one of your images, or { file, url } for a photo you've chosen.
export function openEditor(dialog, source, ctx) {
  const controller = new AbortController();
  dialog.addEventListener("close", () => controller.abort(), { once: true });

  let tool = "brush";
  let size = 40; // on-screen pixels
  let painted = false;
  const undo = [];

  const img = h("img", { src: source.url, alt: source.title || "Image to edit", draggable: "false" });
  const canvas = h("canvas", { "aria-label": "Paint over the area to change" });
  const cursor = h("div", { class: "brush-cursor", hidden: true });
  const wrap = h("div", { class: "editor-canvas-wrap" }, img, canvas, cursor);
  const stage = h("div", { class: "editor-stage" }, wrap);
  const pen = canvas.getContext("2d");

  const prompt = h("textarea", { rows: 3, maxlength: 1000, placeholder: "For example: make the mug matte black", "aria-label": "What should change there?" });
  const error = h("div", { class: "form-error", role: "alert", hidden: true });
  const cost = h("span", { class: "muted small" });
  const generate = h("button", { type: "button", class: "btn primary" }, "Generate edit");
  const undoBtn = h("button", { type: "button", class: "btn", disabled: true }, "Undo");
  const clearBtn = h("button", { type: "button", class: "btn quiet", disabled: true }, "Clear");
  const sizeInput = h("input", { type: "range", min: "8", max: "140", value: String(size), "aria-label": "Brush size" });
  const panel = h("div", { class: "editor-panel" });

  const showError = (message) => { error.textContent = message; error.hidden = !message; };
  const drawCost = () => { cost.textContent = ctx.me() ? `${costLine(ctx.me().images_left, "image")} · Nano Banana 2.1` : ""; };

  // ---------- painting ----------

  img.addEventListener("load", () => {
    const scale = Math.min(1, MAX_MASK_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
    canvas.width = Math.round(img.naturalWidth * scale);
    canvas.height = Math.round(img.naturalHeight * scale);
  }, { once: true });

  const toCanvas = (e) => {
    const box = canvas.getBoundingClientRect();
    return { x: (e.clientX - box.left) * (canvas.width / box.width), y: (e.clientY - box.top) * (canvas.height / box.height), box };
  };

  const moveCursor = (e) => {
    const box = wrap.getBoundingClientRect();
    cursor.hidden = false;
    cursor.style.setProperty("--x", `${e.clientX - box.left}px`);
    cursor.style.setProperty("--y", `${e.clientY - box.top}px`);
    cursor.style.setProperty("--d", `${size}px`);
  };

  const syncButtons = () => {
    undoBtn.disabled = !undo.length;
    clearBtn.disabled = !painted;
  };

  const hasPaint = () => {
    const data = pen.getImageData(0, 0, canvas.width, canvas.height).data;
    for (let i = 3; i < data.length; i += 4) if (data[i]) return true;
    return false;
  };

  let last = null;
  canvas.addEventListener("pointerdown", (e) => {
    if (!canvas.width) return;
    e.preventDefault();
    showError("");
    canvas.setPointerCapture(e.pointerId);
    undo.push(pen.getImageData(0, 0, canvas.width, canvas.height));
    if (undo.length > UNDO_LIMIT) undo.shift();
    last = toCanvas(e);
    stroke(last, last);
  });
  canvas.addEventListener("pointermove", (e) => {
    moveCursor(e);
    if (!last) return;
    const point = toCanvas(e);
    stroke(last, point);
    last = point;
  });
  const end = () => {
    if (!last) return;
    last = null;
    painted = hasPaint();
    syncButtons();
  };
  canvas.addEventListener("pointerup", end);
  canvas.addEventListener("pointercancel", end);
  canvas.addEventListener("pointerleave", () => { cursor.hidden = true; });

  function stroke(from, to) {
    pen.globalCompositeOperation = tool === "brush" ? "source-over" : "destination-out";
    pen.strokeStyle = "#ff5c39";
    pen.lineCap = "round";
    pen.lineJoin = "round";
    pen.lineWidth = size * (canvas.width / from.box.width);
    pen.beginPath();
    pen.moveTo(from.x, from.y);
    pen.lineTo(to.x + 0.01, to.y);
    pen.stroke();
  }

  undoBtn.addEventListener("click", () => {
    const previous = undo.pop();
    if (previous) pen.putImageData(previous, 0, 0);
    painted = hasPaint();
    syncButtons();
  });
  clearBtn.addEventListener("click", () => {
    undo.push(pen.getImageData(0, 0, canvas.width, canvas.height));
    pen.clearRect(0, 0, canvas.width, canvas.height);
    painted = false;
    syncButtons();
  });
  sizeInput.addEventListener("input", () => { size = Number(sizeInput.value); });

  const onKey = (e) => {
    if (e.target === prompt) return;
    if (e.key === "[") sizeInput.value = String(size = Math.max(8, size - 8));
    else if (e.key === "]") sizeInput.value = String(size = Math.min(140, size + 8));
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); undoBtn.click(); }
  };
  dialog.addEventListener("keydown", onKey, { signal: controller.signal });

  // White where painted, black everywhere else.
  const maskBlob = () => new Promise((resolve) => {
    const out = h("canvas");
    out.width = canvas.width;
    out.height = canvas.height;
    const c = out.getContext("2d");
    c.drawImage(canvas, 0, 0);
    c.globalCompositeOperation = "source-in";
    c.fillStyle = "#fff";
    c.fillRect(0, 0, out.width, out.height);
    c.globalCompositeOperation = "destination-over";
    c.fillStyle = "#000";
    c.fillRect(0, 0, out.width, out.height);
    out.toBlob(resolve, "image/png");
  });

  // ---------- sending ----------

  generate.addEventListener("click", async () => {
    showError("");
    if (!ctx.me()) return ctx.signIn();
    if (!painted) return showError("Paint over the area you want to change first.");
    if (prompt.value.trim().length < 3) return showError("Say what should change in the highlighted area.");
    const form = new FormData();
    form.set("prompt", prompt.value);
    form.set("mask", await maskBlob(), "mask.png");
    if (source.id) form.set("source_id", source.id);
    else form.set("image", source.file);
    generate.disabled = true;
    try {
      const { id } = await api("/api/generations/edit", { method: "POST", form });
      await ctx.refreshMe();
      drawCost();
      canvas.hidden = true;
      cursor.hidden = true;
      const working = developing("Editing the highlighted area", "This usually takes under a minute. It'll also be saved in your Library.");
      stage.append(working);
      const g = await watchGeneration(id, controller.signal);
      working.remove();
      if (!g) return;
      await ctx.refreshMe();
      ctx.onChange?.();
      if (g.status === "failed") {
        canvas.hidden = false;
        showError(g.error);
        drawCost();
        return;
      }
      showResult(g);
    } catch (err) {
      if (err.status === 401) { dialog.close(); return ctx.signIn(); }
      canvas.hidden = false;
      showError(err.message);
    } finally {
      generate.disabled = false;
    }
  });

  // ---------- result ----------

  function showResult(g) {
    const before = img.src;
    img.src = g.media_url;
    img.alt = g.title;
    // Hold to see the original; let go to see the edit.
    const compare = h("button", { type: "button", class: "btn" }, "Hold to compare");
    const show = (url) => () => { img.src = url; };
    compare.addEventListener("pointerdown", show(before));
    for (const name of ["pointerup", "pointerleave", "pointercancel"]) compare.addEventListener(name, show(g.media_url));
    compare.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") img.src = before; });
    compare.addEventListener("keyup", () => { img.src = g.media_url; });
    clear(panel,
      h("h2", {}, "Edited"),
      h("p", { class: "muted" }, "Only the highlighted area was changed. The edit is saved in your Library."),
      h("dl", { class: "facts" }, h("dt", {}, "Change"), h("dd", {}, g.user_prompt), h("dt", {}, "Model"), h("dd", {}, g.model_label)),
      h("div", { class: "lightbox-actions" },
        h("a", { class: "btn primary", href: `${g.media_url}?download=1` }, "Download"),
        compare,
        h("button", { type: "button", class: "btn", onclick: () => openEditor(dialog, { id: g.id, url: g.media_url, title: g.title }, ctx) }, "Edit again"),
        h("button", { type: "button", class: "btn quiet", onclick: () => dialog.close() }, "Close")));
  }

  // ---------- layout ----------

  drawCost();
  clear(panel,
    h("h2", {}, "Edit an area"),
    h("p", { class: "muted" }, "Paint over the part you want to change, then say what should change there. Everything else stays as it is."),
    h("div", { class: "editor-tools" },
      segmented([{ value: "brush", label: "Brush" }, { value: "eraser", label: "Eraser" }], tool, (v) => { tool = v; }, "Tool"),
      h("label", { class: "range" }, "Size", sizeInput),
      undoBtn, clearBtn),
    h("label", { class: "field-label", for: "edit-prompt" }, "What should change there?"),
    prompt,
    error,
    h("div", { class: "control-row" }, generate, cost),
    h("button", { type: "button", class: "btn quiet start", onclick: () => dialog.close() }, "Cancel"));
  prompt.id = "edit-prompt";
  clear(dialog, h("div", { class: "editor" }, stage, panel));
  if (!dialog.open) dialog.showModal();
}
