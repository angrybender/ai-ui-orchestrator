(function () {
  "use strict";
  const button = document.querySelector("#agent-recovery-button");
  if (!button) return;
  let busy = false;
  let blockedRuns = new Set();

  window.agentRecovery = {
    notify(runs) {
      const added = runs.filter(run => !blockedRuns.has(run.run_id));
      blockedRuns = new Set(runs.map(run => run.run_id));
      if (!added.length) return;
      const names = added.map(run => run.task_id || "Deleted task").join(", ");
      window.showToast(`Agent queue blocked by unconfirmed remote stop: ${names}. Verify the remote agent has stopped, then use Agent Recovery on Board.`, { type: "error", duration: 6000 });
    }
  };

  async function request(options) {
    const response = await fetch("/api/agent/recovery", options);
    const data = await response.json();
    return { ok: response.ok, data: data };
  }

  button.addEventListener("click", async function () {
    if (busy || button.disabled) return;
    busy = true;
    window.spinner.start(button);
    button.querySelector('span:last-child').textContent = 'Recovering…';
    try {
      const candidates = await request();
      if (!candidates.ok) {
        window.showToast(candidates.data.detail || "Unable to load recovery state", { type: "error" });
        return;
      }
      const run = candidates.data.runs[0];
      if (!run) {
        window.showToast("No agent runs require recovery");
        return;
      }
      const name = run.task_id || "Deleted task";
      if (!window.confirm(`Confirm that the remote agent and its processes for ${name} have already stopped.\nRecovery releases this task's queue block; it does not stop the agent.`)) return;
      const result = await request({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_id: run.run_id }) });
      if (!result.ok) {
        window.showToast(result.data.detail || "Unable to recover agent", { type: "error" });
        return;
      }
      window.showToast(result.data.remaining
        ? `Agent recovered. Remaining blocked runs: ${result.data.remaining}`
        : "Agent recovered. Queue unblocked.");
    } catch (error) {
      if (!error.reported) window.showToast("Unable to recover agent. Please try again.", { type: "error" });
    } finally {
      window.spinner.stop(button);
      busy = false;
    }
  });
})();
