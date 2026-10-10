/* SnapTik front end: no framework, no build step. */
(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const SVG_NS = "http://www.w3.org/2000/svg";
  const ICONS = { video: "i-video", video_sd: "i-video", video_wm: "i-video", audio: "i-music", cover: "i-image" };

  const form = $("#dl-form");
  const input = $("#url");
  const goButton = $("#go");
  const goLabel = $(".label", goButton);
  const pasteButton = $("#paste");
  const statusEl = $("#status");
  const result = $("#result");
  const actions = $("#r-actions");
  const toastEl = $("#toast");

  class ApiError extends Error {
    constructor(message, status, code) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  // ---------------------------------------------------------------- helpers

  const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
  const looksLikeTikTok = (text) => /(^|[\s/.@])tiktok(v)?\.com\//i.test(text);

  function formatDuration(totalSeconds) {
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = Math.floor(totalSeconds % 60);
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text; // textContent only: titles come from TikTok users
    return node;
  }

  function icon(id) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(SVG_NS, "use");
    use.setAttribute("href", `#${id}`);
    svg.append(use);
    return svg;
  }

  function setStatus(message, kind = "") {
    statusEl.textContent = message;
    statusEl.dataset.kind = kind;
    statusEl.hidden = !message;
  }

  function setBusy(busy) {
    goButton.disabled = busy;
    goButton.setAttribute("aria-busy", String(busy));
    goLabel.textContent = busy ? "Fetching…" : "Download";
  }

  let toastTimer;
  function toast(message) {
    toastEl.textContent = message;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, 6000);
  }

  // -------------------------------------------------------------------- API

  async function request(url, options) {
    let response;
    try {
      response = await fetch(url, options);
    } catch (error) {
      if (error.name === "AbortError") throw error;
      throw new ApiError("Couldn't reach the server. Check your connection and try again.", 0, "network");
    }
    let body = null;
    try { body = await response.json(); } catch { /* an error page that isn't ours */ }
    if (!response.ok) {
      throw new ApiError(
        body?.error?.message || `Something went wrong (HTTP ${response.status}). Please try again.`,
        response.status,
        body?.error?.code,
      );
    }
    return body;
  }

  let controller = null;

  async function lookup(text) {
    const value = text.trim();
    if (!value) {
      setStatus("Paste a TikTok link first.", "error");
      input.focus();
      return;
    }
    controller?.abort();
    const mine = (controller = new AbortController());
    setBusy(true);
    setStatus("");
    result.hidden = true;
    try {
      const data = await request("/api/info", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: value }),
        signal: mine.signal,
      });
      render(data);
    } catch (error) {
      if (error.name !== "AbortError") setStatus(error.message, "error");
    } finally {
      if (controller === mine) setBusy(false);
    }
  }

  // ----------------------------------------------------------------- render

  function buildAction(item) {
    const link = el("a", `btn action${item.primary ? " primary" : ""}`);
    link.href = item.href;
    link.dataset.kind = item.kind;
    const text = el("span", "a-text");
    text.append(el("span", "a-title", item.title), el("span", "a-detail", item.detail));
    link.append(icon(ICONS[item.kind] || "i-download"), text);
    if (item.tag) link.append(el("span", "tag", item.tag));
    return link;
  }

  function render(data) {
    const author = [data.author?.name, data.author?.handle && `@${data.author.handle}`].filter(Boolean);
    $("#r-author").textContent = author.join(" · ") || "TikTok";
    $("#r-title").textContent = data.description || data.title;

    const thumb = $("#r-thumb");
    if (data.thumbnail) {
      thumb.src = data.thumbnail;
      thumb.hidden = false;
    } else {
      thumb.removeAttribute("src");
      thumb.hidden = true;
    }
    const duration = $("#r-duration");
    duration.hidden = !data.duration;
    if (data.duration) duration.textContent = formatDuration(data.duration);

    const stats = data.stats || {};
    $("#r-stats").textContent = [["views", "views"], ["likes", "likes"], ["comments", "comments"], ["shares", "shares"]]
      .filter(([key]) => Number.isFinite(stats[key]))
      .map(([key, label]) => `${compact.format(stats[key])} ${label}`)
      .join(" · ");

    const note = $("#r-note");
    note.hidden = !data.note;
    note.textContent = data.note || "";

    actions.replaceChildren(...data.downloads.map(buildAction));
    result.hidden = false;
    $("#r-title").focus({ preventScroll: true });
    result.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // --------------------------------------------------------------- download

  /* The file is fetched by a hidden iframe so the browser streams it straight to disk
     (no buffering in memory) and our own page never navigates away.
       - Success: the response is an attachment, so the iframe never fires "load". The
         server sets a short-lived cookie when the response starts; we watch for it to
         know the download has begun.
       - Failure: the response is our JSON error, which does fire "load"; we show it. */
  function startDownload(link) {
    if (link.getAttribute("aria-busy") === "true") return;
    const nonce = Math.random().toString(36).slice(2, 12).padEnd(8, "0");
    const url = new URL(link.href);
    url.searchParams.set("n", nonce);

    const title = $(".a-title", link);
    const originalTitle = title.textContent;
    link.setAttribute("aria-busy", "true");
    title.textContent = "Preparing…";

    const frame = document.createElement("iframe");
    frame.hidden = true;
    frame.title = "Download";
    frame.src = url.href;

    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      clearInterval(poll);
      clearTimeout(giveUp);
      link.removeAttribute("aria-busy");
      title.textContent = originalTitle;
      document.cookie = `dl_${nonce}=; Max-Age=0; Path=/`;
      setTimeout(() => frame.remove(), 60000); // leave it be while the browser takes over the transfer
    };
    const poll = setInterval(() => {
      if (document.cookie.includes(`dl_${nonce}=`)) finish();
    }, 250);
    const giveUp = setTimeout(finish, 120000);

    frame.addEventListener("load", () => {
      let message = "";
      try { message = JSON.parse(frame.contentDocument.body.textContent).error.message; } catch { /* not our error */ }
      if (message) toast(message);
      finish();
    });
    document.body.append(frame);
  }

  actions.addEventListener("click", (event) => {
    const link = event.target.closest("a.action");
    const plain = event.button === 0 && !(event.metaKey || event.ctrlKey || event.shiftKey || event.altKey);
    if (!link || !plain) return; // let "open in new tab" and friends behave normally
    event.preventDefault();
    startDownload(link);
  });

  // ------------------------------------------------------------------ wiring

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    lookup(input.value);
  });

  // Pasting a TikTok link starts the lookup right away.
  input.addEventListener("paste", (event) => {
    const text = event.clipboardData?.getData("text") || "";
    if (looksLikeTikTok(text)) setTimeout(() => form.requestSubmit(), 0);
  });

  if (navigator.clipboard?.readText) {
    pasteButton.addEventListener("click", async () => {
      try {
        input.value = await navigator.clipboard.readText();
        input.focus();
        if (input.value.trim()) form.requestSubmit();
      } catch {
        input.focus();
        setStatus("Couldn't read the clipboard. Paste into the box with Ctrl+V (or press and hold on a phone).", "info");
      }
    });
  } else {
    pasteButton.hidden = true; // needs a secure context and a browser that allows clipboard reads
  }

  $("#r-reset").addEventListener("click", () => {
    result.hidden = true;
    input.value = "";
    setStatus("");
    window.scrollTo({ top: 0, behavior: "smooth" });
    input.focus({ preventScroll: true });
  });

  $("#theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("snaptik-theme", next); } catch { /* storage unavailable */ }
  });

  // /?url=<tiktok link> pre-fills and runs the lookup (handy for bookmarklets and share targets).
  const prefill = new URLSearchParams(location.search).get("url");
  if (prefill) {
    input.value = prefill;
    lookup(prefill);
  }
})();
