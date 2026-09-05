(() => {
  "use strict";

  const upstreamPrefix = "/price/api/v1/";
  const portalPrefix = "/monitoring/api/prices/admin/";
  const originalFetch = window.fetch.bind(window);

  const csrfToken = () => {
    const prefix = "__Host-texnikach_monitoring_csrf=";
    const item = document.cookie
      .split("; ")
      .find(value => value.startsWith(prefix));
    return item ? decodeURIComponent(item.slice(prefix.length)) : "";
  };

  window.fetch = (input, options = {}) => {
    if (typeof input !== "string" || !input.startsWith(upstreamPrefix)) {
      return originalFetch(input, options);
    }
    const headers = new Headers(options.headers || {});
    const csrf = csrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
    return originalFetch(
      portalPrefix + input.slice(upstreamPrefix.length),
      {...options, credentials: "same-origin", headers},
    );
  };
})();
