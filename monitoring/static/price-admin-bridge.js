(() => {
  "use strict";

  document.addEventListener("DOMContentLoaded", () => {
    const link = document.createElement("a");
    link.href = "/price/entry";
    link.textContent = "Ввод цен →";
    link.style.cssText = "position:fixed;bottom:20px;left:20px;z-index:9999;padding:12px 18px;background:#075e54;color:white;border-radius:12px;font:600 15px system-ui;text-decoration:none;box-shadow:0 4px 18px #0003";
    document.body.appendChild(link);
  });

  const upstreamPrefix = "/price/api/v1/";
  const portalPrefix = "/monitoring/api/prices/admin/";
  const originalFetch = window.fetch.bind(window);
  let snapshotId = null;

  const pollSnapshot = async () => {
    if (document.hidden) return;
    try {
      const response = await originalFetch("/monitoring/api/prices", {
        credentials: "same-origin",
        cache: "no-store",
        headers: {Accept: "application/json"},
      });
      if (!response.ok) return;
      const payload = await response.json();
      const current = String(payload?.data?.snapshot?.snapshot_id || "");
      if (!current) return;
      if (snapshotId === null) snapshotId = current;
      else if (current !== snapshotId) window.location.reload();
    } catch (_) {
      // A temporary monitoring error must not interrupt price administration.
    }
  };

  pollSnapshot();
  window.setInterval(pollSnapshot, 10000);

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
