const API_BASE = window.location.origin; // same-origin by default; change if backend is hosted separately

const fileInput = document.getElementById("file-input");
const dropzone = document.getElementById("dropzone");
const fileListEl = document.getElementById("file-list");
const statusEl = document.getElementById("status");
const resultsSection = document.getElementById("results");
const resultsBody = document.getElementById("results-body");
const resultsTitle = document.getElementById("results-title");
const resultsMeta = document.getElementById("results-meta");
const footerTime = document.getElementById("footer-time");

const btnRaw = document.getElementById("btn-raw");
const btnDecoded = document.getElementById("btn-decoded");
const btnExcel = document.getElementById("btn-excel");

let selectedFiles = [];

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function renderFileList() {
  if (!selectedFiles.length) {
    fileListEl.classList.add("hidden");
    fileListEl.innerHTML = "";
    setButtonsEnabled(false);
    return;
  }
  fileListEl.classList.remove("hidden");
  fileListEl.innerHTML = selectedFiles
    .map(
      (f, i) => `
      <div class="file-row">
        <span class="name">${escapeHtml(f.name)}</span>
        <span class="size">${formatSize(f.size)}</span>
        <button class="remove" data-index="${i}" title="Remove">&times;</button>
      </div>`
    )
    .join("");

  fileListEl.querySelectorAll(".remove").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      const idx = parseInt(e.currentTarget.dataset.index, 10);
      selectedFiles.splice(idx, 1);
      renderFileList();
    });
  });

  setButtonsEnabled(true);
}

function setButtonsEnabled(enabled) {
  btnRaw.disabled = !enabled;
  btnDecoded.disabled = !enabled;
  btnExcel.disabled = !enabled;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function addFiles(fileArray) {
  const pdfs = fileArray.filter((f) => f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf"));
  const existingNames = new Set(selectedFiles.map((f) => f.name + f.size));
  pdfs.forEach((f) => {
    if (!existingNames.has(f.name + f.size)) selectedFiles.push(f);
  });
  renderFileList();
}

fileInput.addEventListener("change", (e) => {
  addFiles(Array.from(e.target.files));
  fileInput.value = ""; // allow re-selecting the same file later
});

["dragenter", "dragover"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.add("drag-over");
  })
);
["dragleave", "drop"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.remove("drag-over");
  })
);
dropzone.addEventListener("drop", (e) => {
  const files = Array.from(e.dataTransfer.files || []);
  addFiles(files);
});

function setStatus(message, showSpinner = false) {
  if (!message) {
    statusEl.classList.add("hidden");
    statusEl.innerHTML = "";
    return;
  }
  statusEl.classList.remove("hidden");
  statusEl.innerHTML = showSpinner ? `<span class="spinner"></span><span>${message}</span>` : `<span>${message}</span>`;
}

function buildFormData() {
  const fd = new FormData();
  selectedFiles.forEach((f) => fd.append("files", f));
  return fd;
}

async function runScan(mode) {
  if (!selectedFiles.length) return;
  setButtonsEnabled(false);
  resultsSection.classList.add("hidden");
  const start = performance.now();
  setStatus(`Scanning ${selectedFiles.length} file(s) for QR codes...`, true);

  try {
    if (mode === "excel") {
      const res = await fetch(`${API_BASE}/api/scan/excel`, { method: "POST", body: buildFormData() });
      if (!res.ok) {
        const err = await safeJson(res);
        throw new Error(err?.error || `Server error (${res.status})`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "invoice_data.xlsx";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);

      const elapsed = ((performance.now() - start) / 1000).toFixed(2);
      setStatus(`Excel file downloaded — took ${elapsed}s.`);
      footerTime.textContent = `Last run: ${elapsed}s for ${selectedFiles.length} file(s)`;
    } else {
      const res = await fetch(`${API_BASE}/api/scan`, { method: "POST", body: buildFormData() });
      const data = await safeJson(res);
      if (!res.ok) throw new Error(data?.error || `Server error (${res.status})`);

      const elapsed = ((performance.now() - start) / 1000).toFixed(2);
      renderResults(data, mode, elapsed);
      setStatus(`Done in ${elapsed}s — ${data.summary.success}/${data.summary.total} succeeded.`);
      footerTime.textContent = `Last run: ${elapsed}s for ${selectedFiles.length} file(s)`;
    }
  } catch (err) {
    setStatus(`Error: ${err.message}`);
  } finally {
    setButtonsEnabled(true);
  }
}

async function safeJson(res) {
  try {
    return await res.json();
  } catch {
    return null;
  }
}

function renderResults(data, mode, elapsed) {
  resultsSection.classList.remove("hidden");
  resultsTitle.textContent = mode === "raw" ? "Raw QR Text" : "Decoded Fields";
  resultsMeta.textContent = `${data.summary.success}/${data.summary.total} succeeded · ${elapsed}s`;

  resultsBody.innerHTML = data.results
    .map((r) => {
      const ok = r.status === "ok";
      const body = ok
        ? mode === "raw"
          ? escapeHtml(r.raw_text)
          : escapeHtml(JSON.stringify(r.decoded, null, 2))
        : escapeHtml(r.error || "Failed");
      return `
        <details class="result-item">
          <summary>
            <span>${escapeHtml(r.filename)}</span>
            <span class="badge ${ok ? "ok" : "error"}">${ok ? "OK" : "FAILED"}</span>
          </summary>
          <pre>${body}</pre>
        </details>`;
    })
    .join("");
}

btnRaw.addEventListener("click", () => runScan("raw"));
btnDecoded.addEventListener("click", () => runScan("decoded"));
btnExcel.addEventListener("click", () => runScan("excel"));
