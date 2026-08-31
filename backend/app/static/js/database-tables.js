(() => {
  "use strict";

  // Column widths are deliberately kept in pixels. A percentage based table
  // cannot provide a predictable drag boundary when columns are hidden.
  const STORAGE_PREFIX = "database.columnWidths.v2.";
  const DEFAULT_MIN_WIDTH = 84;
  const DEFAULT_MAX_WIDTH = 640;
  const KEYBOARD_STEP = 16;

  function columnsFor(table) {
    return Array.from(table.querySelectorAll("col[data-column]"));
  }

  function isVisible(element) {
    return !element.hidden && getComputedStyle(element).display !== "none";
  }

  function visibleColumns(table) {
    return columnsFor(table).filter(isVisible);
  }

  function headersFor(table) {
    return Array.from(table.querySelectorAll("thead th[data-column]")).filter(isVisible);
  }

  function columnFor(table, columnName) {
    return columnsFor(table).find((column) => column.dataset.column === columnName);
  }

  function numberFrom(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function metaFor(column) {
    const fallback = Math.max(DEFAULT_MIN_WIDTH, numberFrom(column.dataset.defaultWidth, 160));
    const min = Math.max(DEFAULT_MIN_WIDTH, numberFrom(column.dataset.minWidth, DEFAULT_MIN_WIDTH));
    const max = Math.max(min, numberFrom(column.dataset.maxWidth, DEFAULT_MAX_WIDTH));
    return {
      defaultWidth: Math.max(min, Math.min(max, fallback)),
      min,
      max,
      fixed: column.dataset.resizable === "false" || column.dataset.column === "action",
    };
  }

  function storageKey(table) {
    return `${STORAGE_PREFIX}${table.dataset.tableKey || table.className}`;
  }

  function parseStoredWidth(value, column) {
    const meta = metaFor(column);
    const raw = typeof value === "number" ? value : parseFloat(String(value));
    if (!Number.isFinite(raw)) return null;
    return Math.round(Math.max(meta.min, Math.min(meta.max, raw)));
  }

  function readWidths(table) {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey(table)) || "{}");
      return value && typeof value === "object" ? value : {};
    } catch {
      return {};
    }
  }

  function saveWidths(table) {
    const widths = {};
    columnsFor(table).forEach((column) => {
      if (column.dataset.column === "action") return;
      const width = parseStoredWidth(column.style.width, column);
      if (width !== null) widths[column.dataset.column] = width;
    });
    try {
      localStorage.setItem(storageKey(table), JSON.stringify(widths));
    } catch {
      // La tabla sigue funcionando aunque el navegador bloquee localStorage.
    }
  }

  function setColumnWidth(column, width) {
    const meta = metaFor(column);
    const clamped = Math.max(meta.min, Math.min(meta.max, width));
    column.style.width = `${Math.round(clamped)}px`;
  }

  function widthOf(table, column) {
    const stored = parseStoredWidth(column.style.width, column);
    if (stored !== null) return stored;
    return metaFor(column).defaultWidth;
  }

  function widthsFor(table, columns = visibleColumns(table)) {
    return new Map(columns.map((column) => [column.dataset.column, widthOf(table, column)]));
  }

  function tableMinWidth(table, columns) {
    const configured = numberFrom(table.dataset.minTableWidth, 0);
    const minimumSum = columns.reduce((sum, column) => sum + metaFor(column).min, 0);
    return Math.max(configured, minimumSum, 860);
  }

  function tableAvailableWidth(table) {
    const wrapper = table.closest(".database-table-wrap");
    return wrapper ? Math.round(wrapper.clientWidth) : 0;
  }

  function applyTableWidth(table, widths) {
    const total = Array.from(widths.values()).reduce((sum, width) => sum + width, 0);
    const columns = visibleColumns(table);
    table.style.width = `${Math.max(tableMinWidth(table, columns), total, tableAvailableWidth(table))}px`;
  }

  function distributeInitialSpace(table, columns, widths, hasSavedWidths) {
    if (hasSavedWidths) return;

    const available = tableAvailableWidth(table);
    const total = Array.from(widths.values()).reduce((sum, width) => sum + width, 0);
    const extra = Math.max(0, available - total);
    const flexible = columns.filter((column) => !metaFor(column).fixed);
    if (!extra || !flexible.length) return;

    const weight = flexible.reduce((sum, column) => sum + widths.get(column.dataset.column), 0);
    let remaining = extra;
    flexible.forEach((column, index) => {
      const key = column.dataset.column;
      const share = index === flexible.length - 1
        ? remaining
        : Math.round(extra * (widths.get(key) / weight));
      const meta = metaFor(column);
      const next = Math.min(meta.max, widths.get(key) + share);
      const actual = next - widths.get(key);
      widths.set(key, next);
      remaining -= actual;
    });
  }

  function initializeLayout(table, {clearSaved = false} = {}) {
    const columns = visibleColumns(table);
    if (!columns.length) return;

    if (clearSaved) {
      try {
        localStorage.removeItem(storageKey(table));
      } catch {
        // Ignore storage failures.
      }
      columnsFor(table).forEach((column) => column.style.removeProperty("width"));
      table.style.removeProperty("width");
    }

    const stored = readWidths(table);
    const widths = new Map();
    let hasSavedWidths = false;
    columns.forEach((column) => {
      const saved = parseStoredWidth(stored[column.dataset.column], column);
      if (saved !== null && !metaFor(column).fixed) hasSavedWidths = true;
      widths.set(column.dataset.column, saved ?? metaFor(column).defaultWidth);
    });

    distributeInitialSpace(table, columns, widths, hasSavedWidths);
    columns.forEach((column) => setColumnWidth(column, widths.get(column.dataset.column)));
    applyTableWidth(table, widths);
    addResizeHandles(table);
  }

  function refreshLayout(table) {
    const columns = visibleColumns(table);
    if (!columns.length) return;
    const widths = widthsFor(table, columns);
    columns.forEach((column) => setColumnWidth(column, widths.get(column.dataset.column)));
    applyTableWidth(table, widths);
    addResizeHandles(table);
  }

  function setBoundary(table, left, right, requestedLeftWidth, startTotal) {
    const leftMeta = metaFor(left);
    const rightMeta = metaFor(right);
    const leftMin = rightMeta.fixed ? leftMeta.min : Math.max(leftMeta.min, startTotal - rightMeta.max);
    const leftMax = rightMeta.fixed ? leftMeta.max : Math.min(leftMeta.max, startTotal - rightMeta.min);
    const leftWidth = Math.max(leftMin, Math.min(leftMax, requestedLeftWidth));
    const rightWidth = rightMeta.fixed ? widthOf(table, right) : startTotal - leftWidth;
    setColumnWidth(left, leftWidth);
    if (!rightMeta.fixed) setColumnWidth(right, rightWidth);

    const widths = widthsFor(table);
    applyTableWidth(table, widths);
  }

  function resizeByKeyboard(table, columnName, delta) {
    const columns = visibleColumns(table);
    const leftIndex = columns.findIndex((column) => column.dataset.column === columnName);
    if (leftIndex < 0 || leftIndex === columns.length - 1) return;

    const left = columns[leftIndex];
    const right = columns[leftIndex + 1];
    const leftWidth = widthOf(table, left);
    const rightWidth = widthOf(table, right);
    const total = leftWidth + rightWidth;
    setBoundary(table, left, right, leftWidth + delta, total);
    saveWidths(table);
  }

  function beginResize(event, table, columnName, handle) {
    if (event.button !== 0) return;
    const columns = visibleColumns(table);
    const leftIndex = columns.findIndex((column) => column.dataset.column === columnName);
    if (leftIndex < 0 || leftIndex === columns.length - 1) return;

    const left = columns[leftIndex];
    const right = columns[leftIndex + 1];
    if (metaFor(left).fixed) return;

    event.preventDefault();
    event.stopPropagation();
    const startLeftWidth = widthOf(table, left);
    const startRightWidth = widthOf(table, right);
    const startTotal = startLeftWidth + startRightWidth;
    const startX = event.clientX;
    table.classList.add("is-resizing");
    document.body.classList.add("is-resizing-table-column");
    handle.setPointerCapture?.(event.pointerId);

    const move = (moveEvent) => {
      const requested = startLeftWidth + (moveEvent.clientX - startX);
      setBoundary(table, left, right, requested, startTotal);
    };
    const finish = () => {
      table.classList.remove("is-resizing");
      document.body.classList.remove("is-resizing-table-column");
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", finish);
      handle.removeEventListener("pointercancel", finish);
      handle.removeEventListener("lostpointercapture", finish);
      saveWidths(table);
    };

    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", finish, {once: true});
    handle.addEventListener("pointercancel", finish, {once: true});
    handle.addEventListener("lostpointercapture", finish, {once: true});
  }

  function addResizeHandles(table) {
    table.querySelectorAll(".table-column-resizer").forEach((handle) => handle.remove());
    const headers = headersFor(table);
    headers.slice(0, -1).forEach((header) => {
      const column = columnFor(table, header.dataset.column);
      if (!column || metaFor(column).fixed) return;

      const handle = document.createElement("button");
      handle.type = "button";
      handle.className = "table-column-resizer";
      handle.setAttribute("role", "separator");
      handle.setAttribute("aria-orientation", "vertical");
      handle.setAttribute("aria-label", `Cambiar ancho de ${header.textContent.trim()}`);
      handle.title = "Arrastra para cambiar el ancho";
      handle.addEventListener("pointerdown", (event) => beginResize(event, table, header.dataset.column, handle));
      handle.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
      });
      handle.addEventListener("keydown", (event) => {
        if (["ArrowLeft", "ArrowRight"].includes(event.key)) {
          event.preventDefault();
          resizeByKeyboard(table, header.dataset.column, event.key === "ArrowRight" ? KEYBOARD_STEP : -KEYBOARD_STEP);
        }
      });
      header.appendChild(handle);
    });
  }

  let contextMenuElement = null;
  let contextMenuRow = null;
  let contextMenuRestoreFocus = null;

  function cleanActionLabel(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function actionLabel(action) {
    return cleanActionLabel(
      action.getAttribute("aria-label")
        || action.getAttribute("title")
        || action.textContent
        || "Acción"
    ) || "Acción";
  }

  function actionTargetKey(action) {
    const form = action.closest("form");
    if (form) {
      return [
        "form",
        (form.method || "get").toUpperCase(),
        form.action,
        action.getAttribute("name") || "",
        action.getAttribute("value") || "",
      ].join(":");
    }

    if (action.matches("a")) {
      return [
        "link",
        action.getAttribute("href") || "",
        action.getAttribute("onclick") || "",
      ].join(":");
    }

    return [
      "button",
      action.getAttribute("type") || "button",
      action.getAttribute("onclick") || "",
    ].join(":");
  }

  function rowActions(row) {
    const actionRoot = row.querySelector(".row-context-actions");
    if (!actionRoot) return [];

    const controls = Array.from(actionRoot.querySelectorAll("a[href], button")).filter((action) => {
      return !action.disabled && !action.hidden;
    });
    const directTargets = new Set(
      controls
        .filter((action) => !action.closest("details"))
        .map(actionTargetKey)
    );
    const seen = new Set();

    return controls.filter((action) => {
      const insideMoreActions = Boolean(action.closest("details"));
      const target = actionTargetKey(action);

      // Some rows keep secondary actions inside a collapsed "..." group.
      // Keep the context menu complete without repeating controls already
      // represented by the primary actions.
      if (insideMoreActions && directTargets.has(target)) return false;

      const key = `${target}:${actionLabel(action).toLocaleLowerCase()}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function ensureContextMenu() {
    if (contextMenuElement) return contextMenuElement;

    contextMenuElement = document.createElement("div");
    contextMenuElement.className = "table-context-menu";
    contextMenuElement.setAttribute("role", "menu");
    contextMenuElement.setAttribute("aria-label", "Acciones de la fila");
    contextMenuElement.hidden = true;
    document.body.appendChild(contextMenuElement);
    return contextMenuElement;
  }

  function closeContextMenu({restoreFocus = true} = {}) {
    if (contextMenuRow) {
      contextMenuRow.classList.remove("context-menu-active");
      contextMenuRow = null;
    }
    if (!contextMenuElement) return;

    contextMenuElement.hidden = true;
    contextMenuElement.replaceChildren();
    contextMenuElement.style.removeProperty("left");
    contextMenuElement.style.removeProperty("top");

    const focusTarget = contextMenuRestoreFocus;
    contextMenuRestoreFocus = null;
    if (restoreFocus && focusTarget?.isConnected && typeof focusTarget.focus === "function") {
      focusTarget.focus({preventScroll: true});
    }
  }

  function invokeRowAction(action) {
    const form = action.form;
    if (form && action.type === "submit" && typeof form.requestSubmit === "function") {
      try {
        form.requestSubmit(action);
        return;
      } catch {
        // Fall back to the native click for older or unusual form controls.
      }
    }
    action.click();
  }

  function openContextMenu(event, row) {
    const actions = rowActions(row);
    if (!actions.length) return false;

    closeContextMenu();
    const menu = ensureContextMenu();
    contextMenuRestoreFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    contextMenuRow = row;
    row.classList.add("context-menu-active");
    menu.replaceChildren();

    actions.forEach((action) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "table-context-menu__item";
      item.setAttribute("role", "menuitem");
      item.textContent = actionLabel(action);
      item.addEventListener("click", (clickEvent) => {
        clickEvent.preventDefault();
        clickEvent.stopPropagation();
        closeContextMenu();
        invokeRowAction(action);
      });
      menu.appendChild(item);
    });

    menu.hidden = false;
    const margin = 8;
    const bounds = menu.getBoundingClientRect();
    const left = Math.max(margin, Math.min(event.clientX, window.innerWidth - bounds.width - margin));
    const top = Math.max(margin, Math.min(event.clientY, window.innerHeight - bounds.height - margin));
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
    menu.querySelector("[role=menuitem]")?.focus({preventScroll: true});
    return true;
  }

  function bindContextMenu(table) {
    if (table.dataset.contextMenuBound === "true") return;
    table.dataset.contextMenuBound = "true";
    table.addEventListener("contextmenu", (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const row = target.closest("tbody tr");
      if (!row || row.closest("table") !== table) return;
      if (openContextMenu(event, row)) {
        event.preventDefault();
        event.stopPropagation();
      }
    });
  }

  function bindGlobalContextMenuEvents() {
    document.addEventListener("pointerdown", (event) => {
      if (!contextMenuElement || contextMenuElement.hidden) return;
      if (event.target instanceof Node && contextMenuElement.contains(event.target)) return;
      closeContextMenu();
    }, true);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && contextMenuElement && !contextMenuElement.hidden) {
        event.preventDefault();
        closeContextMenu();
      }
    });
    document.addEventListener("scroll", () => closeContextMenu(), true);
    window.addEventListener("resize", () => closeContextMenu());
  }

  function findTable(tableKey) {
    return document.querySelector(`table[data-resizable-table][data-table-key="${CSS.escape(tableKey)}"]`);
  }

  function bindTable(table) {
    if (!columnsFor(table).length) return;
    initializeLayout(table);
    bindContextMenu(table);

    const observer = new MutationObserver(() => refreshLayout(table));
    observer.observe(table, {subtree: true, attributes: true, attributeFilter: ["hidden"]});
    table._databaseTableObserver = observer;
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindGlobalContextMenuEvents();
    document.querySelectorAll("table[data-resizable-table]").forEach(bindTable);
    document.querySelectorAll("[data-table-reset]").forEach((button) => {
      if (button.dataset.tableResetBound === "true") return;
      button.dataset.tableResetBound = "true";
      button.addEventListener("click", () => {
        const table = findTable(button.dataset.tableReset);
        if (table) initializeLayout(table, {clearSaved: true});
      });
    });
  });
})();
