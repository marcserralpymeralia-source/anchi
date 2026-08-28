(() => {
  "use strict";

  const MIN_COLUMN_WIDTH = 84;
  const MAX_COLUMN_WIDTH = 640;

  function columnsFor(table) {
    return Array.from(table.querySelectorAll("col[data-column]"));
  }

  function visibleColumns(table) {
    return columnsFor(table).filter((column) => !column.hidden && getComputedStyle(column).display !== "none");
  }

  function resizableColumns(table) {
    return visibleColumns(table).filter((column) => column.dataset.column !== "action");
  }

  function storageKey(table) {
    return `database.columnWidths.${table.dataset.tableKey || table.className}`;
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
      if (column.style.width) widths[column.dataset.column] = column.style.width;
    });
    try {
      localStorage.setItem(storageKey(table), JSON.stringify(widths));
    } catch {
      // La tabla sigue funcionando aunque el navegador bloquee localStorage.
    }
  }

  function restoreWidths(table) {
    columnsFor(table).forEach((column) => {
      column.style.removeProperty("width");
    });
    try {
      localStorage.removeItem(storageKey(table));
    } catch {
      // La tabla sigue funcionando aunque el navegador bloquee localStorage.
    }
  }

  function applySavedWidths(table) {
    const widths = readWidths(table);
    columnsFor(table).forEach((column) => {
      if (column.dataset.column === "action") return;
      const width = widths[column.dataset.column];
      if (typeof width === "string" && /^\d+(\.\d+)?px$/.test(width)) {
        column.style.width = width;
      }
    });
  }

  function addResizeHandles(table) {
    const headers = Array.from(table.querySelectorAll("thead th[data-column]")).filter(
      (header) => header.dataset.column !== "action" && getComputedStyle(header).display !== "none"
    );
    headers.forEach((header, index) => {
      if (index === headers.length - 1) {
        const existing = header.querySelector(".table-column-resizer");
        if (existing) existing.remove();
        return;
      }
      if (header.querySelector(".table-column-resizer")) return;
      const handle = document.createElement("span");
      handle.className = "table-column-resizer";
      handle.setAttribute("role", "separator");
      handle.setAttribute("tabindex", "0");
      handle.setAttribute("aria-orientation", "vertical");
      handle.setAttribute("aria-label", `Cambiar ancho de ${header.textContent.trim()}`);
      handle.title = "Arrastra para cambiar el ancho";
      handle.addEventListener("pointerdown", (event) => beginResize(event, table, header.dataset.column));
      handle.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
        event.preventDefault();
        resizeByKeyboard(table, header.dataset.column, event.key === "ArrowRight" ? 16 : -16);
      });
      header.appendChild(handle);
    });
  }

  function resizeByKeyboard(table, columnName, delta) {
    const columns = resizableColumns(table);
    const targetIndex = columns.findIndex((column) => column.dataset.column === columnName);
    if (targetIndex < 0) return;
    const target = columns[targetIndex];
    const neighbour = columns[targetIndex + 1] || columns[targetIndex - 1];
    if (!neighbour) return;
    const targetCell = table.querySelector(`thead th[data-column="${CSS.escape(columnName)}"]`);
    const neighbourCell = table.querySelector(`thead th[data-column="${CSS.escape(neighbour.dataset.column)}"]`);
    if (!targetCell || !neighbourCell) return;
    const targetIsBeforeNeighbour = targetIndex < columns.indexOf(neighbour);
    const targetWidth = targetCell.getBoundingClientRect().width;
    const neighbourWidth = neighbourCell.getBoundingClientRect().width;
    const nextTargetWidth = Math.max(MIN_COLUMN_WIDTH, Math.min(MAX_COLUMN_WIDTH, targetWidth + (targetIsBeforeNeighbour ? delta : -delta)));
    const actualDelta = nextTargetWidth - targetWidth;
    const nextNeighbourWidth = Math.max(MIN_COLUMN_WIDTH, neighbourWidth - actualDelta);
    if (nextNeighbourWidth === MIN_COLUMN_WIDTH && actualDelta > neighbourWidth - MIN_COLUMN_WIDTH) return;
    target.style.width = `${Math.round(nextTargetWidth)}px`;
    neighbour.style.width = `${Math.round(nextNeighbourWidth)}px`;
    saveWidths(table);
  }

  function beginResize(event, table, columnName) {
    if (event.button !== 0) return;
    const columns = resizableColumns(table);
    const target = columns.find((column) => column.dataset.column === columnName);
    if (!target) return;

    const targetIndex = columns.indexOf(target);
    const neighbour = columns[targetIndex + 1] || columns[targetIndex - 1];
    if (!neighbour) return;
    const targetIsBeforeNeighbour = targetIndex < columns.indexOf(neighbour);
    const targetCell = table.querySelector(`thead th[data-column="${CSS.escape(columnName)}"]`);
    const neighbourCell = table.querySelector(`thead th[data-column="${CSS.escape(neighbour.dataset.column)}"]`);
    if (!targetCell || !neighbourCell) return;

    event.preventDefault();
    event.stopPropagation();
    const startTargetWidth = targetCell.getBoundingClientRect().width;
    const startNeighbourWidth = neighbourCell.getBoundingClientRect().width;
    const startX = event.clientX;
    const minNeighbour = Math.max(MIN_COLUMN_WIDTH, neighbourCell.scrollWidth > 0 ? Math.min(neighbourCell.scrollWidth, 180) : MIN_COLUMN_WIDTH);
    const minTarget = MIN_COLUMN_WIDTH;
    const maxTarget = Math.max(minTarget, Math.min(MAX_COLUMN_WIDTH, startTargetWidth + startNeighbourWidth - minNeighbour));
    table.classList.add("is-resizing");
    document.body.classList.add("is-resizing-table-column");
    if (table.setPointerCapture) table.setPointerCapture(event.pointerId);

    const move = (moveEvent) => {
      const rawDelta = moveEvent.clientX - startX;
      const delta = targetIsBeforeNeighbour ? rawDelta : -rawDelta;
      const targetWidth = Math.max(minTarget, Math.min(maxTarget, startTargetWidth + delta));
      const actualDelta = targetWidth - startTargetWidth;
      const neighbourWidth = Math.max(minNeighbour, startNeighbourWidth - actualDelta);
      target.style.width = `${Math.round(targetWidth)}px`;
      neighbour.style.width = `${Math.round(neighbourWidth)}px`;
    };
    const finish = () => {
      table.classList.remove("is-resizing");
      document.body.classList.remove("is-resizing-table-column");
      table.removeEventListener("pointermove", move);
      table.removeEventListener("pointerup", finish);
      table.removeEventListener("pointercancel", finish);
      saveWidths(table);
    };
    table.addEventListener("pointermove", move);
    table.addEventListener("pointerup", finish, {once: true});
    table.addEventListener("pointercancel", finish, {once: true});
  }

  function findTable(tableKey) {
    return document.querySelector(`table[data-resizable-table][data-table-key="${CSS.escape(tableKey)}"]`);
  }

  function initTable(table) {
    if (!columnsFor(table).length) return;
    applySavedWidths(table);
    addResizeHandles(table);
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("table[data-resizable-table]").forEach(initTable);
    document.querySelectorAll("[data-table-reset]").forEach((button) => {
      if (button.dataset.tableResetBound === "true") return;
      button.dataset.tableResetBound = "true";
      button.addEventListener("click", () => {
        const table = findTable(button.dataset.tableReset);
        if (table) restoreWidths(table);
      });
    });
  });
})();
