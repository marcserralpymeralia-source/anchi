(function () {
  const BACKFILL_BATCH_SIZE = 5;
  const CONTINUE_PATH_PREFIX = "/settings/email/backfill/continue/";
  const SELECTOR_FORM = "[data-email-backfill-form]";
  const SELECTOR_ROOT = "[data-email-backfill-root]";

  function clampLimit(value) {
    const parsed = Number.parseInt(value, 10);
    if (!Number.isFinite(parsed) || Number.isNaN(parsed)) return 1;
    return Math.max(1, Math.min(parsed, 100));
  }

  function toCount(value) {
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) && !Number.isNaN(parsed) && parsed > 0 ? parsed : 0;
  }

  function createBackfillState(limit) {
    const safeLimit = clampLimit(limit);
    return {
      batchSize: BACKFILL_BATCH_SIZE,
      maxIterations: Math.max(1, Math.ceil(safeLimit / BACKFILL_BATCH_SIZE) + 2),
      batches: 0,
      totals: {found: 0, saved: 0, duplicates: 0, errors: 0},
      seenJobIds: new Set(),
      previousRemaining: null,
      running: false,
      stopped: false,
      status: "idle",
      message: "",
      errorType: null,
      lastJobId: null,
      requests: [],
    };
  }

  function collectNodes(form) {
    const root = form.closest(SELECTOR_ROOT) || form.parentElement || form;
    return {
      root,
      state: root.querySelector("[data-email-backfill-state]"),
      found: root.querySelector("[data-email-backfill-found]"),
      saved: root.querySelector("[data-email-backfill-saved]"),
      duplicates: root.querySelector("[data-email-backfill-duplicates]"),
      errors: root.querySelector("[data-email-backfill-errors]"),
      batches: root.querySelector("[data-email-backfill-batches]"),
      remaining: root.querySelector("[data-email-backfill-remaining]"),
      message: root.querySelector("[data-email-backfill-message]"),
      buttons: Array.from(form.querySelectorAll("button")),
    };
  }

  function setText(node, value) {
    if (node) node.textContent = String(value ?? "");
  }

  function renderStatus(nodes, state) {
    setText(nodes.state, state.status === "running" ? "Procesando" : (state.status === "failed" ? "Error" : "Completado"));
    setText(nodes.found, state.totals.found);
    setText(nodes.saved, state.totals.saved);
    setText(nodes.duplicates, state.totals.duplicates);
    setText(nodes.errors, state.totals.errors);
    setText(nodes.batches, state.batches);
    setText(nodes.remaining, state.remaining ?? 0);
    setText(nodes.message, state.message || "El backfill manual encadena lotes de 5 correos de forma automática hasta completar el rango o alcanzar el límite.");
  }

  function setBusy(nodes, busy) {
    nodes.buttons.forEach((button) => {
      if (busy) {
        if (button.dataset.originalText === undefined) {
          button.dataset.originalText = button.textContent || "";
        }
        if (button.dataset.loadingText) {
          button.textContent = button.dataset.loadingText;
        }
        button.disabled = true;
      } else {
        if (button.dataset.originalText !== undefined) {
          button.textContent = button.dataset.originalText;
          delete button.dataset.originalText;
        }
        button.disabled = false;
      }
    });
  }

  function mergeBatch(state, response) {
    state.batches += 1;
    state.totals.found += toCount(response.found);
    state.totals.saved += toCount(response.saved ?? response.imported);
    state.totals.duplicates += toCount(response.duplicates);
    state.totals.errors += toCount(response.errors);
  }

  function formatFailedResult(state, response, errorType, message) {
    state.status = "failed";
    state.errorType = errorType;
    state.message = message || response?.message || "No se pudo completar el backfill.";
    state.stopped = true;
    return {
      ...response,
      ok: false,
      status: "failed",
      error_type: errorType,
      message: state.message,
      batches: state.batches,
      found: state.totals.found,
      saved: state.totals.saved,
      duplicates: state.totals.duplicates,
      errors: state.totals.errors,
      remaining: Math.max(toCount(response?.remaining), 0),
    };
  }

  function applyBackfillBatch(state, response) {
    const jobId = response?.job_id !== undefined && response?.job_id !== null ? String(response.job_id) : "";
    if (jobId && state.seenJobIds.has(jobId)) {
      return formatFailedResult(state, response, "backfill_job_repeated", "La cadena de backfill no progresa.");
    }
    if (jobId) {
      state.seenJobIds.add(jobId);
      state.lastJobId = jobId;
    }

    mergeBatch(state, response);
    state.remaining = Math.max(toCount(response?.remaining), 0);

    if (response?.ok === false || response?.status === "failed") {
      return formatFailedResult(state, response, response?.error_type || "backfill_failed", response?.message || "La petición de backfill falló.");
    }

    const continuationJobId = response?.continuation_job_id !== undefined && response?.continuation_job_id !== null ? String(response.continuation_job_id) : "";
    const hasMore = Boolean(response?.has_more);
    if (!continuationJobId || !hasMore || state.remaining <= 0) {
      state.status = "success";
      state.message = response?.message || "Backfill IMAP completado";
      state.stopped = true;
      return {
        ...response,
        ok: true,
        status: "success",
        batches: state.batches,
        found: state.totals.found,
        saved: state.totals.saved,
        duplicates: state.totals.duplicates,
        errors: state.totals.errors,
        remaining: state.remaining,
      };
    }

    if (state.previousRemaining !== null && state.remaining >= state.previousRemaining) {
      return formatFailedResult(state, response, "backfill_no_progress", "La cadena de backfill no avanza.");
    }
    if (state.seenJobIds.has(continuationJobId) || continuationJobId === jobId) {
      return formatFailedResult(state, response, "backfill_job_repeated", "La cadena de backfill no progresa.");
    }
    if (state.batches >= state.maxIterations) {
      return formatFailedResult(state, response, "backfill_iteration_limit", "La cadena de backfill superó el número máximo de iteraciones.");
    }

    state.previousRemaining = state.remaining;
    return {
      ...response,
      ok: true,
      status: "running",
      continue: true,
      next_job_id: continuationJobId,
      batches: state.batches,
      found: state.totals.found,
      saved: state.totals.saved,
      duplicates: state.totals.duplicates,
      errors: state.totals.errors,
      remaining: state.remaining,
    };
  }

  async function runBackfillSequence(form, fetchFn = fetch, nodes = collectNodes(form)) {
    if (!form || form.dataset.backfillRunning === "true") {
      return {ok: false, error_type: "already_running", message: "El backfill ya está en ejecución."};
    }
    const tracker = createBackfillState(form.querySelector("[name='limit']")?.value);
    form.dataset.backfillRunning = "true";
    setBusy(nodes, true);
    renderStatus(nodes, tracker);
    let nextUrl = form.action;
    let body = new FormData(form);
    try {
      while (!tracker.stopped) {
        tracker.requests.push(nextUrl);
        const response = await fetchFn(nextUrl, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Accept": "application/json",
            "X-Requested-With": "fetch",
          },
          body,
        });
        let serverResult = {};
        try {
          serverResult = await response.json();
        } catch (_error) {
          serverResult = {};
        }
        const applied = applyBackfillBatch(tracker, {
          job_id: serverResult.job_id,
          continuation_job_id: serverResult.continuation_job_id,
          has_more: serverResult.has_more,
          remaining: serverResult.remaining,
          found: serverResult.found,
          saved: serverResult.saved,
          imported: serverResult.imported,
          duplicates: serverResult.duplicates,
          errors: serverResult.errors,
          ok: serverResult.ok !== false && serverResult.status !== "failed" && response.ok,
          status: serverResult.status,
          error_type: serverResult.error_type,
          message: serverResult.message,
        });
        renderStatus(nodes, tracker);

        if (!response.ok || applied.ok === false) {
          tracker.status = "failed";
          tracker.message = applied.message || serverResult.message || `HTTP ${response.status}`;
          tracker.errorType = applied.error_type || serverResult.error_type || `http_${response.status}`;
          break;
        }

        if (!applied.continue) {
          tracker.status = "success";
          tracker.message = applied.message || serverResult.message || "Backfill IMAP completado";
          break;
        }

        nextUrl = `${CONTINUE_PATH_PREFIX}${encodeURIComponent(applied.next_job_id)}`;
        body = undefined;
      }
      renderStatus(nodes, tracker);
      return {
        ok: tracker.status !== "failed",
        status: tracker.status,
        error_type: tracker.errorType,
        message: tracker.message,
        batches: tracker.batches,
        found: tracker.totals.found,
        saved: tracker.totals.saved,
        duplicates: tracker.totals.duplicates,
        errors: tracker.totals.errors,
        remaining: tracker.remaining ?? 0,
        requests: tracker.requests.slice(),
        last_job_id: tracker.lastJobId,
      };
    } finally {
      form.dataset.backfillRunning = "false";
      setBusy(nodes, false);
    }
  }

  async function handleBackfillSubmit(event) {
    const form = event.currentTarget;
    if (!form || form.dataset.backfillRunning === "true") {
      event.preventDefault();
      return;
    }
    const submitter = event.submitter;
    const message = form.dataset.confirm || submitter?.dataset?.confirm;
    if (message && !confirm(message)) {
      event.preventDefault();
      return;
    }
    event.preventDefault();
    const nodes = collectNodes(form);
    try {
      await runBackfillSequence(form, fetch, nodes);
    } catch (error) {
      console.warn("No se pudo completar el backfill manual", error);
      const failure = createBackfillState(form.querySelector("[name='limit']")?.value);
      failure.status = "failed";
      failure.message = error && error.message ? error.message : "No se pudo completar el backfill manual.";
      renderStatus(nodes, failure);
    }
  }

  function bindBackfillForms(root = document) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll(SELECTOR_FORM).forEach((form) => {
      if (form.dataset.backfillBound === "true") return;
      form.dataset.backfillBound = "true";
      form.addEventListener("submit", handleBackfillSubmit);
    });
  }

  const api = {
    createBackfillState,
    applyBackfillBatch,
    runBackfillSequence,
    bindBackfillForms,
    collectNodes,
    clampLimit,
    toCount,
  };

  globalThis.AnchiEmailBackfill = api;
  if (typeof document !== "undefined") {
    bindBackfillForms(document);
  }
})();
