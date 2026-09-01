const selectedWorkbenchItems = new Set();

async function openWorkbenchItemDetail(detailUrl) {
  const dialog = document.getElementById("workbench-detail-modal");
  const body = document.getElementById("workbench-detail-modal-body");
  if (!dialog || !body || !detailUrl) return;
  dialog.dataset.dirty = "false";
  body.innerHTML = `
    <div class="review-modal-header">
      <div>
        <h2>Cargando detalle...</h2>
        <p class="muted">Preparando la propuesta y el correo recibido.</p>
      </div>
      <button type="button" class="secondary" onclick="closeWorkbenchDetailModal()">Cerrar</button>
    </div>
  `;
  dialog.showModal();
  try {
    const response = await fetch(detailUrl, {headers: {"X-Requested-With": "fetch"}});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    body.innerHTML = await response.text();
    bindWorkbenchDetailContent(body, dialog);
  } catch (error) {
    console.warn("No se pudo cargar el detalle operativo", error);
    body.innerHTML = `
      <div class="review-modal-header">
        <div>
          <h2>No se pudo cargar el detalle</h2>
          <p class="muted">Prueba de nuevo o abre la vista completa.</p>
        </div>
        <button type="button" class="secondary" onclick="closeWorkbenchDetailModal()">Cerrar</button>
      </div>
    `;
  }
}

function closeWorkbenchDetailModal() {
  const dialog = document.getElementById("workbench-detail-modal");
  if (!dialog) return;
  if (dialog.dataset.dirty === "true" && !confirm("Hay cambios sin guardar. ¿Cerrar igualmente?")) return;
  dialog.close();
}

function markWorkbenchItem(card) {
  document.querySelectorAll(".queue-item.is-selected").forEach((node) => node.classList.remove("is-selected"));
  card.classList.add("is-selected");
}

function selectWorkbenchItem(card) {
  markWorkbenchItem(card);
  if (card.dataset.detailUrl) openWorkbenchItemDetail(card.dataset.detailUrl);
}

function workbenchItemKey(card) {
  return `${card.dataset.selectionKind || card.dataset.kind || ""}:${card.dataset.selectionId || ""}`;
}

function updateWorkbenchBulkBar() {
  const form = document.getElementById("workbench-bulk-form");
  const countNode = document.getElementById("workbench-selected-count");
  const hiddenNode = document.getElementById("workbench-selected-items");
  const items = Array.from(selectedWorkbenchItems).filter(Boolean);
  if (countNode) countNode.textContent = String(items.length);
  if (hiddenNode) {
    hiddenNode.value = JSON.stringify(items.map((value) => {
      const [kind, id] = String(value).split(":", 2);
      return {kind, id: Number(id)};
    }));
  }
  if (form) form.hidden = items.length === 0;
}

function syncWorkbenchSelectionStates() {
  const visibleKeys = new Set();
  document.querySelectorAll(".queue-item").forEach((card) => {
    const key = workbenchItemKey(card);
    visibleKeys.add(key);
    const checkbox = card.querySelector(".queue-item-select");
    if (checkbox) checkbox.checked = selectedWorkbenchItems.has(key);
    card.classList.toggle("is-selected", selectedWorkbenchItems.has(key));
  });
  Array.from(selectedWorkbenchItems).forEach((key) => {
    if (!visibleKeys.has(key)) selectedWorkbenchItems.delete(key);
  });
  updateWorkbenchBulkBar();
}

function toggleWorkbenchSelection(checkbox) {
  const card = checkbox.closest(".queue-item");
  if (!card) return;
  const key = workbenchItemKey(card);
  if (checkbox.checked) {
    selectedWorkbenchItems.add(key);
    card.classList.add("is-selected");
  } else {
    selectedWorkbenchItems.delete(key);
    card.classList.remove("is-selected");
  }
  updateWorkbenchBulkBar();
}

function submitWorkbenchBulkAction(action, destructive = false) {
  const form = document.getElementById("workbench-bulk-form");
  if (!form || selectedWorkbenchItems.size === 0) return;
  if (destructive && !confirm("¿Seguro que quieres eliminar los pedidos seleccionados? Esta acción los moverá a eliminados.")) return;
  const targetState = document.getElementById("workbench-target-state");
  const stateSelect = document.getElementById("workbench-state-select");
  if (targetState) targetState.value = action === "change_state" ? (stateSelect?.value || "") : "";
  if (action === "change_state" && targetState && !targetState.value) {
    alert("Selecciona un estado antes de aplicar el cambio.");
    return;
  }
  form.querySelectorAll("input[type='hidden'][name='action']").forEach((node) => node.remove());
  const actionNode = document.createElement("input");
  actionNode.type = "hidden";
  actionNode.name = "action";
  actionNode.value = action;
  form.appendChild(actionNode);
  form.submit();
}

function openLineProductPicker(lineId) {
  const picker = document.getElementById(`line-product-picker-${lineId}`);
  const select = document.querySelector(`[data-line-product-select="${lineId}"]`);
  const trigger = document.querySelector(`[data-line-picker-trigger="${lineId}"]`);
  if (!picker || !select) return;
  picker.hidden = false;
  if (trigger) trigger.hidden = true;
  const form = select.form;
  if (form) form.dataset.dirty = "true";
  select.focus({preventScroll: true});
  try {
    if (typeof select.showPicker === "function") {
      select.showPicker();
    }
  } catch (error) {
    console.warn("No se pudo abrir el selector de producto", error);
  }
}

function bindSourceTabs(root) {
  root.querySelectorAll("[data-source-tabs]").forEach((tabs) => {
    const container = tabs.closest("[data-source-scope]") || tabs.closest(".review-pane") || root;
    const panels = Array.from(container.querySelectorAll("[data-source-panel]")).filter((panel) => {
      const nestedScope = panel.closest("[data-source-scope]");
      return !nestedScope || nestedScope === container;
    });
    tabs.querySelectorAll("[data-source-tab]").forEach((button) => {
      button.addEventListener("click", () => {
        const target = button.dataset.sourceTab;
        tabs.querySelectorAll("[data-source-tab]").forEach((tab) => tab.classList.toggle("active", tab === button));
        panels.forEach((panel) => {
          const panelKey = panel.dataset.sourcePanel || "";
          panel.hidden = !(panelKey === target || panelKey.startsWith(`${target}-`));
        });
      });
    });
  });
}

function bindAttachmentSelects(root) {
  root.querySelectorAll("[data-attachment-select]").forEach((control) => {
    if (control.tagName === "SELECT") {
      control.addEventListener("change", () => selectAttachmentPreviewFromSelect(control));
      if (control.value) selectAttachmentPreviewFromSelect(control);
      return;
    }
    renderAttachmentPreviewContent(control.dataset.orderId, control);
  });
}

function bindWorkbenchDetailContent(root, dialog) {
  bindSourceTabs(root);
  bindAttachmentSelects(root);
  root.querySelectorAll("input, textarea, select").forEach((field) => {
    field.addEventListener("change", () => {
      if (dialog) dialog.dataset.dirty = "true";
    });
  });
  root.querySelectorAll("[data-line-product-select]").forEach((select) => {
    select.addEventListener("change", () => {
      const form = select.form;
      if (form) form.requestSubmit();
    });
  });
}

function copyText(id) {
  const node = document.getElementById(id);
  if (node) navigator.clipboard.writeText(node.textContent || "");
}

function switchOrderPdf(select, frameId) {
  const frame = document.getElementById(frameId);
  if (frame && select.value) frame.src = select.value;
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function humanSize(bytes) {
  const value = Number(bytes) || 0;
  if (value <= 0) return "";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(value < 10_240 ? 1 : 0)} KB`;
  return `${(value / (1024 * 1024)).toFixed(value < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

function parseDelimitedText(text) {
  const raw = String(text || "").replace(/^\uFEFF/, "").trim();
  if (!raw) return [];
  const lines = raw.split(/\r?\n/).filter(Boolean);
  if (!lines.length) return [];
  const candidates = [",", ";", "\t", "|"];
  let delimiter = ",";
  let maxColumns = 0;
  candidates.forEach((candidate) => {
    const columns = lines[0].split(candidate).length;
    if (columns > maxColumns) {
      maxColumns = columns;
      delimiter = candidate;
    }
  });
  return lines.slice(0, 12).map((line) => line.split(delimiter).map((cell) => cell.trim()));
}

function renderAttachmentPreviewEmpty(orderId, message, downloadUrl = "") {
  const preview = document.getElementById(`review-attachment-preview-${orderId}`);
  if (!preview) return;
  preview.innerHTML = `
    <div class="review-attachment-preview-empty">
      <strong>${escapeHtml(message)}</strong>
      ${downloadUrl ? `<a class="button secondary compact-button" href="${escapeHtml(downloadUrl)}">Descargar</a>` : ""}
    </div>
  `;
}

function renderAttachmentPreviewLoading(orderId, filename) {
  const preview = document.getElementById(`review-attachment-preview-${orderId}`);
  if (!preview) return;
  preview.innerHTML = `
    <div class="review-attachment-preview-empty">
      <strong>${escapeHtml(filename)}</strong>
      <span>Cargando previsualización...</span>
    </div>
  `;
}

function attachmentActions(previewUrl, downloadUrl) {
  return `
    <div class="review-channel-actions">
      <a class="button secondary compact-button" target="_blank" href="${escapeHtml(previewUrl)}">Abrir</a>
      <a class="button secondary compact-button" href="${escapeHtml(downloadUrl)}">Descargar</a>
    </div>
  `;
}

function pdfFrameUrl(url) {
  if (!url) return "";
  return url.includes("#") ? url : `${url}#view=FitH&navpanes=0`;
}

async function renderAttachmentPreviewContent(orderId, attachmentNode) {
  const preview = document.getElementById(`review-attachment-preview-${orderId}`);
  if (!preview) return;
  const kind = attachmentNode.dataset.previewKind || "unsupported";
  const previewUrl = attachmentNode.dataset.previewUrl || "";
  const downloadUrl = attachmentNode.dataset.downloadUrl || "";
  const filename = attachmentNode.dataset.filename || "Adjunto";
  const sizeLabel = humanSize(attachmentNode.dataset.sizeBytes);
  const extractedText = (attachmentNode.dataset.extractedText || "").trim();
  renderAttachmentPreviewLoading(orderId, filename);
  if (!previewUrl) {
    renderAttachmentPreviewEmpty(orderId, "No se puede previsualizar este archivo.", downloadUrl);
    return;
  }
  if (kind === "pdf") {
    const frameUrl = pdfFrameUrl(previewUrl);
    preview.innerHTML = `
      <div class="review-attachment-preview-head">
        <div>
          <strong>${escapeHtml(filename)}</strong>
          <span>${escapeHtml(sizeLabel)}</span>
        </div>
        ${attachmentActions(previewUrl, downloadUrl)}
      </div>
      <iframe class="review-attachment-frame" src="${escapeHtml(frameUrl)}"></iframe>
    `;
    return;
  }
  if (kind === "image") {
    preview.innerHTML = `
      <div class="review-attachment-preview-head">
        <div>
          <strong>${escapeHtml(filename)}</strong>
          <span>${escapeHtml(sizeLabel)}</span>
        </div>
        ${attachmentActions(previewUrl, downloadUrl)}
      </div>
      <div class="review-attachment-image-wrap">
        <img class="review-attachment-image" src="${escapeHtml(previewUrl)}" alt="${escapeHtml(filename)}">
      </div>
    `;
    return;
  }
  const canUseText = kind === "text" || kind === "csv" || (kind === "doc" && Boolean(extractedText)) || (kind === "sheet" && Boolean(extractedText)) || Boolean(extractedText);
  if (canUseText) {
    let text = extractedText;
    if (!text || kind === "csv") {
      try {
        const response = await fetch(previewUrl, {credentials: "same-origin"});
        if (response.ok) {
          text = await response.text();
        }
      } catch (error) {
        console.warn("No se pudo cargar el texto del adjunto", error);
      }
    }
    text = String(text || "").trim();
    if (!text) {
      renderAttachmentPreviewEmpty(orderId, "No se puede previsualizar este archivo.", downloadUrl);
      return;
    }
    if (kind === "csv") {
      const rows = parseDelimitedText(text);
      if (rows.length) {
        const [header, ...body] = rows;
        const visibleRows = body.slice(0, 8);
        preview.innerHTML = `
          <div class="review-attachment-preview-head">
            <div>
              <strong>${escapeHtml(filename)}</strong>
              <span>${escapeHtml(sizeLabel)}</span>
            </div>
            ${attachmentActions(previewUrl, downloadUrl)}
          </div>
          <div class="review-attachment-table-wrap">
            <table class="review-attachment-table">
              <thead><tr>${header.map((cell) => `<th>${escapeHtml(cell)}</th>`).join("")}</tr></thead>
              <tbody>
                ${visibleRows.map((row) => `<tr>${header.map((_, index) => `<td>${escapeHtml(row[index] || "")}</td>`).join("")}</tr>`).join("")}
              </tbody>
            </table>
          </div>
        `;
        return;
      }
    }
    preview.innerHTML = `
      <div class="review-attachment-preview-head">
        <div>
          <strong>${escapeHtml(filename)}</strong>
          <span>${escapeHtml(sizeLabel)}</span>
        </div>
        ${attachmentActions(previewUrl, downloadUrl)}
      </div>
      <pre class="review-attachment-text">${escapeHtml(text)}</pre>
    `;
    return;
  }
  renderAttachmentPreviewEmpty(orderId, "No se puede previsualizar este archivo.", downloadUrl);
}

function selectAttachmentPreviewFromSelect(select) {
  const option = select.selectedOptions ? select.selectedOptions[0] : null;
  if (!option) return;
  renderAttachmentPreviewContent(option.dataset.orderId, option);
}

function anyWorkbenchModalOpen() {
  return Array.from(document.querySelectorAll(".order-review-modal")).some((dialog) => dialog.open || dialog.dataset.dirty === "true");
}

let workbenchRefreshInFlight = false;

async function softRefreshWorkbench() {
  if (workbenchRefreshInFlight || document.visibilityState === "hidden" || anyWorkbenchModalOpen()) return;
  const currentShell = document.querySelector(".workbench-shell");
  if (!currentShell) return;
  workbenchRefreshInFlight = true;
  currentShell.classList.add("is-refreshing");
  currentShell.setAttribute("aria-busy", "true");
  const url = new URL(window.location.href);
  url.searchParams.set("partial", "workbench");
  try {
    const response = await fetch(url.toString(), {
      headers: {"X-Requested-With": "fetch", "Accept": "text/html"},
      credentials: "same-origin",
    });
    if (!response.ok) return;
    const html = await response.text();
    const doc = new DOMParser().parseFromString(html, "text/html");
    const next = doc.querySelector(".workbench-shell");
    if (!next) return;
    currentShell.replaceWith(next);
    syncWorkbenchSelectionStates();
    const stamp = document.getElementById("workbench-last-refresh");
    if (stamp) stamp.textContent = `Ultima actualizacion: ${new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}`;
  } catch (error) {
    console.warn("No se pudo actualizar la bandeja", error);
  } finally {
    workbenchRefreshInFlight = false;
    const refreshedShell = document.querySelector(".workbench-shell");
    if (refreshedShell) {
      refreshedShell.classList.remove("is-refreshing");
      refreshedShell.removeAttribute("aria-busy");
    }
  }
}

function initWorkbenchDashboard() {
  bindWorkbenchDetailContent(document, null);
  const initialQueueItem = document.querySelector(".queue-item");
  if (initialQueueItem) markWorkbenchItem(initialQueueItem);
  syncWorkbenchSelectionStates();
  const stamp = document.getElementById("workbench-last-refresh");
  if (stamp) stamp.textContent = `Ultima actualizacion: ${new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}`;
  setInterval(softRefreshWorkbench, 45000);
}

document.addEventListener("DOMContentLoaded", initWorkbenchDashboard);
