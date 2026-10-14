(function () {
  "use strict";
  const button = document.querySelector("#clear-db-button");
  if (!button) return;
  let busy = false;

  button.addEventListener("click", async function () {
    if (busy || button.disabled) return;
    if (!window.confirm("Permanently clear the entire local database and local task attachments?\nAll tasks (including Archive), attachment files, chat messages and agent history will be deleted. This cannot be undone.\nEnsure remote agents and their processes have stopped. Settings, logs and remote directories are preserved.")) return;
    busy = true;
    window.spinner.start(button);
    button.querySelector("span:last-child").textContent = "Clearing…";
    try {
      const response = await fetch("/api/database/clear", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirmed: true })
      });
      if (!response.ok) throw new Error("Unable to clear database.");
      window.showToast("Local database cleared.");
    } catch (error) {
      if (!error.reported) window.showToast("Unable to clear database. Please try again.", { type: "error" });
    } finally {
      window.spinner.stop(button);
      busy = false;
    }
  });
})();
