/* SnapTik front end: no framework, no build step. */
(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const SVG_NS = "http://www.w3.org/2000/svg";
  const ICONS = {
    video: "i-video", video_sd: "i-video", video_wm: "i-video",
    audio: "i-music", cover: "i-image", images: "i-image", meta: "i-file",
  };

  const form = $("#dl-form");
  const input = $("#url");
  const goButton = $("#go");
  const goLabel = $(".label", goButton);
  const pasteButton = $("#paste");
  const statusEl = $("#status");
  const toastEl = $("#toast");

  const result = $("#result");
  const actions = $("#r-actions");
  const backButton = $("#r-back");

  const profile = $("#profile");
  const postList = $("#p-list");
  const downloadButton = $("#p-download");
  const stopButton = $("#p-stop");
  const progressBox = $("#p-progress");
  const progressBar = $("#p-bar");
  const progressText = $("#p-progress-text");
  const wants = { media: $("#w-media"), audio: $("#w-audio"), cover: $("#w-cover"), best: $("#w-best") };
  const SAVES = ["media", "audio", "cover"]; // "best" only says which video file, it is not something to save

  const state = {
    profile: null, // the profile being shown; kept so "back to profile" needs no new request
    selected: new Set(), // ids ticked in the post list
    job: null, // id of the ZIP that is being built right now
    jobHandle: null, // whose posts it holds, in case another profile is on screen meanwhile
  };

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
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function formatDuration(totalSeconds) {
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = Math.floor(totalSeconds % 60);
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
  }

  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  function formatWait(seconds) {
    return seconds < 90 ? plural(Math.max(5, Math.round(seconds / 5) * 5), "second") : plural(Math.round(seconds / 60), "minute");
  }

  /* Unguessable ids: a download's id is also what lets the page watch and stop it. */
  function randomId(length) {
    const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789";
    const bytes = new Uint8Array(length);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (byte) => alphabet[byte % alphabet.length]).join("");
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text; // textContent only: captions come from TikTok users
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
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, 7000);
  }

  function show(section) {
    result.hidden = section !== result;
    profile.hidden = section !== profile;
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

  async function lookup(text, { from = null } = {}) {
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
    try {
      const data = await request("/api/info", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: value }),
        signal: mine.signal,
      });
      if (data.type === "profile") renderProfile(data);
      else renderPost(data, from);
    } catch (error) {
      if (error.name !== "AbortError") setStatus(error.message, "error");
    } finally {
      if (controller === mine) setBusy(false);
    }
  }

  // ------------------------------------------------------------ one post

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

  function renderPost(data, from) {
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
    $("#r-stats").textContent = ["views", "likes", "comments", "shares"]
      .filter((key) => Number.isFinite(stats[key]))
      .map((key) => `${compact.format(stats[key])} ${key}`)
      .join(" · ");

    const note = $("#r-note");
    note.hidden = !data.note;
    note.textContent = data.note || "";

    backButton.hidden = !from;
    if (from) backButton.textContent = `← Back to @${from.handle}`;

    actions.replaceChildren(...data.downloads.map(buildAction));
    show(result);
    $("#r-title").focus({ preventScroll: true });
    result.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // -------------------------------------------------------- the download frame

  /* Files are fetched by a hidden iframe so the browser streams them straight to disk (no
     buffering in memory) and our own page never navigates away.
       - Success: the response is an attachment, so the iframe never fires "load". The server
         sets a short-lived cookie when the response starts; watching for it tells us the
         download has begun.
       - Failure: the response is our JSON error, which does fire "load"; we show it. */
  function fetchInFrame(href, { onStarted, onError, linger = 60000 } = {}) {
    const url = new URL(href, location.origin);
    let nonce = url.searchParams.get("n");
    if (!nonce) {
      nonce = randomId(10);
      url.searchParams.set("n", nonce);
    }
    const frame = document.createElement("iframe");
    frame.hidden = true;
    frame.title = "Download";
    frame.src = url.href;

    let finished = false;
    const finish = (message) => {
      if (finished) return;
      finished = true;
      clearInterval(poll);
      clearTimeout(giveUp);
      document.cookie = `dl_${nonce}=; Max-Age=0; Path=/`;
      setTimeout(() => frame.remove(), linger); // leave it be while the browser takes over the transfer
      if (message) onError?.(message);
      else onStarted?.();
    };
    const poll = setInterval(() => {
      if (document.cookie.includes(`dl_${nonce}=`)) finish();
    }, 250);
    const giveUp = setTimeout(() => finish(), 120000);

    frame.addEventListener("load", () => {
      let message = "";
      try { message = JSON.parse(frame.contentDocument.body.textContent).error.message; } catch { /* not our error */ }
      if (message) toast(message);
      finish(message);
    });
    document.body.append(frame);
  }

  function startDownload(link) {
    if (link.getAttribute("aria-busy") === "true") return;
    const title = $(".a-title", link);
    const original = title.textContent;
    link.setAttribute("aria-busy", "true");
    title.textContent = "Preparing…";
    const restore = () => {
      link.removeAttribute("aria-busy");
      title.textContent = original;
    };
    fetchInFrame(link.href, { onStarted: restore, onError: restore });
  }

  actions.addEventListener("click", (event) => {
    const link = event.target.closest("a.action");
    const plain = event.button === 0 && !(event.metaKey || event.ctrlKey || event.shiftKey || event.altKey);
    if (!link || !plain) return; // let "open in new tab" and friends behave normally
    event.preventDefault();
    startDownload(link);
  });

  // ------------------------------------------------------------- a profile

  // What the server is asked for: the ticked boxes, except that "highest resolution" only means something for videos.
  const wantedKinds = () => Object.keys(wants).filter((key) => wants[key].checked && (key !== "best" || wants.media.checked));
  const bulkOptions = () => state.profile?.bulk || {};

  function metaLine(post) {
    return [
      post.date,
      post.photos ? plural(post.photos, "photo") : post.duration ? formatDuration(post.duration) : null,
      Number.isFinite(post.views) ? `${compact.format(post.views)} views` : null,
    ].filter(Boolean).join(" · ");
  }

  function buildPost(post, selectable) {
    const item = el("li", "post");
    const pick = el("label", "post-pick");
    if (selectable) {
      const box = el("input");
      box.type = "checkbox";
      box.value = post.id;
      box.checked = state.selected.has(post.id);
      pick.append(box);
    }
    const thumb = el("img", "post-thumb");
    thumb.alt = "";
    thumb.loading = "lazy";
    thumb.referrerPolicy = "no-referrer";
    if (post.thumbnail) thumb.src = post.thumbnail;
    const text = el("span", "post-text");
    text.append(el("span", "post-caption", post.caption || "(no caption)"), el("span", "post-meta", metaLine(post)));
    pick.append(thumb, text);

    const open = el("button", "link-btn open", "Open");
    open.type = "button";
    open.dataset.url = post.url;
    item.append(pick, open);
    return item;
  }

  function renderProfile(data) {
    if (state.profile?.handle !== data.handle) state.selected = new Set();
    state.profile = data;

    $("#p-handle").textContent = `@${data.handle}`;
    $("#p-title").textContent = data.name || data.handle;
    const count = data.posts.length;
    $("#p-count").textContent = `${plural(count, "post")}${data.more ? ", the most recent ones" : ""}`;

    const enabled = Boolean(data.bulk?.enabled);
    const warning = $("#p-warning");
    const notice = data.warning || (enabled ? "" : "Saving many posts at once is turned off on this server. Open a post to download it.");
    warning.hidden = !notice;
    warning.textContent = notice;

    $("#p-bulk").hidden = !enabled;
    $("#p-actions").hidden = !enabled;
    postList.classList.toggle("no-select", !enabled);
    postList.replaceChildren(...data.posts.map((post) => buildPost(post, enabled)));
    postList.scrollTop = 0;

    if (!state.job) resetProgress(); // a ZIP that is still being built keeps its progress and Stop button
    updateSelection();
    show(profile);
    $("#p-title").focus({ preventScroll: true });
    profile.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function syncBoxes() {
    for (const box of postList.querySelectorAll('input[type="checkbox"]')) box.checked = state.selected.has(box.value);
  }

  function updateSelection() {
    if (!state.profile) return;
    const { max_posts: max = 30, delay = 3 } = bulkOptions();
    const n = state.selected.size;
    $("#p-selected").textContent = `${n} selected (up to ${max} per ZIP)`;
    $("#p-all").textContent = `Select the newest ${Math.min(max, state.profile.posts.length)}`;
    $(".label", downloadButton).textContent = n ? `Download ${plural(n, "post")} as ZIP` : "Download as ZIP";
    downloadButton.disabled = !n || !SAVES.some((key) => wants[key].checked) || Boolean(state.job);
    wants.best.disabled = !wants.media.checked;
    $("#p-quality").hidden = !wants.media.checked;
    $("#p-quality").textContent = wants.best.checked
      ? "Highest resolution, up to 1080p. Some of these files are H.265, which some players (such as the default Windows apps) can't open."
      : "Videos are saved in the format that plays on any device (H.264, up to 720p). Tick “Highest resolution” for sharper 1080p files.";
    $("#p-hint").textContent = n
      ? `Posts are fetched one at a time, with a pause of about ${plural(delay, "second")} between them so TikTok doesn't block this server. ` +
        `${plural(n, "post")} will take roughly ${formatWait(n * (delay + 4))}. For a complete archive of a big profile, use the command-line tool in the README.`
      : "Tick the posts you want, then download them together.";
  }

  postList.addEventListener("change", (event) => {
    const box = event.target;
    if (!(box instanceof HTMLInputElement) || box.type !== "checkbox") return;
    const { max_posts: max = 30 } = bulkOptions();
    if (box.checked && state.selected.size >= max) {
      box.checked = false;
      toast(`A ZIP holds up to ${max} posts. Download this batch first, then pick the next one.`);
      return;
    }
    if (box.checked) state.selected.add(box.value);
    else state.selected.delete(box.value);
    updateSelection();
  });

  postList.addEventListener("click", (event) => {
    const button = event.target.closest("button.open");
    if (!button) return;
    input.value = button.dataset.url;
    lookup(button.dataset.url, { from: state.profile });
  });

  $("#p-all").addEventListener("click", () => {
    const { max_posts: max = 30 } = bulkOptions();
    state.selected = new Set(state.profile.posts.slice(0, max).map((post) => post.id));
    syncBoxes();
    updateSelection();
  });

  $("#p-none").addEventListener("click", () => {
    state.selected.clear();
    syncBoxes();
    updateSelection();
  });

  for (const box of Object.values(wants)) box.addEventListener("change", updateSelection);

  backButton.addEventListener("click", () => {
    if (state.profile) renderProfile(state.profile);
  });

  // ------------------------------------------------------------ the ZIP run

  function resetProgress() {
    progressBox.hidden = true;
    stopButton.hidden = false;
    stopButton.disabled = false;
    stopButton.textContent = "Stop and keep what's done";
    progressBar.max = 1;
    progressBar.value = 0;
    progressText.textContent = "";
  }

  const jobLabel = () => (state.jobHandle && state.jobHandle !== state.profile?.handle ? `@${state.jobHandle}: ` : "");

  function showProgress(snap) {
    progressBox.hidden = false;
    progressBar.max = Math.max(1, snap.total);
    progressBar.value = snap.done;
    const parts = [`${snap.done} of ${snap.total} posts`];
    if (snap.saved) parts.push(`${snap.saved} saved`);
    if (snap.skipped) parts.push(`${snap.skipped} unavailable`);
    if (snap.failed) parts.push(`${snap.failed} failed`);
    progressText.textContent = jobLabel() + parts.join(" · ") + (snap.message ? `. ${snap.message}` : "");
  }

  function summary(snap) {
    const tail = [snap.skipped && `${snap.skipped} unavailable (private or removed)`, snap.failed && `${snap.failed} failed`]
      .filter(Boolean).join(", ");
    if (snap.state === "done") {
      return `Done: ${snap.saved} saved${tail ? `, ${tail}` : ""}. The ZIP is in your downloads; posts.csv in it lists every caption.`;
    }
    if (snap.state === "stopped") {
      return `Stopped: ${snap.saved} saved${tail ? `, ${tail}` : ""}. The ZIP in your downloads has what was finished.`;
    }
    return `Stopped early: ${snap.message} The ZIP in your downloads has ${plural(snap.saved, "saved post")} and a REPORT.txt.`;
  }

  function endRun(snap) {
    if (snap) {
      progressBar.value = progressBar.max;
      progressText.textContent = jobLabel() + summary(snap);
      stopButton.hidden = true;
    } else {
      progressBox.hidden = true;
    }
    state.job = null;
    state.jobHandle = null;
    updateSelection();
  }

  async function watch(job) {
    let misses = 0;
    while (state.job === job) {
      await sleep(1500);
      if (state.job !== job) return;
      try {
        const snap = await request(`/api/bulk/progress?n=${encodeURIComponent(job)}`);
        misses = 0;
        showProgress(snap);
        if (snap.state !== "running") return endRun(snap);
      } catch (error) {
        if (error.status === 404 && ++misses >= 3) {
          toast("The download didn't start. Please try again.");
          return endRun(null);
        }
      }
    }
  }

  downloadButton.addEventListener("click", () => {
    if (state.job) return;
    const ids = [...postList.querySelectorAll('input[type="checkbox"]:checked')].map((box) => box.value); // newest first
    const what = wantedKinds().join(",");
    if (!ids.length || !what) return;

    const job = randomId(20);
    const url = new URL("/api/bulk", location.origin);
    url.search = new URLSearchParams({ user: state.profile.handle, ids: ids.join(","), what, n: job }).toString();

    state.job = job;
    state.jobHandle = state.profile.handle;
    resetProgress();
    showProgress({ total: ids.length, done: 0, saved: 0, skipped: 0, failed: 0, message: "Starting…" });
    progressBox.scrollIntoView({ behavior: "smooth", block: "nearest" });
    updateSelection();
    // The server refusing the request shows its own message in a toast. A ZIP can take up to half an hour, so
    // the frame stays for longer than a single file's (no browser should be able to tie the transfer to it).
    fetchInFrame(url.href, { onError: () => endRun(null), linger: 35 * 60 * 1000 });
    watch(job);
  });

  stopButton.addEventListener("click", async () => {
    if (!state.job) return;
    stopButton.disabled = true;
    stopButton.textContent = "Stopping…";
    try {
      await request(`/api/bulk/stop?n=${encodeURIComponent(state.job)}`, { method: "POST" });
    } catch (error) {
      toast(error.message);
    }
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

  function startOver() {
    if (state.job && !confirm("A ZIP is still being built. Start over anyway? It keeps running; use Stop first to end it.")) return;
    show(null);
    state.profile = null;
    state.selected = new Set();
    input.value = "";
    setStatus("");
    window.scrollTo({ top: 0, behavior: "smooth" });
    input.focus({ preventScroll: true });
  }

  $("#r-reset").addEventListener("click", startOver);
  $("#p-reset").addEventListener("click", startOver);

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
