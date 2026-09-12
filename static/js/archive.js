(function () {
  "use strict";

  const list = document.querySelector("#archive-list");
  const pagination = document.querySelector("#archive-pagination");
  let page = 1;

  function showError(error) {
    window.showToast(error.message || "Unable to load archive", { type: "error" });
  }

  function render(data) {
    list.replaceChildren();
    if (!data.tasks.length) {
      const empty = document.createElement("p");
      empty.className = "archive-empty";
      empty.textContent = "Archive is empty";
      list.append(empty);
    }
    data.tasks.forEach((task) => {
      const item = document.createElement("article");
      item.className = "archive-task";
      item.tabIndex = 0;
      item.dataset.taskId = task.task_id;
      const id = document.createElement("strong");
      id.textContent = task.task_id;
      const title = document.createElement("span");
      title.textContent = task.title;
      const created = document.createElement("time");
      created.textContent = task.created_at;
      const status = document.createElement("em");
      status.textContent = "ARCHIVE";
      item.append(id, title, created, status);
      const open = (event) => {
        if (event.ctrlKey || event.metaKey) window.open(`/tasks/${encodeURIComponent(task.task_id)}`, "_blank");
        else window.location.href = `/tasks/${encodeURIComponent(task.task_id)}`;
      };
      item.addEventListener("click", open);
      item.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(event); } });
      list.append(item);
    });
    pagination.replaceChildren();
    for (let number = 1; number <= data.pages; number += 1) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = number;
      button.setAttribute("aria-label", `Archive page ${number}`);
      button.setAttribute("aria-current", number === data.page ? "page" : "false");
      button.disabled = number === data.page;
      button.addEventListener("click", () => load(number));
      pagination.append(button);
    }
  }

  async function load(nextPage) {
    try {
      const response = await fetch(`/api/archive/tasks?page=${nextPage}`);
      if (!response.ok) throw new Error("Unable to load archive");
      page = nextPage;
      render(await response.json());
    } catch (error) { showError(error); }
  }

  load(page);
})();
