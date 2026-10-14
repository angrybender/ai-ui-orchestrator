(function () {
  "use strict";

  const key = "orchestrator.newTaskDraft";
  let draft = { title: "", description: "" };
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    if (saved && typeof saved.title === "string" && typeof saved.description === "string") {
      draft = { title: saved.title, description: saved.description };
    }
  } catch (_) { /* Storage may be unavailable; keep the draft in memory. */ }

  window.taskDraft = {
    read: () => ({ ...draft }),
    write: (title, description) => {
      draft = { title, description };
      try { localStorage.setItem(key, JSON.stringify(draft)); } catch (_) { /* Memory fallback. */ }
    },
    clear: () => {
      draft = { title: "", description: "" };
      try { localStorage.removeItem(key); } catch (_) { /* Memory fallback. */ }
    }
  };
})();
