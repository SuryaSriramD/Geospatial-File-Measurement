"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const PAGE_SIZE = 25;
  const STORAGE_KEY = "geomeasure.lastFile";
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const state = { file: null, busy: false, epoch: 0, job: null, fileId: null, metadata: null, page: null, offset: 0, loadingPage: false, timer: null, retry: null, maxUpload: null };
  const stepDefaults = { validate: "Check file format, size, and contents.", parse: "Extract geometry, properties, and source CRS.", measure: "Use a local projection for area and length.", save: "Store the file and feature measurements." };
  const statusLabels = { CALCULATED: "Calculated", NOT_APPLICABLE: "Not applicable", UNSUPPORTED: "Unsupported", INVALID: "Invalid" };
  const statusClasses = { CALCULATED: "calculated", NOT_APPLICABLE: "not-applicable", UNSUPPORTED: "unsupported", INVALID: "invalid" };
  const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
  const integer = new Intl.NumberFormat();
  let lastViewButton = null;

  function storageGet() { try { const id = localStorage.getItem(STORAGE_KEY); return id && UUID.test(id) ? id : null; } catch { return null; } }
  function storageSet(id) { try { localStorage.setItem(STORAGE_KEY, id); } catch { /* Results remain usable without browser storage. */ } }
  function showError(message, retry = null, title = "Something needs attention") {
    $("error-title").textContent = title;
    $("error-message").textContent = message;
    $("error-banner").hidden = false;
    $("retry-button").hidden = !retry;
    state.retry = retry;
  }
  function clearError() { $("error-banner").hidden = true; $("retry-button").hidden = true; state.retry = null; }
  function messageFrom(data, fallback) {
    const detail = data?.detail ?? data?.error ?? data;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail.message === "string") return detail.message;
    if (Array.isArray(detail)) return detail.map((item) => item.msg || "Invalid request").join("; ");
    return fallback;
  }
  async function requestJSON(url, options = {}) {
    let response;
    try { response = await fetch(url, { cache: "no-store", ...options }); } catch { throw new Error("Could not connect to the API. Check that the server is running, then retry."); }
    let data;
    try { data = await response.json(); } catch { throw new Error(`The API returned an unreadable response (${response.status}).`); }
    if (!response.ok) throw new Error(messageFrom(data, `The request failed (${response.status}).`));
    return data;
  }
  function setBusy(busy) {
    state.busy = busy;
    $("file-input").disabled = busy;
    $("browse-label").setAttribute("aria-disabled", String(busy));
    $("clear-file").disabled = busy;
    $("process-button").disabled = busy || !state.file;
    $("sample-button").disabled = busy;
    $("restore-button").disabled = busy;
  }
  function setBadge(text, className = "neutral") { $("pipeline-badge").textContent = text; $("pipeline-badge").className = `pill ${className}`; }
  function resetPipeline() {
    document.querySelectorAll("[data-step]").forEach((node, i) => {
      node.dataset.status = "PENDING";
      node.querySelector(".step-marker").textContent = String(i + 1);
      node.querySelector(".step-time").textContent = "";
      node.querySelector(".step-detail").textContent = stepDefaults[node.dataset.step];
      node.querySelector(".step-status").textContent = "Pending";
    });
    setBadge("READY");
    $("pipeline-message").textContent = "Waiting for your file";
    $("pipeline-description").textContent = "Four steps from source data to results.";
  }
  function resetResults() {
    state.fileId = null; state.metadata = null; state.page = null; state.offset = 0; state.loadingPage = false;
    $("results").hidden = true;
    $("empty-results").hidden = false;
    $("feature-detail").hidden = true;
    $("export-button").removeAttribute("href");
    $("restore-button").hidden = !storageGet();
    history.replaceState(null, "", location.pathname + location.search);
  }
  function humanSize(bytes) { return bytes < 1024 * 1024 ? `${number.format(bytes / 1024)} KB` : `${number.format(bytes / (1024 * 1024))} MB`; }
  function selectFile(file) {
    if (state.busy) return;
    state.epoch += 1; clearTimeout(state.timer); state.job = null; clearError(); resetPipeline(); resetResults();
    state.file = null;
    if (file && !/\.(kml|zip)$/i.test(file.name)) { showError("Choose a .kml file or a .zip containing a Shapefile."); file = null; }
    if (file && file.size === 0) { showError("This file is empty. Choose a file containing geospatial data."); file = null; }
    if (file && state.maxUpload && file.size > state.maxUpload) { showError(`This file exceeds the ${humanSize(state.maxUpload)} upload limit.`); file = null; }
    state.file = file;
    $("selected-file").hidden = !file;
    $("drop-zone").classList.toggle("is-selected", !!file);
    $("selected-name").textContent = file ? file.name : "";
    $("selected-size").textContent = file ? `${humanSize(file.size)} · ${/\.kml$/i.test(file.name) ? "KML" : "Zipped Shapefile"}` : "";
    $("drop-title").textContent = file ? "Your file is ready" : "Drop a geospatial file here";
    $("drop-caption").textContent = file ? "Choose another file to replace it" : "or choose one from your computer";
    $("upload-status").textContent = file ? "Ready when you are. Select Process file to begin." : "Choose a file to get started.";
    $("file-input").value = "";
    setBusy(false);
  }
  function upload(file, epoch) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/jobs/");
      xhr.responseType = "json";
      xhr.timeout = 120000;
      xhr.upload.onprogress = (event) => {
        if (epoch !== state.epoch) return;
        $("upload-status").textContent = event.lengthComputable ? `Uploading file · ${Math.round(event.loaded / event.total * 100)}%` : "Uploading file…";
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300 && xhr.response) resolve(xhr.response);
        else reject(new Error(messageFrom(xhr.response, `Upload failed (${xhr.status}).`)));
      };
      xhr.onerror = () => reject(new Error("Could not upload the file. Check that the server is running, then try again."));
      xhr.ontimeout = () => reject(new Error("The upload timed out. Check your connection and try again."));
      const form = new FormData(); form.append("file", file); xhr.send(form);
    });
  }
  async function processFile() {
    if (!state.file || state.busy) return;
    const epoch = ++state.epoch;
    clearError(); resetPipeline(); resetResults(); setBusy(true);
    setBadge("UPLOADING", "running");
    $("pipeline-message").textContent = "Sending file to the API";
    $("upload-status").textContent = "Uploading file…";
    try {
      const job = await upload(state.file, epoch);
      if (epoch !== state.epoch) return;
      state.job = job;
      $("upload-status").textContent = "Upload received. Following the processing job.";
      await handleJob(job, epoch);
    } catch (error) {
      if (epoch !== state.epoch) return;
      setBusy(false); setBadge("UPLOAD FAILED", "failed");
      $("pipeline-message").textContent = "The file could not be submitted";
      $("upload-status").textContent = "Upload failed. You can try processing the file again.";
      showError(error.message);
    }
  }
  function renderJob(job) {
    (job.steps || []).forEach((step) => {
      const node = Array.from(document.querySelectorAll("[data-step]")).find((item) => item.dataset.step === step.key);
      if (!node) return;
      node.dataset.status = step.status;
      const marker = step.status === "COMPLETED" ? "✓" : step.status === "FAILED" ? "!" : String(Object.keys(stepDefaults).indexOf(step.key) + 1);
      node.querySelector(".step-marker").textContent = marker;
      node.querySelector(".step-detail").textContent = step.detail || stepDefaults[step.key];
      node.querySelector(".step-status").textContent = step.status.toLowerCase();
      node.querySelector(".step-time").textContent = step.duration_ms == null ? (step.status === "RUNNING" ? "IN PROGRESS" : "") : (step.duration_ms < 1000 ? `${Math.round(step.duration_ms)} ms` : `${number.format(step.duration_ms / 1000)} s`);
    });
    const measuring = job.steps?.some((step) => step.key === "measure" && step.status === "RUNNING");
    setBadge(job.status, job.status === "FAILED" ? "failed" : job.status === "COMPLETED" ? "complete" : "running");
    $("pipeline-message").textContent = job.status === "COMPLETED" ? "Processing complete · results saved" : job.status === "FAILED" ? "Processing stopped · see the error above" : job.status === "QUEUED" ? "Queued · waiting for processing" : measuring && job.total != null ? `${integer.format(job.processed)} / ${integer.format(job.total)} features measured` : "Processing on the server";
  }
  async function handleJob(job, epoch) {
    if (epoch !== state.epoch) return;
    state.job = job; renderJob(job);
    if (job.status === "FAILED") {
      setBusy(false);
      $("upload-status").textContent = "Processing failed. Review the message and choose another file or retry.";
      showError(job.error?.message || "The server could not process this file.");
      return;
    }
    if (job.status === "COMPLETED") {
      $("upload-status").textContent = "File processed. Loading your results…";
      try {
        await loadResults(job.file_id, epoch, true);
        if (epoch !== state.epoch) return;
        $("upload-status").textContent = "Complete. Your results are ready below.";
        setBusy(false);
      } catch (error) {
        if (epoch !== state.epoch) return;
        setBusy(false);
        $("upload-status").textContent = "Processing is complete. Results could not be loaded.";
        showError(error.message, () => resumeJob(job.id));
      }
      return;
    }
    state.timer = setTimeout(() => pollJob(job.id, epoch), 650);
  }
  async function pollJob(id, epoch) {
    if (epoch !== state.epoch) return;
    try { const job = await requestJSON(`/api/jobs/${encodeURIComponent(id)}/`); await handleJob(job, epoch); }
    catch (error) {
      if (epoch !== state.epoch) return;
      setBusy(false); setBadge("CONNECTION LOST", "failed");
      $("pipeline-message").textContent = "Status updates paused · processing may still be running";
      $("upload-status").textContent = "Connection interrupted. Retry to recover this job’s status.";
      showError(error.message, () => resumeJob(id), "Could not refresh processing status");
    }
  }
  function resumeJob(id) { clearError(); setBusy(true); setBadge("RECONNECTING", "running"); pollJob(id, state.epoch); }
  async function loadResults(id, epoch, focus = false) {
    if (!id || !UUID.test(id)) throw new Error("The API did not return a valid file ID.");
    const [metadata, page] = await Promise.all([requestJSON(`/api/files/${encodeURIComponent(id)}/`), requestJSON(`/api/files/${encodeURIComponent(id)}/measurements/?limit=${PAGE_SIZE}&offset=0`)]);
    if (epoch !== state.epoch) return;
    state.fileId = id; state.metadata = metadata; state.page = page; state.offset = 0;
    $("export-button").href = `/api/files/${encodeURIComponent(id)}/export/`;
    $("results-title").textContent = metadata.filename;
    let dateText = "";
    if (metadata.created_at) { const date = new Date(metadata.created_at); if (!Number.isNaN(date.getTime())) dateText = ` · Saved ${date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}`; }
    $("result-context").textContent = `${integer.format(metadata.feature_count)} features${dateText}`;
    const summary = metadata.measurement_summary;
    $("stat-total").textContent = integer.format(metadata.feature_count);
    $("stat-calculated").textContent = integer.format(summary.calculated);
    $("stat-not-applicable").textContent = integer.format(summary.not_applicable);
    $("stat-attention").textContent = integer.format(summary.unsupported + summary.invalid);
    $("attention-description").textContent = `${integer.format(summary.unsupported)} unsupported · ${integer.format(summary.invalid)} invalid`;
    $("source-crs").textContent = metadata.crs || "Not recorded";
    $("output-crs").textContent = `${metadata.geometry_crs || "EPSG:4326"} · WGS84`;
    renderPage();
    $("empty-results").hidden = true; $("results").hidden = false;
    storageSet(id); history.replaceState(null, "", `#file=${encodeURIComponent(id)}`);
    if (focus) $("results-title").focus({ preventScroll: true });
  }
  function textCell(row, content, className = "") { const cell = document.createElement("td"); cell.textContent = content; if (className) cell.className = className; row.append(cell); return cell; }
  function featureName(item) { const name = item.properties?.name; return name != null && String(name).trim() ? String(name) : item.feature_id || `Feature ${item.index + 1}`; }
  function numeric(value) { return value == null || !Number.isFinite(value) ? "—" : number.format(value); }
  function renderPage() {
    const page = state.page;
    const body = $("features-body"); body.replaceChildren();
    $("feature-detail").hidden = true; lastViewButton = null;
    page.items.forEach((item) => {
      const row = document.createElement("tr");
      const nameCell = document.createElement("td");
      const name = document.createElement("span"); name.className = "feature-name"; name.textContent = featureName(item); name.title = featureName(item);
      const id = document.createElement("span"); id.className = "feature-id"; id.textContent = `#${item.index + 1} · ${item.feature_id}`;
      nameCell.append(name, id); row.append(nameCell);
      textCell(row, item.geometry_type || "Unknown");
      textCell(row, numeric(item.measurement.area_m2), "numeric");
      textCell(row, numeric(item.measurement.length_m), "numeric");
      const statusCell = document.createElement("td"); const badge = document.createElement("span"); badge.className = `pill ${statusClasses[item.measurement.status] || "neutral"}`; badge.textContent = statusLabels[item.measurement.status] || item.measurement.status; statusCell.append(badge); row.append(statusCell);
      const actionCell = document.createElement("td"); const button = document.createElement("button"); button.type = "button"; button.className = "table-view"; button.textContent = "View ↗"; button.setAttribute("aria-label", `View details for ${featureName(item)}`); button.addEventListener("click", () => { body.querySelectorAll("tr").forEach((entry) => entry.classList.remove("selected")); row.classList.add("selected"); lastViewButton = button; showFeature(item); }); actionCell.append(button); row.append(actionCell); body.append(row);
    });
    if (!page.items.length) { const row = document.createElement("tr"); const cell = textCell(row, "This file contains no features.", "empty-table"); cell.colSpan = 6; body.append(row); }
    $("table-count").textContent = `${integer.format(page.total)} TOTAL`;
    const start = page.total ? state.offset + 1 : 0;
    $("pagination-label").textContent = `${integer.format(start)}–${integer.format(Math.min(state.offset + page.items.length, page.total))} of ${integer.format(page.total)}`;
    updatePagination();
  }
  function updatePagination() {
    $("previous-button").disabled = state.loadingPage || state.offset === 0;
    $("next-button").disabled = state.loadingPage || !state.page || state.offset + state.page.items.length >= state.page.total;
  }
  async function changePage(offset) {
    if (state.loadingPage || !state.fileId) return;
    const epoch = state.epoch; const id = state.fileId;
    state.loadingPage = true; updatePagination(); clearError();
    $("table-count").textContent = "LOADING…";
    try {
      const page = await requestJSON(`/api/files/${encodeURIComponent(id)}/measurements/?limit=${PAGE_SIZE}&offset=${offset}`);
      if (epoch !== state.epoch) return;
      state.page = page; state.offset = offset; renderPage();
    } catch (error) { if (epoch === state.epoch) { showError(error.message, () => changePage(offset)); $("table-count").textContent = `${integer.format(state.page.total)} TOTAL`; } }
    finally { if (epoch === state.epoch) { state.loadingPage = false; updatePagination(); } }
  }
  function showFeature(item) {
    $("detail-title").textContent = featureName(item);
    $("detail-status").className = `pill ${statusClasses[item.measurement.status] || "neutral"}`;
    $("detail-status").textContent = statusLabels[item.measurement.status] || item.measurement.status;
    const measurement = item.measurement;
    let explanation = measurement.reason || "No measurement is available for this feature.";
    if (measurement.status === "CALCULATED") {
      const values = [];
      if (measurement.area_m2 != null) values.push(`Area: ${number.format(measurement.area_m2)} m²`);
      if (measurement.length_m != null) values.push(`Length: ${number.format(measurement.length_m)} m`);
      explanation = `${values.join(" · ")}. Calculated using ${measurement.crs || "a local projected coordinate system"}.`;
    }
    $("detail-explanation").textContent = explanation;
    $("detail-id").textContent = item.feature_id;
    $("detail-type").textContent = item.geometry_type || "Unknown";
    $("detail-crs").textContent = measurement.crs || "Not applicable";
    $("detail-geometry-crs").textContent = item.crs || "Not recorded";
    $("detail-properties").textContent = JSON.stringify(item.properties, null, 2);
    $("detail-geometry").textContent = JSON.stringify(item.geometry, null, 2);
    $("feature-detail").hidden = false;
    $("detail-title").focus({ preventScroll: true });
    $("feature-detail").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "nearest" });
  }
  async function openSaved(id) {
    if (state.busy || !id) return;
    const epoch = ++state.epoch; clearTimeout(state.timer); clearError(); resetPipeline(); resetResults(); setBusy(true);
    setBadge("LOADING", "running"); $("pipeline-message").textContent = "Retrieving a saved result";
    try {
      await loadResults(id, epoch, true);
      if (epoch !== state.epoch) return;
      setBadge("SAVED RESULT", "complete");
      $("pipeline-message").textContent = "Saved result loaded · step timing unavailable";
      $("pipeline-description").textContent = "This result was processed previously. Upload a file to follow a new job.";
    } catch (error) {
      if (epoch !== state.epoch) return;
      setBadge("LOAD FAILED", "failed"); $("pipeline-message").textContent = "Could not retrieve the saved result";
      showError(error.message, () => openSaved(id));
    } finally { if (epoch === state.epoch) setBusy(false); }
  }

  $("file-input").addEventListener("change", (event) => { if (event.target.files[0]) selectFile(event.target.files[0]); });
  $("clear-file").addEventListener("click", () => selectFile(null));
  $("process-button").addEventListener("click", processFile);
  $("browse-label").addEventListener("click", (event) => { if (state.busy) event.preventDefault(); });
  $("dismiss-error").addEventListener("click", clearError);
  $("retry-button").addEventListener("click", () => { const retry = state.retry; if (retry) retry(); });
  $("previous-button").addEventListener("click", () => changePage(Math.max(0, state.offset - PAGE_SIZE)));
  $("next-button").addEventListener("click", () => changePage(state.offset + PAGE_SIZE));
  $("restore-button").addEventListener("click", () => openSaved(storageGet()));
  $("close-detail").addEventListener("click", () => { $("feature-detail").hidden = true; $("features-body").querySelectorAll("tr").forEach((row) => row.classList.remove("selected")); lastViewButton?.focus(); });
  $("sample-button").addEventListener("click", async () => {
    if (state.busy) return;
    const epoch = ++state.epoch; clearError(); setBusy(true); $("upload-status").textContent = "Loading the sample file…";
    try {
      const response = await fetch("/api/example-file", { cache: "no-store" });
      if (!response.ok) throw new Error("The sample file could not be loaded. Try again or choose a file from your computer.");
      const blob = await response.blob();
      if (epoch !== state.epoch) return;
      setBusy(false); selectFile(new File([blob], "sample.kml", { type: "application/vnd.google-earth.kml+xml" })); await processFile();
    } catch (error) { if (epoch === state.epoch) { setBusy(false); $("upload-status").textContent = "The sample could not be loaded."; showError(error.message); } }
  });
  let dragDepth = 0;
  const dropZone = $("drop-zone");
  dropZone.addEventListener("dragenter", (event) => { event.preventDefault(); if (!state.busy) { dragDepth += 1; dropZone.classList.add("dragging"); } });
  dropZone.addEventListener("dragover", (event) => { event.preventDefault(); event.dataTransfer.dropEffect = state.busy ? "none" : "copy"; });
  dropZone.addEventListener("dragleave", (event) => { event.preventDefault(); dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) dropZone.classList.remove("dragging"); });
  dropZone.addEventListener("drop", (event) => { event.preventDefault(); dragDepth = 0; dropZone.classList.remove("dragging"); if (state.busy) return; if (event.dataTransfer.files.length !== 1) { showError("Please choose one geospatial file at a time."); return; } selectFile(event.dataTransfer.files[0]); });
  document.addEventListener("dragover", (event) => { if (Array.from(event.dataTransfer.types).includes("Files")) event.preventDefault(); });
  document.addEventListener("drop", (event) => { if (Array.from(event.dataTransfer.types).includes("Files")) event.preventDefault(); });

  $("restore-button").hidden = !storageGet();
  requestJSON("/api/config").then((config) => {
    state.maxUpload = config.max_upload_bytes;
    $("file-limit").textContent = `.kml or .zip · Up to ${humanSize(config.max_upload_bytes)}`;
  }).catch(() => { /* The server remains the authority for upload limits. */ });
  function openResultFromLink() {
    const id = new URLSearchParams(location.hash.slice(1)).get("file");
    if (!id || state.busy || id === state.fileId) return;
    if (UUID.test(id)) openSaved(id);
    else showError("This result link does not contain a valid file ID. Choose a file to start a new measurement.");
  }
  window.addEventListener("hashchange", openResultFromLink);
  openResultFromLink();
})();
