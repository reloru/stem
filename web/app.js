/* Stem — client. No dependencies, no build step, no inline script.
 *
 * Three responsibilities: get a file to the server, watch the job, and run a
 * four-channel mixer over the previews the server produced. The mixer is
 * playback only; every download is rendered server-side from the lossless
 * stems so nothing the user saves has been through MP3.
 */
(function () {
  "use strict";

  /* ------------------------------------------------------------------ *
   * constants
   * ------------------------------------------------------------------ */

  var STEM_COLOURS = {
    vocals: "#ffb454",
    drums: "#ff6b81",
    bass: "#5aa9ff",
    other: "#4ecdc4"
  };

  // Fader travel in decibels. Unity sits at 0.8 of the way up, which leaves
  // 12 dB of boost above it and a usable amount of resolution below.
  var MIN_DB = -48;
  var MAX_DB = 12;
  var UNITY_POS = (0 - MIN_DB) / (MAX_DB - MIN_DB);
  var SNAP = 0.015;
  var PEAK_RESOLUTION = 2048;
  var KEY_STORAGE = "stem.accessKey";

  /* ------------------------------------------------------------------ *
   * small helpers
   * ------------------------------------------------------------------ */

  function $(id) { return document.getElementById(id); }

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function clamp(value, low, high) {
    return value < low ? low : value > high ? high : value;
  }

  function posToDb(pos) {
    return pos <= 0 ? -Infinity : MIN_DB + pos * (MAX_DB - MIN_DB);
  }

  function posToGain(pos) {
    return pos <= 0 ? 0 : Math.pow(10, posToDb(pos) / 20);
  }

  function formatDb(pos) {
    if (pos <= 0) return "-∞ dB";
    var db = posToDb(pos);
    return (db >= 0 ? "+" : "") + db.toFixed(1) + " dB";
  }

  function formatTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) seconds = 0;
    var whole = Math.floor(seconds);
    var minutes = Math.floor(whole / 60);
    var rest = whole % 60;
    return minutes + ":" + (rest < 10 ? "0" : "") + rest;
  }

  function formatBytes(bytes) {
    if (!bytes) return "";
    var mb = bytes / (1024 * 1024);
    return mb >= 1 ? mb.toFixed(1) + " MB" : Math.round(bytes / 1024) + " kB";
  }

  var toastTimer = null;
  function toast(message, bad) {
    var node = $("toast");
    node.textContent = message;
    node.classList.toggle("bad", !!bad);
    node.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.hidden = true; }, 5200);
  }

  function download(url, filename) {
    var anchor = make("a");
    anchor.href = url;
    if (filename) anchor.download = filename;
    anchor.style.display = "none";
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
  }

  /* ------------------------------------------------------------------ *
   * access key
   * ------------------------------------------------------------------ */

  var accessKey = "";
  try { accessKey = window.localStorage.getItem(KEY_STORAGE) || ""; } catch (e) { accessKey = ""; }

  function storeKey(value) {
    accessKey = value;
    try {
      if (value) window.localStorage.setItem(KEY_STORAGE, value);
      else window.localStorage.removeItem(KEY_STORAGE);
    } catch (e) { /* private mode; the key simply will not persist */ }
  }

  /* ------------------------------------------------------------------ *
   * remembered balances
   *
   * A phone browser discards backgrounded tabs under memory pressure, and
   * the decoded stems make this page an expensive tab to keep. A discard
   * reloads the page, so without this every fader, mute and solo the user
   * set would be gone when they switch apps and come back.
   * ------------------------------------------------------------------ */

  var MIX_STORAGE = "stem.mixes";
  var MIX_HISTORY = 20;

  function readMixes() {
    try { return JSON.parse(window.localStorage.getItem(MIX_STORAGE)) || {}; }
    catch (e) { return {}; }
  }

  function saveMix() {
    if (!job || !engine.channels.length) return;
    var all = readMixes();
    all[job.id] = {
      at: Date.now(),
      monitor: engine.masterPos,
      channels: engine.channels.map(function (channel) {
        return {
          name: channel.name,
          pos: channel.pos,
          mute: channel.mute,
          solo: channel.solo
        };
      })
    };
    Object.keys(all)
      .sort(function (a, b) { return (all[b].at || 0) - (all[a].at || 0); })
      .slice(MIX_HISTORY)
      .forEach(function (id) { delete all[id]; });
    try { window.localStorage.setItem(MIX_STORAGE, JSON.stringify(all)); }
    catch (e) { /* private mode or quota; the balance just will not persist */ }
  }

  var saveTimer = null;
  function scheduleSave() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(saveMix, 400);
  }

  function restoreMix(jobId) {
    var stored = readMixes()[jobId];
    if (!stored || !stored.channels) return false;
    var byName = {};
    stored.channels.forEach(function (entry) { byName[entry.name] = entry; });
    var restored = false;
    engine.channels.forEach(function (channel) {
      var entry = byName[channel.name];
      if (!entry) return;
      if (typeof entry.pos === "number") channel.pos = clamp(entry.pos, 0, 1);
      channel.mute = !!entry.mute;
      channel.solo = !!entry.solo;
      restored = true;
    });
    if (restored && typeof stored.monitor === "number") {
      engine.masterPos = clamp(stored.monitor, 0, 1);
      if (masterFader) masterFader.set(engine.masterPos, false);
      $("master-readout").textContent = formatDb(engine.masterPos);
    }
    return restored;
  }

  var keyResolve = null;

  function askForKey(message) {
    $("key-error").hidden = !message;
    $("key-error").textContent = message || "";
    $("key-input").value = accessKey;
    $("key-dialog").hidden = false;
    $("key-input").focus();
    return new Promise(function (resolve) { keyResolve = resolve; });
  }

  function closeKeyDialog(result) {
    $("key-dialog").hidden = true;
    var resolve = keyResolve;
    keyResolve = null;
    if (resolve) resolve(result);
  }

  async function verifyKey(candidate) {
    var response = await fetch("/api/key", {
      method: "POST",
      headers: { "X-Stem-Key": candidate }
    });
    return response.ok;
  }

  /* ------------------------------------------------------------------ *
   * api
   * ------------------------------------------------------------------ */

  async function apiGet(path) {
    var response = await fetch(path, { headers: { "X-Stem-Key": accessKey } });
    var payload = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(payload.error || "Request failed (" + response.status + ")");
    return payload;
  }

  async function apiPost(path, body) {
    var response = await fetch(path, {
      method: "POST",
      headers: {
        "X-Stem-Key": accessKey,
        "Content-Type": "application/json"
      },
      body: JSON.stringify(body || {})
    });
    var payload = await response.json().catch(function () { return {}; });
    if (response.status === 401) { var err = new Error("Access key required."); err.unauthorised = true; throw err; }
    if (!response.ok) throw new Error(payload.error || "Request failed (" + response.status + ")");
    return payload;
  }

  /* ------------------------------------------------------------------ *
   * fader
   * ------------------------------------------------------------------ */

  function Fader(element, options) {
    this.el = element;
    this.value = options.value === undefined ? UNITY_POS : options.value;
    this.defaultValue = options.defaultValue === undefined ? this.value : options.defaultValue;
    this.step = options.step || 0.01;
    this.onInput = options.onInput || function () {};
    this.orientation = options.orientation || "h";

    this.track = make("div", "fader-track");
    this.fill = make("div", "fader-fill");
    this.unity = make("div", "fader-unity");
    this.knob = make("div", "fader-knob");
    this.track.appendChild(this.fill);
    this.track.appendChild(this.unity);
    this.track.appendChild(this.knob);
    this.el.appendChild(this.track);

    this.el.setAttribute("role", "slider");
    this.el.setAttribute("tabindex", "0");
    this.el.setAttribute("aria-valuemin", "0");
    this.el.setAttribute("aria-valuemax", "100");
    if (options.label) this.el.setAttribute("aria-label", options.label);
    this.el.style.setProperty("--unity", String(UNITY_POS));
    if (options.colour) this.el.style.setProperty("--stem", options.colour);

    this.setOrientation(this.orientation);
    this.set(this.value, false);

    var self = this;
    this.dragging = false;

    this.el.addEventListener("pointerdown", function (event) {
      self.dragging = true;
      self.el.setPointerCapture(event.pointerId);
      self.el.focus();
      self.fromPointer(event);
      event.preventDefault();
    });
    this.el.addEventListener("pointermove", function (event) {
      if (self.dragging) self.fromPointer(event);
    });
    this.el.addEventListener("pointerup", function (event) {
      self.dragging = false;
      if (self.el.hasPointerCapture(event.pointerId)) self.el.releasePointerCapture(event.pointerId);
    });
    this.el.addEventListener("pointercancel", function () { self.dragging = false; });
    this.el.addEventListener("dblclick", function () { self.set(self.defaultValue); });
    this.el.addEventListener("keydown", function (event) { self.fromKey(event); });
  }

  Fader.prototype.setOrientation = function (orientation) {
    this.orientation = orientation;
    this.el.classList.toggle("fader-v", orientation === "v");
    this.el.classList.toggle("fader-h", orientation !== "v");
    this.el.setAttribute("aria-orientation", orientation === "v" ? "vertical" : "horizontal");
  };

  /* `snap` is for dragging only. A keyboard step is 0.01 and the snap window
   * is wider than that, so applying it to key presses would pull every single
   * step back to unity and leave the fader immovable from the keyboard. */
  Fader.prototype.set = function (value, notify, snap) {
    var next = clamp(value, 0, 1);
    if (snap && Math.abs(next - UNITY_POS) < SNAP) next = UNITY_POS;
    this.value = next;
    this.el.style.setProperty("--pos", String(next));
    this.el.classList.toggle("silent", next <= 0);
    this.el.setAttribute("aria-valuenow", String(Math.round(next * 100)));
    this.el.setAttribute("aria-valuetext", formatDb(next));
    if (notify !== false) this.onInput(next);
  };

  Fader.prototype.fromPointer = function (event) {
    var rect = this.track.getBoundingClientRect();
    var position;
    if (this.orientation === "v") {
      position = rect.height ? 1 - (event.clientY - rect.top) / rect.height : 0;
    } else {
      position = rect.width ? (event.clientX - rect.left) / rect.width : 0;
    }
    this.set(position, true, true);
  };

  Fader.prototype.fromKey = function (event) {
    var delta = 0;
    switch (event.key) {
      case "ArrowUp": case "ArrowRight": delta = this.step; break;
      case "ArrowDown": case "ArrowLeft": delta = -this.step; break;
      case "PageUp": delta = this.step * 10; break;
      case "PageDown": delta = -this.step * 10; break;
      case "Home": this.set(0); event.preventDefault(); return;
      case "End": this.set(1); event.preventDefault(); return;
      case "Enter": case " ": this.set(this.defaultValue); event.preventDefault(); return;
      default: return;
    }
    this.set(this.value + delta);
    event.preventDefault();
  };

  /* ------------------------------------------------------------------ *
   * audio engine
   * ------------------------------------------------------------------ */

  var engine = {
    ctx: null,
    master: null,
    masterPos: UNITY_POS,
    channels: [],
    duration: 0,
    playing: false,
    startedAt: 0,
    offset: 0,
    loop: false,
    frame: 0
  };

  function audioContext() {
    if (!engine.ctx) {
      var Ctor = window.AudioContext || window.webkitAudioContext;
      engine.ctx = new Ctor();
      engine.master = engine.ctx.createGain();
      engine.master.gain.value = posToGain(engine.masterPos);
      engine.master.connect(engine.ctx.destination);
    }
    return engine.ctx;
  }

  async function loadStems(jobId, stemNames, onProgress) {
    var ctx = audioContext();
    var loaded = 0;
    var channels = [];

    for (var i = 0; i < stemNames.length; i++) {
      var name = stemNames[i];
      var response = await fetch("/api/jobs/" + jobId + "/preview/" + name + ".mp3");
      if (!response.ok) throw new Error("Could not load the " + name + " stem.");
      var bytes = await response.arrayBuffer();
      var buffer = await decode(ctx, bytes);
      var gainNode = ctx.createGain();
      gainNode.connect(engine.master);
      channels.push({
        name: name,
        buffer: buffer,
        node: gainNode,
        source: null,
        pos: UNITY_POS,
        mute: false,
        solo: false,
        peaks: computePeaks(buffer, PEAK_RESOLUTION)
      });
      loaded += 1;
      onProgress(loaded / stemNames.length);
    }

    // One shared vertical scale keeps the four waveforms comparable: a stem
    // that is genuinely quiet still looks quiet next to the others.
    var ceiling = 0;
    channels.forEach(function (channel) {
      for (var i = 0; i < channel.peaks.length; i++) {
        if (channel.peaks[i] > ceiling) ceiling = channel.peaks[i];
      }
    });
    var scale = ceiling > 0 ? 1 / ceiling : 1;
    channels.forEach(function (channel) { channel.peakScale = scale; });

    engine.channels = channels;
    engine.duration = channels.reduce(function (longest, channel) {
      return Math.max(longest, channel.buffer.duration);
    }, 0);
    engine.offset = 0;
    applyGains();
    return channels;
  }

  function decode(ctx, bytes) {
    // Safari still needs the callback form of decodeAudioData.
    return new Promise(function (resolve, reject) {
      var result = ctx.decodeAudioData(bytes, resolve, reject);
      if (result && typeof result.then === "function") result.then(resolve, reject);
    });
  }

  function computePeaks(buffer, columns) {
    var left = buffer.getChannelData(0);
    var right = buffer.numberOfChannels > 1 ? buffer.getChannelData(1) : left;
    var block = Math.max(1, Math.floor(buffer.length / columns));
    var peaks = new Float32Array(columns);
    for (var c = 0; c < columns; c++) {
      var start = c * block;
      var end = Math.min(buffer.length, start + block);
      var peak = 0;
      for (var i = start; i < end; i++) {
        var value = Math.abs(left[i]);
        var other = Math.abs(right[i]);
        if (other > value) value = other;
        if (value > peak) peak = value;
      }
      peaks[c] = peak;
    }
    return peaks;
  }

  function anySolo() {
    return engine.channels.some(function (channel) { return channel.solo; });
  }

  function effectiveGain(channel) {
    var soloed = anySolo();
    if (channel.mute) return 0;
    if (soloed && !channel.solo) return 0;
    return posToGain(channel.pos);
  }

  function applyGains() {
    if (!engine.ctx) return;
    var now = engine.ctx.currentTime;
    engine.channels.forEach(function (channel) {
      channel.node.gain.setTargetAtTime(effectiveGain(channel), now, 0.012);
    });
    if (engine.master) engine.master.gain.setTargetAtTime(posToGain(engine.masterPos), now, 0.012);
  }

  function currentTime() {
    if (!engine.playing || !engine.ctx) return engine.offset;
    var elapsed = engine.ctx.currentTime - engine.startedAt;
    if (elapsed < 0) elapsed = 0;
    return Math.min(engine.duration, engine.offset + elapsed);
  }

  function stopSources() {
    engine.channels.forEach(function (channel) {
      if (channel.source) {
        try { channel.source.stop(); } catch (e) { /* already stopped */ }
        try { channel.source.disconnect(); } catch (e) { /* detached */ }
        channel.source = null;
      }
    });
  }

  async function play() {
    if (engine.playing || !engine.channels.length) return;
    var ctx = audioContext();
    if (ctx.state === "suspended") await ctx.resume();
    if (engine.offset >= engine.duration - 0.02) engine.offset = 0;

    var when = ctx.currentTime + 0.06;
    engine.channels.forEach(function (channel) {
      var source = ctx.createBufferSource();
      source.buffer = channel.buffer;
      source.connect(channel.node);
      source.start(when, Math.min(engine.offset, Math.max(0, channel.buffer.duration - 0.001)));
      channel.source = source;
    });
    engine.startedAt = when;
    engine.playing = true;
    applyGains();
  }

  function pause() {
    if (!engine.playing) return;
    engine.offset = currentTime();
    stopSources();
    engine.playing = false;
  }

  function stop() {
    stopSources();
    engine.playing = false;
    engine.offset = 0;
  }

  function seek(seconds) {
    var target = clamp(seconds, 0, Math.max(0, engine.duration - 0.02));
    if (engine.playing) {
      stopSources();
      engine.playing = false;
      engine.offset = target;
      play();
    } else {
      engine.offset = target;
    }
  }

  /* ------------------------------------------------------------------ *
   * waveforms
   * ------------------------------------------------------------------ */

  function drawWaveform(canvas, channel) {
    var width = canvas.clientWidth;
    var height = canvas.clientHeight;
    if (!width || !height) return;
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);

    var ctx2d = canvas.getContext("2d");
    ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx2d.clearRect(0, 0, width, height);
    ctx2d.fillStyle = STEM_COLOURS[channel.name] || "#6ea8fe";

    var columns = Math.max(1, Math.floor(width));
    var per = channel.peaks.length / columns;
    var mid = height / 2;
    var reach = mid - 1;
    for (var x = 0; x < columns; x++) {
      var from = Math.floor(x * per);
      var to = Math.max(from + 1, Math.floor((x + 1) * per));
      var peak = 0;
      for (var i = from; i < to && i < channel.peaks.length; i++) {
        if (channel.peaks[i] > peak) peak = channel.peaks[i];
      }
      var half = Math.max(0.5, Math.min(reach, peak * channel.peakScale * reach));
      ctx2d.fillRect(x, mid - half, 1, half * 2);
    }
  }

  /* ------------------------------------------------------------------ *
   * views
   * ------------------------------------------------------------------ */

  var VIEWS = ["view-upload", "view-processing", "view-failed", "view-mixer"];

  function showView(id) {
    VIEWS.forEach(function (name) { $(name).hidden = name !== id; });
  }

  /* ------------------------------------------------------------------ *
   * application state
   * ------------------------------------------------------------------ */

  var config = null;
  var job = null;
  var pollTimer = null;
  var stemFaders = [];
  var masterFader = null;
  var strips = [];
  var desktopQuery = window.matchMedia("(min-width: 900px)");

  function stopPolling() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  function resetToUpload() {
    stopPolling();
    stop();
    engine.channels = [];
    engine.duration = 0;
    strips = [];
    stemFaders = [];
    $("strips").textContent = "";
    $("mixer").hidden = true;
    $("mixer-loading").hidden = false;
    $("track-meta").hidden = true;
    $("new-track-button").hidden = true;
    $("copy-link-button").hidden = true;
    $("file-input").value = "";
    $("upload-progress").hidden = true;
    job = null;
    if (window.location.hash) history.replaceState(null, "", window.location.pathname);
    showView("view-upload");
  }

  /* Installed on the home screen (iOS or Android), this app has no address
   * bar and no share/copy affordance of its own -- there is otherwise no way
   * to hand this job to another browser or device, or even to see its URL.
   * Falls back to showing the link in the toast itself if the clipboard
   * write is refused (an insecure context, or the permission denied). */
  async function copyJobLink() {
    if (!job) return;
    var url = location.origin + location.pathname + "#job=" + job.id;
    try {
      await navigator.clipboard.writeText(url);
      toast("Link copied — opens this job in any browser.");
    } catch (error) {
      toast(url);
    }
  }

  /* ---------------------- upload ---------------------- */

  function uploadFile(file) {
    if (!file) return;
    var limit = (config ? config.max_upload_mb : 100) * 1024 * 1024;
    if (file.size > limit) {
      toast("That file is " + formatBytes(file.size) + "; the limit is " + config.max_upload_mb + " MB.", true);
      return;
    }

    var form = new FormData();
    form.append("file", file, file.name);

    var request = new XMLHttpRequest();
    request.open("POST", "/api/jobs");
    request.setRequestHeader("X-Stem-Key", accessKey);
    request.responseType = "json";

    $("upload-progress").hidden = false;
    $("upload-fill").style.width = "0%";
    $("upload-status").textContent = "Uploading " + file.name;

    request.upload.onprogress = function (event) {
      if (!event.lengthComputable) return;
      var percent = (event.loaded / event.total) * 100;
      $("upload-fill").style.width = percent.toFixed(1) + "%";
      $("upload-status").textContent =
        "Uploading " + file.name + " — " + percent.toFixed(0) + "%";
    };

    request.onerror = function () {
      $("upload-progress").hidden = true;
      toast("The upload failed to reach the server.", true);
    };

    request.onload = async function () {
      $("upload-progress").hidden = true;
      var payload = request.response || {};
      if (request.status === 401) {
        var supplied = await askForKey("That key was not accepted.");
        if (supplied) uploadFile(file);
        return;
      }
      if (request.status < 200 || request.status >= 300) {
        toast(payload.error || "Upload rejected (" + request.status + ").", true);
        return;
      }
      job = payload;
      history.replaceState(null, "", "#job=" + job.id);
      showProcessing(job);
      pollTimer = setTimeout(pollJob, 900);
    };

    request.send(form);
  }

  async function beginUpload(file) {
    if (config && config.requires_key && !accessKey) {
      var supplied = await askForKey("");
      if (!supplied) return;
    }
    uploadFile(file);
  }

  /* ---------------------- polling ---------------------- */

  function showProcessing(current) {
    showView("view-processing");
    $("new-track-button").hidden = false;
    $("copy-link-button").hidden = false;
    updateTrackMeta(current);

    var stage = current.stage || "Working";
    if (current.state === "separating" && current.separation_pass > 1) {
      stage = "Separating stems — pass " + current.separation_pass;
    }
    $("processing-stage").textContent = stage;

    var fill = $("processing-fill");
    if (typeof current.progress === "number") {
      fill.classList.remove("indeterminate");
      fill.style.width = current.progress.toFixed(1) + "%";
    } else {
      fill.classList.add("indeterminate");
      fill.style.width = "";
    }

    var parts = [];
    if (current.duration_seconds) parts.push(formatTime(current.duration_seconds) + " of audio");
    if (typeof current.elapsed_seconds === "number") parts.push(formatTime(current.elapsed_seconds) + " elapsed");
    $("processing-detail").textContent = parts.join(" · ");
  }

  async function pollJob() {
    if (!job) return;
    try {
      var current = await apiGet("/api/jobs/" + job.id);
      job = current;
      if (current.state === "done") {
        stopPolling();
        openMixer(current);
        return;
      }
      if (current.state === "error") {
        stopPolling();
        showView("view-failed");
        $("failed-message").textContent = current.error || "The job failed.";
        return;
      }
      showProcessing(current);
      pollTimer = setTimeout(pollJob, 1200);
    } catch (error) {
      stopPolling();
      showView("view-failed");
      $("failed-message").textContent = error.message;
    }
  }

  function updateTrackMeta(current) {
    if (!current) { $("track-meta").hidden = true; return; }
    $("track-meta").hidden = false;
    $("track-name").textContent = current.original_name || "track";
    var bits = [];
    if (current.duration_seconds) bits.push(formatTime(current.duration_seconds));
    if (current.sample_rate) bits.push((current.sample_rate / 1000).toFixed(1) + " kHz");
    if (current.input_bytes) bits.push(formatBytes(current.input_bytes));
    $("track-detail").textContent = bits.join(" · ");
  }

  /* ---------------------- mixer ---------------------- */

  async function openMixer(current) {
    // Opening a second job over a first must not leave the first one playing.
    stop();
    engine.channels = [];
    strips = [];
    stemFaders = [];
    $("strips").textContent = "";
    updatePlayButton();

    showView("view-mixer");
    $("new-track-button").hidden = false;
    $("copy-link-button").hidden = false;
    $("mixer").hidden = true;
    $("mixer-loading").hidden = false;
    $("load-fill").style.width = "0%";
    $("load-status").textContent = "Loading stems…";
    updateTrackMeta(current);

    try {
      await loadStems(current.id, current.stems, function (fraction) {
        $("load-fill").style.width = (fraction * 100).toFixed(0) + "%";
        $("load-status").textContent =
          "Loading stems… " + Math.round(fraction * current.stems.length) +
          " of " + current.stems.length;
      });
    } catch (error) {
      showView("view-failed");
      $("failed-message").textContent = error.message;
      return;
    }

    var restored = restoreMix(current.id);
    buildStrips(current);
    if (restored) {
      applyGains();
      toast("Picked up the balance you left on this track.");
    }
    $("download-zip").href = "/api/jobs/" + current.id + "/stems.zip";
    $("time-total").textContent = formatTime(engine.duration);
    $("export-status").textContent = "";
    $("instrumental-status").textContent = "";
    $("mixer-loading").hidden = true;
    $("mixer").hidden = false;
    requestAnimationFrame(function () {
      strips.forEach(function (strip) { drawWaveform(strip.canvas, strip.channel); });
    });
    startRenderLoop();
  }

  function buildStrips(current) {
    var container = $("strips");
    container.textContent = "";
    strips = [];
    stemFaders = [];

    engine.channels.forEach(function (channel, index) {
      var colour = STEM_COLOURS[channel.name] || "#6ea8fe";
      var strip = make("div", "strip");
      strip.style.setProperty("--stem", colour);

      var head = make("div", "strip-head");
      head.appendChild(make("span", "strip-dot"));
      head.appendChild(make("span", "strip-name", channel.name));

      var stemLink = make("a", "strip-download", "WAV");
      stemLink.href = "/api/jobs/" + current.id + "/stems/" + channel.name + ".wav";
      stemLink.setAttribute("download", "");
      stemLink.setAttribute("aria-label", "Download the " + channel.name + " stem");
      head.appendChild(stemLink);
      strip.appendChild(head);

      var waveWrap = make("div", "wave-wrap");
      var canvas = make("canvas");
      var playhead = make("div", "playhead");
      waveWrap.appendChild(canvas);
      waveWrap.appendChild(playhead);
      strip.appendChild(waveWrap);

      var faderEl = make("div", "fader");
      strip.appendChild(faderEl);

      var controls = make("div", "strip-controls");
      var readout = make("span", "mono strip-readout", formatDb(channel.pos));
      controls.appendChild(readout);

      var toggles = make("div", "strip-toggles");
      var muteButton = make("button", "toggle-button mute", "M");
      muteButton.type = "button";
      muteButton.setAttribute("aria-pressed", String(channel.mute));
      muteButton.setAttribute("aria-label", "Mute " + channel.name);
      var soloButton = make("button", "toggle-button solo", "S");
      soloButton.type = "button";
      soloButton.setAttribute("aria-pressed", String(channel.solo));
      soloButton.setAttribute("aria-label", "Solo " + channel.name);
      toggles.appendChild(muteButton);
      toggles.appendChild(soloButton);
      controls.appendChild(toggles);
      strip.appendChild(controls);

      container.appendChild(strip);

      var fader = new Fader(faderEl, {
        orientation: desktopQuery.matches ? "v" : "h",
        value: channel.pos,
        defaultValue: UNITY_POS,
        colour: colour,
        label: channel.name + " level",
        onInput: function (value) {
          channel.pos = value;
          readout.textContent = formatDb(value);
          applyGains();
          refreshStripStates();
          scheduleSave();
        }
      });
      stemFaders.push(fader);

      muteButton.addEventListener("click", function () {
        channel.mute = !channel.mute;
        muteButton.setAttribute("aria-pressed", String(channel.mute));
        applyGains();
        refreshStripStates();
        scheduleSave();
      });

      soloButton.addEventListener("click", function () {
        channel.solo = !channel.solo;
        soloButton.setAttribute("aria-pressed", String(channel.solo));
        applyGains();
        refreshStripStates();
        scheduleSave();
      });

      attachSeek(waveWrap, function (fraction) { seek(fraction * engine.duration); });

      strips.push({
        channel: channel,
        element: strip,
        canvas: canvas,
        playhead: playhead,
        wrap: waveWrap,
        index: index
      });
    });

    refreshStripStates();

    if (window.ResizeObserver) {
      var observer = new ResizeObserver(function () {
        strips.forEach(function (strip) { drawWaveform(strip.canvas, strip.channel); });
      });
      strips.forEach(function (strip) { observer.observe(strip.wrap); });
    } else {
      window.addEventListener("resize", function () {
        strips.forEach(function (strip) { drawWaveform(strip.canvas, strip.channel); });
      });
    }
  }

  function refreshStripStates() {
    strips.forEach(function (strip) {
      strip.element.classList.toggle("inaudible", effectiveGain(strip.channel) === 0);
    });
  }

  function attachSeek(element, onSeek) {
    var dragging = false;
    function fractionFrom(event) {
      var rect = element.getBoundingClientRect();
      return rect.width ? clamp((event.clientX - rect.left) / rect.width, 0, 1) : 0;
    }
    element.addEventListener("pointerdown", function (event) {
      dragging = true;
      element.setPointerCapture(event.pointerId);
      onSeek(fractionFrom(event));
      event.preventDefault();
    });
    element.addEventListener("pointermove", function (event) {
      if (dragging) onSeek(fractionFrom(event));
    });
    element.addEventListener("pointerup", function (event) {
      dragging = false;
      if (element.hasPointerCapture(event.pointerId)) element.releasePointerCapture(event.pointerId);
    });
    element.addEventListener("pointercancel", function () { dragging = false; });
  }

  function startRenderLoop() {
    if (engine.frame) cancelAnimationFrame(engine.frame);
    var lastFraction = -1;

    function tick() {
      engine.frame = requestAnimationFrame(tick);
      var time = currentTime();

      if (engine.playing && time >= engine.duration - 0.005) {
        if (engine.loop) { seek(0); }
        else { stop(); updatePlayButton(); }
      }

      var fraction = engine.duration ? time / engine.duration : 0;
      // Paused, the playhead does not move; skip the layout work entirely
      // rather than rewriting five elements sixty times a second.
      if (Math.abs(fraction - lastFraction) < 1e-5) return;
      lastFraction = fraction;

      $("scrub-fill").style.width = (fraction * 100).toFixed(3) + "%";
      $("time-current").textContent = formatTime(time);
      strips.forEach(function (strip) {
        var width = strip.wrap.clientWidth;
        strip.playhead.style.transform = "translateX(" + (fraction * width).toFixed(2) + "px)";
      });
    }
    tick();
  }

  function updatePlayButton() {
    var button = $("play-button");
    button.textContent = "";
    var icon = make("span", engine.playing ? "icon-pause" : "icon-play");
    icon.setAttribute("aria-hidden", "true");
    button.appendChild(icon);
    button.setAttribute("aria-label", engine.playing ? "Pause" : "Play");
  }

  async function togglePlay() {
    if (engine.playing) pause();
    else await play();
    updatePlayButton();
  }

  /* ---------------------- export ---------------------- */

  var ALL_EXPORT_BUTTON_IDS = ["export-wav", "export-mp3", "export-instrumental"];

  /* `options.gains` fixes the balance regardless of the live faders (used by
   * the instrumental export); omitted, it reads the faders/mute/solo as they
   * currently stand, matching what's audible in the mixer right now. */
  async function exportMix(format, options) {
    if (!job || !engine.channels.length) return;
    options = options || {};
    var gains = options.gains;
    if (!gains) {
      gains = {};
      engine.channels.forEach(function (channel) {
        gains[channel.name] = effectiveGain(channel);
      });
    }
    var suffix = options.suffix || "mix";
    var statusEl = $(options.statusId || "export-status");

    statusEl.textContent = "Rendering " + format.toUpperCase() + "…";
    ALL_EXPORT_BUTTON_IDS.forEach(function (id) {
      var button = $(id);
      if (button) button.disabled = true;
    });
    try {
      var result = await apiPost("/api/jobs/" + job.id + "/mix", {
        gains: gains,
        format: format
      });
      var note = formatBytes(result.bytes);
      if (result.attenuation_db && result.attenuation_db < 0) {
        note += " · pulled back " + Math.abs(result.attenuation_db).toFixed(1) +
          " dB to stay under full scale";
      }
      statusEl.textContent = note;
      download(result.url, (job.original_name || "track") + " - " + suffix + "." + format);
    } catch (error) {
      statusEl.textContent = "";
      if (error.unauthorised) {
        var supplied = await askForKey("Exporting needs the access key.");
        if (supplied) exportMix(format, options);
        return;
      }
      toast(error.message, true);
    } finally {
      ALL_EXPORT_BUTTON_IDS.forEach(function (id) {
        var button = $(id);
        if (button) button.disabled = false;
      });
    }
  }

  function exportInstrumental() {
    if (!engine.channels.length) return;
    var gains = {};
    engine.channels.forEach(function (channel) {
      gains[channel.name] = channel.name === "vocals" ? 0 : 1;
    });
    exportMix("wav", {
      gains: gains,
      suffix: "instrumental",
      statusId: "instrumental-status"
    });
  }

  /* ---------------------- keyboard ---------------------- */

  function isTyping(target) {
    if (!target) return false;
    var tag = target.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || target.isContentEditable;
  }

  function onKeyDown(event) {
    if (isTyping(event.target)) return;
    if (!$("key-dialog").hidden) {
      if (event.key === "Escape") closeKeyDialog(null);
      return;
    }
    if ($("view-mixer").hidden || $("mixer").hidden) return;
    if (event.target.getAttribute && event.target.getAttribute("role") === "slider") {
      if (event.key !== " ") return;
    }

    if (event.key === " ") { togglePlay(); event.preventDefault(); return; }
    if (event.key === "ArrowLeft") { seek(currentTime() - 5); event.preventDefault(); return; }
    if (event.key === "ArrowRight") { seek(currentTime() + 5); event.preventDefault(); return; }

    var index = "1234".indexOf(event.key);
    if (index >= 0 && index < engine.channels.length) {
      var channel = engine.channels[index];
      var strip = strips[index];
      if (event.shiftKey) {
        channel.mute = !channel.mute;
        strip.element.querySelector(".mute").setAttribute("aria-pressed", String(channel.mute));
      } else {
        channel.solo = !channel.solo;
        strip.element.querySelector(".solo").setAttribute("aria-pressed", String(channel.solo));
      }
      applyGains();
      refreshStripStates();
      scheduleSave();
      event.preventDefault();
    }
  }

  /* ---------------------- wiring ---------------------- */

  function wireUpload() {
    var zone = $("dropzone");
    var input = $("file-input");

    $("browse-button").addEventListener("click", function () { input.click(); });
    zone.addEventListener("click", function (event) {
      if (event.target === $("browse-button")) return;
      input.click();
    });
    input.addEventListener("change", function () {
      if (input.files && input.files[0]) beginUpload(input.files[0]);
    });

    ["dragenter", "dragover"].forEach(function (name) {
      zone.addEventListener(name, function (event) {
        event.preventDefault();
        zone.classList.add("dragging");
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      zone.addEventListener(name, function (event) {
        event.preventDefault();
        zone.classList.remove("dragging");
      });
    });
    zone.addEventListener("drop", function (event) {
      var files = event.dataTransfer && event.dataTransfer.files;
      if (files && files[0]) beginUpload(files[0]);
    });
  }

  function wireTransport() {
    $("play-button").addEventListener("click", togglePlay);
    $("stop-button").addEventListener("click", function () {
      stop();
      updatePlayButton();
    });
    $("loop-button").addEventListener("click", function () {
      engine.loop = !engine.loop;
      $("loop-button").setAttribute("aria-pressed", String(engine.loop));
    });
    attachSeek($("scrub"), function (fraction) { seek(fraction * engine.duration); });

    masterFader = new Fader($("master-fader"), {
      orientation: "h",
      value: UNITY_POS,
      defaultValue: UNITY_POS,
      label: "Monitor level",
      onInput: function (value) {
        engine.masterPos = value;
        $("master-readout").textContent = formatDb(value);
        applyGains();
        scheduleSave();
      }
    });
    $("master-readout").textContent = formatDb(UNITY_POS);

    $("export-wav").addEventListener("click", function () { exportMix("wav"); });
    $("export-mp3").addEventListener("click", function () { exportMix("mp3"); });
    $("export-instrumental").addEventListener("click", exportInstrumental);
  }

  function wireDialog() {
    $("key-button").addEventListener("click", function () { askForKey(""); });
    $("key-cancel").addEventListener("click", function () { closeKeyDialog(null); });
    $("key-input").addEventListener("keydown", function (event) {
      if (event.key === "Enter") $("key-save").click();
      if (event.key === "Escape") closeKeyDialog(null);
    });
    $("key-save").addEventListener("click", async function () {
      var candidate = $("key-input").value.trim();
      if (!candidate) { closeKeyDialog(null); return; }
      $("key-save").disabled = true;
      var ok = false;
      try { ok = await verifyKey(candidate); } catch (e) { ok = false; }
      $("key-save").disabled = false;
      if (!ok) {
        $("key-error").hidden = false;
        $("key-error").textContent = "That key was not accepted.";
        return;
      }
      storeKey(candidate);
      closeKeyDialog(candidate);
      toast("Access key saved.");
    });
  }

  function wireChrome() {
    $("new-track-button").addEventListener("click", resetToUpload);
    $("copy-link-button").addEventListener("click", copyJobLink);
    $("failed-retry").addEventListener("click", resetToUpload);
    $("processing-cancel").addEventListener("click", async function () {
      var id = job && job.id;
      resetToUpload();
      if (!id) return;
      try {
        await fetch("/api/jobs/" + id, {
          method: "DELETE",
          headers: { "X-Stem-Key": accessKey }
        });
      } catch (e) { /* the sweeper will collect it */ }
    });

    document.addEventListener("keydown", onKeyDown);

    var onLayoutChange = function () {
      var orientation = desktopQuery.matches ? "v" : "h";
      stemFaders.forEach(function (fader) { fader.setOrientation(orientation); });
      strips.forEach(function (strip) { drawWaveform(strip.canvas, strip.channel); });
    };
    if (desktopQuery.addEventListener) desktopQuery.addEventListener("change", onLayoutChange);
    else desktopQuery.addListener(onLayoutChange);
  }

  /* ---------------------- boot ---------------------- */

  async function init() {
    wireUpload();
    wireTransport();
    wireDialog();
    wireChrome();
    updatePlayButton();

    try {
      config = await apiGet("/api/config");
    } catch (error) {
      toast("Could not reach the server: " + error.message, true);
      return;
    }

    $("upload-hint").textContent =
      config.accepted_suffixes.map(function (s) { return s.slice(1); }).join(" ") +
      " · up to " + config.max_upload_mb + " MB · up to " +
      Math.round(config.max_duration_seconds / 60) + " minutes · kept for " +
      config.job_ttl_hours + " hours";
    $("key-button").hidden = !config.requires_key;

    window.addEventListener("hashchange", function () { openFromHash(); });
    if (await openFromHash()) return;

    // A PWA launched from its home-screen icon always lands on the bare
    // start_url with no hash -- the manifest spec gives it nowhere else to
    // go, so iOS and Android do not restore whatever page was open when the
    // app was last closed. Without this fallback, every relaunch loses the
    // job outright, even though its balance is still sitting in
    // localStorage from the fader-memory feature. Silent on failure (no
    // toast): the user did not navigate to a link, so an unprompted "that
    // expired" message on a cold launch would read as an error rather than
    // the routine cleanup it is.
    var recent = mostRecentJobId();
    if (recent && (await resumeJob(recent, { announceFailure: false }))) {
      history.replaceState(null, "", window.location.pathname + "#job=" + recent);
      return;
    }
    showView("view-upload");
  }

  function mostRecentJobId() {
    var all = readMixes();
    var ids = Object.keys(all);
    if (!ids.length) return null;
    ids.sort(function (a, b) { return (all[b].at || 0) - (all[a].at || 0); });
    return ids[0];
  }

  /* Shared by openFromHash (an explicit link, where a dead job is worth
   * announcing) and the cold-launch fallback above (where it is not). */
  async function resumeJob(jobId, options) {
    options = options || {};
    stopPolling();
    try {
      var existing = await apiGet("/api/jobs/" + jobId);
      job = existing;
      if (existing.state === "done") { await openMixer(existing); return true; }
      if (existing.state === "error") {
        showView("view-failed");
        $("failed-message").textContent = existing.error || "The job failed.";
        return true;
      }
      showProcessing(existing);
      pollTimer = setTimeout(pollJob, 900);
      return true;
    } catch (error) {
      if (options.announceFailure) {
        toast("That link has expired.", true);
        history.replaceState(null, "", window.location.pathname);
      }
      return false;
    }
  }

  /* Open whatever job the fragment names. Changing the fragment on an already
   * loaded page is a same-document navigation, so this runs both at boot and
   * from the hashchange event; without the second, pasting a job link into an
   * open tab would do nothing at all. */
  async function openFromHash() {
    var match = /(?:^|#|&)job=([A-Za-z0-9_-]{16,64})/.exec(window.location.hash);
    if (!match) return false;
    if (job && job.id === match[1] && !$("view-mixer").hidden) return true;
    return resumeJob(match[1], { announceFailure: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
