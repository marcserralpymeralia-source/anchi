(function () {
  function setText(node, value) {
    if (node) node.textContent = value || "";
  }

  function setValue(form, selector, value) {
    const node = form.querySelector(selector);
    if (node && value !== undefined && value !== null) node.value = value;
  }

  function updateSslFlag(form) {
    const security = form.querySelector("[data-email-manual-security]");
    const useSsl = form.querySelector("[data-email-manual-use-ssl]");
    if (security && useSsl) useSsl.value = security.value === "ssl_tls" ? "on" : "off";
  }

  function endpointDescription(endpoint) {
    if (!endpoint) return "";
    const security = endpoint.security === "ssl_tls" ? "SSL/TLS" : (endpoint.security === "starttls" ? "STARTTLS" : "sin cifrado");
    return `${endpoint.protocol.toUpperCase()} verificado: ${endpoint.host}:${endpoint.port} · ${security}`;
  }

  function setBusy(button, busy) {
    if (!button) return;
    if (busy) {
      button.dataset.originalText = button.textContent || "";
      button.textContent = button.dataset.loadingText || "Comprobando...";
      button.disabled = true;
    } else {
      button.textContent = button.dataset.originalText || button.textContent;
      delete button.dataset.originalText;
      button.disabled = false;
    }
  }

  function showManual(panel, toggle, visible) {
    if (!panel || !toggle) return;
    panel.hidden = !visible;
    toggle.setAttribute("aria-expanded", visible ? "true" : "false");
    toggle.textContent = visible ? "Ocultar configuración manual" : "Configuración manual";
  }

  function copyDetectedConfig(root, result) {
    const manual = root.querySelector("[data-email-manual-form]");
    const auto = root.querySelector("[data-email-autoconfig-form]");
    const imap = (result.imap && result.imap.protocol === "imap" ? result.imap : null)
      || (result.suggested_imap && result.suggested_imap.protocol === "imap" ? result.suggested_imap : null);
    if (!manual || !auto || !imap) return;
    const provider = ["gmail", "microsoft365", "imap"].includes(result.provider) ? result.provider : "imap";
    const email = result.email || auto.querySelector("[name='email']")?.value || "";
    const password = auto.querySelector("[name='password']")?.value || "";
    setValue(manual, "[data-email-manual-provider]", provider);
    setValue(manual, "[data-email-manual-email]", email);
    setValue(manual, "[data-email-manual-username]", imap.username || email);
    setValue(manual, "[data-email-manual-password]", password);
    setValue(manual, "[data-email-manual-host]", imap.host);
    setValue(manual, "[data-email-manual-port]", imap.port);
    setValue(manual, "[data-email-manual-security]", imap.security);
    setValue(manual, "[data-email-manual-folder]", imap.folder || "INBOX");
    updateSslFlag(manual);
  }

  function renderResult(root, result) {
    const status = root.querySelector("[data-email-autoconfig-status]");
    const title = root.querySelector("[data-email-autoconfig-status-title]");
    const message = root.querySelector("[data-email-autoconfig-status-message]");
    const details = root.querySelector("[data-email-autoconfig-details]");
    if (!status) return;
    status.hidden = false;
    status.dataset.state = result.detected ? "success" : "warning";
    setText(title, result.detected ? "Configuración encontrada" : "Configuración no verificada");
    setText(message, result.message || "");
    const lines = [];
    if (result.imap) lines.push(endpointDescription(result.imap));
    else if (result.suggested_imap) lines.push(`IMAP encontrado (sin verificar): ${result.suggested_imap.host}:${result.suggested_imap.port}`);
    if (result.pop3) lines.push(endpointDescription(result.pop3));
    else if (result.suggested_pop3) lines.push(`POP3 encontrado (sin verificar): ${result.suggested_pop3.host}:${result.suggested_pop3.port}`);
    if (result.smtp) lines.push(endpointDescription(result.smtp));
    if (!result.imap && (result.pop3 || result.suggested_pop3)) lines.push("Anchi necesita IMAP para sincronizar automáticamente.");
    setText(details, lines.join(" · "));
    copyDetectedConfig(root, result);
  }

  function initialize(root) {
    const autoForm = root.querySelector("[data-email-autoconfig-form]");
    const manualForm = root.querySelector("[data-email-manual-form]");
    const manualPanel = root.querySelector("[data-email-manual-panel]");
    const manualToggle = root.querySelector("[data-email-manual-toggle]");
    const security = manualForm?.querySelector("[data-email-manual-security]");
    if (!autoForm || !manualForm || !manualPanel || !manualToggle) return;

    manualToggle.addEventListener("click", function () {
      showManual(manualPanel, manualToggle, manualPanel.hidden);
    });
    security?.addEventListener("change", function () {
      updateSslFlag(manualForm);
    });
    manualForm.addEventListener("submit", function () {
      updateSslFlag(manualForm);
    });

    autoForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      if (autoForm.dataset.running === "true") return;
      autoForm.dataset.running = "true";
      const submit = autoForm.querySelector("button[type='submit']");
      setBusy(submit, true);
      try {
        const response = await fetch(autoForm.action, {
          method: "POST",
          credentials: "same-origin",
          headers: {"Accept": "application/json", "X-Requested-With": "fetch"},
          body: new FormData(autoForm),
        });
        let result = {};
        try {
          result = await response.json();
        } catch (_error) {
          result = {};
        }
        if (!response.ok || result.ok === false) {
          throw new Error(result.message || "No se ha podido comprobar la cuenta.");
        }
        renderResult(root, result);
      } catch (error) {
        renderResult(root, {detected: false, message: error.message || "No se ha podido comprobar la cuenta."});
      } finally {
        autoForm.dataset.running = "false";
        setBusy(submit, false);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-email-autoconfig-root]").forEach(initialize);
  });
})();
