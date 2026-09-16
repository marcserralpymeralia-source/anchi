(function () {
  "use strict";

  const meta = document.querySelector('meta[name="csrf-token"]');
  const token = meta && meta.content;
  if (!token) return;

  const mutating = new Set(["POST", "PUT", "PATCH", "DELETE"]);
  const sameOrigin = (value) => {
    try {
      return new URL(value, window.location.href).origin === window.location.origin;
    } catch (_) {
      return false;
    }
  };

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const method = (form.method || "get").toUpperCase();
    if (!mutating.has(method) || !sameOrigin(form.action || window.location.href)) return;
    if (!form.querySelector('input[name="_csrf_token"]')) {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "_csrf_token";
      input.value = token;
      form.appendChild(input);
    }
  }, true);

  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const options = init ? { ...init } : {};
    const method = (options.method || (input && input.method) || "GET").toUpperCase();
    const url = typeof input === "string" ? input : (input && input.url) || window.location.href;
    if (mutating.has(method) && sameOrigin(url)) {
      const headers = new Headers(options.headers || (input && input.headers) || {});
      if (!headers.has("X-CSRF-Token")) headers.set("X-CSRF-Token", token);
      options.headers = headers;
    }
    return nativeFetch(input, options);
  };
})();
