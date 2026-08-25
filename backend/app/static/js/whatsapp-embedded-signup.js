(() => {
  "use strict";

  const roots = Array.from(document.querySelectorAll("[data-whatsapp-embedded-signup]"));
  if (!roots.length) return;
  const configuredRoots = roots.filter((root) => root.dataset.metaConfigured === "true");
  if (!configuredRoots.length) return;

  let activeFlow = null;

  const setStatus = (root, message, state = "idle") => {
    const status = root.querySelector("[data-whatsapp-signup-status]");
    if (status) {
      status.textContent = message;
      status.dataset.state = state;
    }
  };

  const setButtonBusy = (root, busy) => {
    const button = root.querySelector("[data-whatsapp-signup-button]");
    if (!button) return;
    button.disabled = busy || root.dataset.metaConfigured !== "true";
    button.setAttribute("aria-busy", busy ? "true" : "false");
  };

  const isFacebookOrigin = (origin) => {
    try {
      const url = new URL(origin);
      return url.protocol === "https:" && (url.hostname === "facebook.com" || url.hostname.endsWith(".facebook.com"));
    } catch (_error) {
      return false;
    }
  };

  const finishIfReady = async () => {
    if (!activeFlow || activeFlow.completing || !activeFlow.code || !activeFlow.sessionInfo) return;
    const root = activeFlow.root;
    const sessionData = activeFlow.sessionInfo.data || {};
    const onboardingMode = activeFlow.sessionInfo.event === "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"
      ? "coexistence"
      : "cloud_api";
    if (!sessionData.waba_id || (onboardingMode !== "coexistence" && !sessionData.phone_number_id)) {
      setStatus(root, "Meta no devolvió los datos necesarios de la cuenta. Repite el proceso.", "error");
      setButtonBusy(root, false);
      activeFlow = null;
      return;
    }
    activeFlow.completing = true;
    setStatus(root, "Validando el número y preparando el webhook…", "loading");
    try {
      const response = await fetch(root.dataset.completeUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({
          code: activeFlow.code,
          waba_id: String(sessionData.waba_id),
          phone_number_id: String(sessionData.phone_number_id || ""),
          business_id: String(sessionData.business_id || ""),
          onboarding_mode: onboardingMode,
          state: root.dataset.signupState,
          return_to: root.dataset.returnTo,
        }),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) {
        throw new Error(result.message || "No se pudo completar la conexión con Meta.");
      }
      setStatus(root, result.message || "WhatsApp conectado correctamente.", "success");
      if (result.redirect_url) window.location.assign(result.redirect_url);
    } catch (error) {
      setStatus(root, error instanceof Error ? error.message : "No se pudo completar la conexión con Meta.", "error");
      setButtonBusy(root, false);
      activeFlow = null;
    }
  };

  const handleEmbeddedSignupEvent = (event) => {
    if (!activeFlow || !isFacebookOrigin(event.origin)) return;
    let payload = event.data;
    if (typeof payload === "string") {
      try {
        payload = JSON.parse(payload);
      } catch (_error) {
        return;
      }
    }
    if (!payload || payload.type !== "WA_EMBEDDED_SIGNUP") return;
    if (["FINISH", "FINISH_ONLY_WABA", "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"].includes(payload.event)) {
      activeFlow.sessionInfo = payload;
      finishIfReady();
      return;
    }
    if (payload.event === "CANCEL") {
      setStatus(activeFlow.root, "Acceso cancelado. No se ha guardado ningún cambio.", "idle");
      setButtonBusy(activeFlow.root, false);
      activeFlow = null;
      return;
    }
    if (payload.event === "ERROR") {
      const message = payload.data && payload.data.error_message
        ? payload.data.error_message
        : "Meta no pudo completar Embedded Signup.";
      setStatus(activeFlow.root, message, "error");
      setButtonBusy(activeFlow.root, false);
      activeFlow = null;
    }
  };

  const launchSignup = (root) => {
    if (!window.FB) {
      setStatus(root, "El acceso de Meta todavía está cargando. Inténtalo de nuevo en unos segundos.", "error");
      return;
    }
    setButtonBusy(root, true);
    setStatus(root, "Abriendo el acceso seguro de Meta…", "loading");
    activeFlow = { root, code: null, sessionInfo: null, completing: false };
    const extras = {
      setup: {},
      featureType: root.dataset.metaFeatureType,
      sessionInfoVersion: "3",
    };
    if (root.dataset.metaFlowVersion) extras.version = root.dataset.metaFlowVersion;
    window.FB.login(
      (response) => {
        if (!activeFlow || activeFlow.root !== root) return;
        const code = response && response.authResponse && response.authResponse.code;
        if (!code) {
          setStatus(root, "El acceso se canceló o Meta no devolvió autorización.", "idle");
          setButtonBusy(root, false);
          activeFlow = null;
          return;
        }
        activeFlow.code = code;
        finishIfReady();
      },
      {
        config_id: root.dataset.metaConfigId,
        response_type: "code",
        override_default_response_type: true,
        extras,
      },
    );
  };

  const initializeSdk = () => {
    const primary = configuredRoots[0];
    window.FB.init({
      appId: primary.dataset.metaAppId,
      autoLogAppEvents: true,
      xfbml: false,
      version: primary.dataset.metaApiVersion,
    });
    configuredRoots.forEach((root) => {
      setButtonBusy(root, false);
      setStatus(root, "Listo para iniciar Embedded Signup.", "ready");
    });
  };

  window.addEventListener("message", handleEmbeddedSignupEvent);
  roots.forEach((root) => {
    const button = root.querySelector("[data-whatsapp-signup-button]");
    if (!button || root.dataset.metaConfigured !== "true") return;
    setButtonBusy(root, true);
    setStatus(root, "Cargando el acceso seguro de Meta…", "loading");
    button.addEventListener("click", () => launchSignup(root));
  });

  if (window.FB) {
    initializeSdk();
    return;
  }
  const previousAsyncInit = window.fbAsyncInit;
  window.fbAsyncInit = () => {
    if (typeof previousAsyncInit === "function") previousAsyncInit();
    initializeSdk();
  };
  if (!document.getElementById("facebook-jssdk")) {
    const script = document.createElement("script");
    script.id = "facebook-jssdk";
    script.async = true;
    script.defer = true;
    script.crossOrigin = "anonymous";
    script.src = "https://connect.facebook.net/en_US/sdk.js";
    script.onerror = () => {
      roots.forEach((root) => {
        if (root.dataset.metaConfigured === "true") {
          setStatus(root, "No se pudo cargar el acceso de Meta. Comprueba la conexión y vuelve a intentarlo.", "error");
        }
      });
    };
    document.head.appendChild(script);
  }
})();
