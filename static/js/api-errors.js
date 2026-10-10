(function () {
  "use strict";

  const originalFetch = window.fetch.bind(window);
  const failures = new Map();

  function describe(value) {
    if (typeof value === "string") return value.trim();
    if (Array.isArray(value)) return value.map(describe).filter(Boolean).join("; ");
    if (value && typeof value === "object") {
      if (typeof value.msg === "string") {
        const location = Array.isArray(value.loc) ? value.loc.filter(part => part !== "body").join(".") : "";
        return (location ? location + ": " : "") + value.msg;
      }
      return Object.entries(value).map(([key, item]) => {
        const message = describe(item);
        return message ? key + ": " + message : "";
      }).filter(Boolean).join("; ");
    }
    return value == null ? "" : String(value);
  }

  window.fetch = async function (input, options) {
    const url = input instanceof Request ? input.url : String(input);
    const method = String(options?.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    const key = method + " " + url;
    const polling = method === "GET" && /\/api\/board\/tasks\/[^/?]+\/chat(?:[?#]|$)/.test(url);
    try {
      const response = await originalFetch(input, options);
      if (response.ok) {
        failures.delete(key);
        return response;
      }
      let detail;
      let message = "";
      try {
        const body = await response.text();
        try {
          const payload = JSON.parse(body);
          detail = payload && typeof payload === "object" ? payload.detail ?? payload.message ?? payload.error : payload;
          message = describe(detail);
        } catch (error) {
          // HTML error pages and tracebacks are not suitable user-facing diagnostics.
          if (!/<[^>]+>/.test(body) && !/Traceback \(most recent call last\)/.test(body)) message = body.trim();
        }
      } catch (error) { /* A status remains useful when the error body cannot be read. */ }
      const error = new Error(message || `Request failed (HTTP ${response.status}${response.statusText ? " " + response.statusText : ""})`);
      error.detail = detail;
      error.status = response.status;
      throw error;
    } catch (error) {
      if (error.name === "AbortError") throw error;
      if (!error.status) error.message = "Unable to connect to the server. Please try again.";
      // Suppress repeated failures of background polling until recovery; actions remain visible.
      if (!polling || failures.get(key) !== error.message) {
        window.showToast(error.message, { type: "error", duration: 6000 });
      }
      if (polling) failures.set(key, error.message);
      error.reported = true;
      throw error;
    }
  };
}());
