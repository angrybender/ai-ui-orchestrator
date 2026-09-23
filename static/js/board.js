(function () {
  "use strict";

  const statuses = ["BACKLOG", "OPEN", "WAIT", "IN PROGRESS", "REVIEW", "DONE"];
  const transitions = { "BACKLOG": ["OPEN", "ARCHIVE"], "WAIT": ["ARCHIVE"], "IN PROGRESS": ["BACKLOG"], "REVIEW": ["OPEN", "BACKLOG", "DONE"], "DONE": ["ARCHIVE"] };
  let tasks = [];
  let editing = null;
  let removedAttachments = [];
  const $ = (selector) => document.querySelector(selector);

  function apiError(response) {
    return response.text().then((text) => {
      let detail = "Request failed";
      try {
        const body = JSON.parse(text);
        detail = body.detail;
      } catch (error) {
        detail = text || detail;
      }
      const error = new Error(typeof detail === "string" ? detail : "Validation failed");
      error.detail = detail;
      throw error;
    });
  }

  function clearErrors() {
    document.querySelectorAll(".field-error").forEach((element) => { element.textContent = ""; });
  }

  function showErrors(detail) {
    clearErrors();
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      Object.keys(detail).forEach((key) => {
        const error = document.querySelector(`[data-error="${key}"]`);
        if (error) error.textContent = detail[key];
      });
    }
    window.showToast("Please check the form", { type: "error" });
  }

  function receiveTasks(snapshot) {
    if (document.querySelector(".dragging")) {
      pendingSnapshot = snapshot;
      return;
    }
    tasks = snapshot;
    render(false);
  }

  let pendingSnapshot = null;

  function render(refresh = true) {
    if (refresh && $("#board") && window.boardLive) {
      pendingSnapshot = null;
      window.boardLive.refresh(receiveTasks);
    }
    statuses.forEach((status) => {
      const list = document.querySelector(`.task-list[data-status="${status}"]`);
      if (!list) return;
      list.replaceChildren();
      const columnTasks = tasks.filter((task) => task.status === status);
      const count = document.querySelector(`.board-column[data-status="${status}"] .column-count`);
      if (count) count.textContent = columnTasks.length;
      columnTasks.forEach((task) => {
        const card = document.createElement("article");
        card.className = `task-card status-${task.status.toLowerCase().replaceAll(" ", "-")}${task.is_error ? " is-error" : ""}`;
        card.draggable = true;
        card.dataset.taskId = task.task_id;
        const bar = document.createElement("div");
        bar.className = "task-card-bar";
        const id = document.createElement("p");
        id.className = "task-id";
        id.textContent = task.task_id;
        const title = document.createElement("p");
        title.className = "task-title";
        title.textContent = task.title;
        card.append(bar, id, title);
        if (task.status === "IN PROGRESS") {
          const extra = document.createElement("div");
          extra.className = "task-extra-status";
          if (["Init", "Agent"].includes(task.phase)) extra.dataset.phase = task.phase;
          extra.append(document.createElement("span"), document.createTextNode(
            ["Init", "Agent"].includes(task.phase) ? task.phase : "In progress"));
          card.append(extra);
        }
        card.addEventListener("click", (event) => {
          if (event.ctrlKey || event.metaKey) {
            window.open(`/tasks/${encodeURIComponent(task.task_id)}`, "_blank");
          } else {
            openForm(task);
          }
        });
        card.addEventListener("dragstart", () => { card.classList.add("dragging"); });
        card.addEventListener("dragend", () => {
          card.classList.remove("dragging");
          if (pendingSnapshot) {
            const snapshot = pendingSnapshot;
            pendingSnapshot = null;
            receiveTasks(snapshot);
          }
        });
        list.append(card);
      });
    });
  }

  function resetFileUploads() {
    const list = $("#file-upload-list");
    if (!list) return;
    list.replaceChildren();
    addFileUpload();
  }

  function addFileUpload() {
    const list = $("#file-upload-list");
    if (!list) return;
    const row = document.createElement("div");
    row.className = "file-upload-row";
    const input = document.createElement("input");
    input.className = "task-file-input";
    input.type = "file";
    const remove = document.createElement("button");
    remove.className = "button file-upload-remove";
    remove.type = "button";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => row.remove());
    row.append(input, remove);
    list.append(row);
    updateFileUploadButtons();
  }

  function updateFileUploadButtons() {
    const rows = document.querySelectorAll(".file-upload-row");
    rows.forEach((row) => { row.querySelector(".file-upload-remove").hidden = rows.length === 1; });
  }

  function getSelectedFiles() {
    return Array.from(document.querySelectorAll(".task-file-input"))
      .map((input) => input.files[0])
      .filter(Boolean);
  }

  function openForm(task) {
    const form = $("#task-form");
    if (!form) return;
    const mode = $("#task-mode");
    const key = $("#task-key");
    const dialogTitle = $("#task-dialog-title");
    const taskId = $("#task-id");
    const title = $("#task-title");
    const description = $("#task-description");
    const status = $("#task-status");
    const statusControl = $("#status-control");
    const modal = $("#task-modal");
    if (!mode || !key || !dialogTitle || !taskId || !title || !description || !status || !statusControl || !modal) return;
    editing = task || null;
    removedAttachments = [];
    clearErrors();
    mode.value = task ? "edit" : "create";
    key.value = task ? task.task_id : "";
    dialogTitle.textContent = task ? `Edit ${task.task_id}` : "New task";
    taskId.value = task ? task.task_id : "";
    taskId.readOnly = Boolean(task);
    title.value = task ? task.title : "";
    description.value = task ? task.description : "";
    status.replaceChildren();
    const availableStatuses = task ? [task.status].concat(transitions[task.status] || []) : ["BACKLOG"];
    availableStatuses.forEach((statusValue) => {
      const option = document.createElement("option");
      option.value = statusValue;
      option.textContent = statusValue;
      status.append(option);
    });
    status.value = task ? task.status : "BACKLOG";
    statusControl.hidden = !task;
    const archiveMode = window.openTaskStatus === "ARCHIVE";
    const deleteButton = $("#delete-task");
    if (deleteButton) deleteButton.hidden = !task || !["BACKLOG", "ARCHIVE"].includes(task.status);
    if (archiveMode) {
      status.disabled = true;
      document.querySelectorAll("#task-form input, #task-form textarea, #task-form select").forEach((field) => { field.disabled = true; });
      $("#add-file-upload")?.remove();
      $("#file-upload-list")?.remove();
    }
    const stopButton = $("#stop-task");
    if (stopButton) stopButton.hidden = !task || task.status !== "IN PROGRESS";
    const archiveButton = $("#archive-task");
    if (archiveButton) archiveButton.hidden = !task || !["WAIT", "DONE"].includes(task.status);
    ["#reopen-task", "#done-task"].forEach((selector) => {
      const button = $(selector);
      if (button) button.hidden = !task || task.status !== "REVIEW";
    });
    const cancel = $("#cancel-task");
    if (cancel) cancel.hidden = !$("#board");
    if (!archiveMode) resetFileUploads();
    renderAttachments(task ? task.attachments : []);
    if (archiveMode) {
      document.querySelectorAll("#attachment-list button").forEach((button) => { button.disabled = true; });
    }
    modal.hidden = false;
    if (window.taskChat) window.taskChat.open(task, (nextStatus) => {
      if (!editing || editing.status === nextStatus) return;
      editing.status = nextStatus;
      status.replaceChildren();
      [nextStatus].concat(transitions[nextStatus] || []).forEach((value) => {
        const option = document.createElement("option");
        option.value = option.textContent = value;
        status.append(option);
      });
      ["#reopen-task", "#done-task"].forEach((selector) => { const button = $(selector); if (button) button.hidden = nextStatus !== "REVIEW"; });
      if (stopButton) stopButton.hidden = nextStatus !== "IN PROGRESS";
      if (deleteButton) deleteButton.hidden = !["BACKLOG", "ARCHIVE"].includes(nextStatus);
      if (archiveButton) archiveButton.hidden = !["WAIT", "DONE"].includes(nextStatus);
    }, () => { render(); });
    title.focus();
  }

  function renderAttachments(attachments) {
    const list = $("#attachment-list");
    list.replaceChildren();
    (attachments || []).forEach((attachment) => {
      const row = document.createElement("div");
      row.className = "attachment-row";
      const name = document.createElement("span");
      name.textContent = attachment.original_name;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "Remove";
      remove.addEventListener("click", () => {
        removedAttachments.push(attachment.id);
        row.remove();
      });
      row.append(name, remove);
      list.append(row);
    });
  }

  function closeForm() {
    if (window.taskChat) window.taskChat.close();
    $("#task-modal").hidden = true;
    editing = null;
  }

  function requestCloseForm() {
    if ($("#task-form")?.dataset.spinnerActive !== "true") closeForm();
  }

  async function reviewTask(destination, button) {
    if (!editing || editing.status !== "REVIEW" || button.disabled) return;
    if (window.spinner && !window.spinner.start(button)) return;
    const taskId = editing.task_id;
    try {
      const response = await fetch(`/api/board/tasks/${encodeURIComponent(taskId)}/move`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: destination, position: 0 })
      });
      if (!response.ok) return await apiError(response);
      tasks = (await response.json()).tasks;
      render();
      if (window.openTaskId) {
        openForm(tasks.find((task) => task.task_id === taskId));
      } else {
        closeForm();
      }
      window.showToast(`Task moved to ${destination}`);
    } catch (error) { window.showToast(error.message || "Unable to move task", { type: "error" }); }
    finally { if (window.spinner) window.spinner.stop(button); }
  }

  async function archiveTask() {
    if (!editing || !["WAIT", "DONE"].includes(editing.status)) return;
    const button = $("#archive-task");
    if (button.disabled) return;
    if (window.spinner && !window.spinner.start(button)) return;
    try {
      const response = await fetch(`/api/board/tasks/${encodeURIComponent(editing.task_id)}/move`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "ARCHIVE", position: 0 })
      });
      if (!response.ok) return await apiError(response);
      closeForm();
      if (window.openTaskId) {
        window.location.href = "/archive";
        return;
      }
      tasks = (await response.json()).tasks;
      render();
      window.showToast("Task archived");
    } catch (error) { window.showToast(error.message || "Unable to archive task", { type: "error" }); }
    finally { if (window.spinner) window.spinner.stop(button); }
  }

  async function deleteTask() {
    const button = $("#delete-task");
    if (!editing || !["BACKLOG", "ARCHIVE"].includes(editing.status) || button.disabled) return;
    if (!window.confirm("Delete this task and its files?")) return;
    if (window.spinner && !window.spinner.start(button)) return;
    const taskId = editing.task_id;
    const archive = editing.status === "ARCHIVE";
    try {
      const response = await fetch(`/api/${archive ? "archive" : "board"}/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
      if (!response.ok) return await apiError(response);
      if (window.openTaskId) {
        window.location.href = archive ? "/archive" : "/";
        return;
      }
      tasks = tasks.filter((task) => task.task_id !== taskId);
      render();
      closeForm();
      window.showToast("Task deleted");
    } catch (error) { window.showToast(error.message || "Unable to delete task", { type: "error" }); }
    finally { if (window.spinner) window.spinner.stop(button); }
  }

  async function load() {
    try {
      const response = await fetch(window.openTaskStatus === "ARCHIVE"
        ? `/api/archive/tasks/${encodeURIComponent(window.openTaskId)}`
        : "/api/board/tasks");
      if (!response.ok) return await apiError(response);
      if (window.openTaskStatus === "ARCHIVE") {
        openForm(await response.json());
        return;
      }
      tasks = (await response.json()).tasks;
      render();
      if (window.openTaskId) {
        const task = tasks.find((item) => item.task_id === window.openTaskId);
        if (task) openForm(task);
      }
    } catch (error) { window.showToast(error.message || "Unable to load tasks", { type: "error" }); }
  }

  async function save(event, stop = false) {
    event.preventDefault();
    if (stop && (!editing || editing.status !== "IN PROGRESS")) return;
    const stoppedId = stop ? editing.task_id : null;
    const button = $(stop ? "#stop-task" : "#save-task");
    if (!button || button.disabled) return;
    if (window.spinner && !window.spinner.start(button)) return;
    try {
      const selectedFiles = getSelectedFiles();
      const taskId = $("#task-id");
      const title = $("#task-title");
      const description = $("#task-description");
      const status = $("#task-status");
      if (!taskId || !title || !description || !status) return;
      const url = editing ? `/api/board/tasks/${encodeURIComponent(editing.task_id)}` : "/api/board/tasks";
      let request;
      if (selectedFiles.length) {
        const data = new FormData();
        data.append("task_id", $("#task-id").value);
        data.append("title", $("#task-title").value);
        data.append("description", $("#task-description").value);
        if (editing) {
          data.append("status", stop ? "BACKLOG" : $("#task-status").value);
          data.append("remove_attachment_ids", JSON.stringify(removedAttachments));
        }
        selectedFiles.forEach((file) => data.append("files", file));
        request = { method: editing ? "PUT" : "POST", body: data };
      } else {
        const payload = { title: $("#task-title").value, description: $("#task-description").value };
        if (editing) {
          payload.status = stop ? "BACKLOG" : $("#task-status").value;
          payload.remove_attachment_ids = removedAttachments;
        } else {
          payload.task_id = $("#task-id").value;
        }
        request = { method: editing ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) };
      }
      const response = await fetch(url, request);
      if (!response.ok) return await apiError(response);
      closeForm();
      await load();
      if (stop && !window.openTaskId) openForm(tasks.find(task => task.task_id === stoppedId));
      window.showToast(stop ? "Task stopped" : "Task saved");
    } catch (error) {
      if (error.detail) showErrors(error.detail); else window.showToast(error.message || "Unable to save task", { type: "error" });
    } finally { if (window.spinner) window.spinner.stop(button); }
  }

  async function move(task, list) {
    const destination = list.dataset.status;
    if (destination !== task.status && !(transitions[task.status] || []).includes(destination)) {
      window.showToast(`Cannot move from ${task.status} to ${destination}`, { type: "error" });
      render();
      return;
    }
    const position = Array.from(list.querySelectorAll(".task-card")).findIndex((card) => card.dataset.taskId === task.task_id);
    try {
      const response = await fetch(`/api/board/tasks/${encodeURIComponent(task.task_id)}/move`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: destination, position: Math.max(0, position) }) });
      if (!response.ok) return await apiError(response);
      tasks = (await response.json()).tasks;
      render();
    } catch (error) { window.showToast(error.message || "Unable to move task", { type: "error" }); render(); }
  }

  function getDragAfterElement(container, y) {
    const cards = [...container.querySelectorAll(".task-card:not(.dragging)")];
    return cards.reduce((closest, card) => {
      const box = card.getBoundingClientRect();
      const offset = y - box.top - box.height / 2;
      return offset < 0 && offset > closest.offset ? { offset, element: card } : closest;
    }, { offset: Number.NEGATIVE_INFINITY }).element;
  }

  function setupDrop() {
    document.querySelectorAll(".task-list").forEach((list) => {
      list.addEventListener("dragover", (event) => { event.preventDefault(); });
      list.addEventListener("drop", (event) => {
        event.preventDefault();
        const dragging = document.querySelector(".dragging");
        const taskId = dragging?.dataset.taskId;
        const task = tasks.find((item) => item.task_id === taskId);
        if (!task) return;
        const afterElement = getDragAfterElement(list, event.clientY);
        if (afterElement) {
          list.insertBefore(dragging, afterElement);
        } else {
          list.append(dragging);
        }
        move(task, list);
      });
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    const form = $("#task-form");
    if (!form) return;
    $("#new-task-button")?.addEventListener("click", async () => {
      if (form.dataset.spinnerActive === "true") return;
      try {
        const response = await fetch("/api/board/tasks/next-id");
        if (!response.ok) return await apiError(response);
        const data = await response.json();
        if (form.dataset.spinnerActive === "true") return;
        openForm(null);
        $("#task-id").value = data.task_id;
      } catch (error) { window.showToast(error.message || "Unable to generate task ID", { type: "error" }); }
    });
    $("#close-task")?.addEventListener("click", requestCloseForm);
    $("#cancel-task")?.addEventListener("click", requestCloseForm);
    $("#archive-task")?.addEventListener("click", archiveTask);
    $("#delete-task")?.addEventListener("click", deleteTask);
    $("#reopen-task")?.addEventListener("click", (event) => reviewTask("OPEN", event.currentTarget));
    $("#done-task")?.addEventListener("click", (event) => reviewTask("DONE", event.currentTarget));
    $("#add-file-upload")?.addEventListener("click", addFileUpload);
    $("#task-modal")?.addEventListener("click", (event) => { if (event.target.id === "task-modal" && $("#board")) requestCloseForm(); });
    $("#stop-task")?.addEventListener("click", (event) => {
      if (form.reportValidity()) save(event, true);
    });
    form.addEventListener("submit", save);
    setupDrop();
    load().finally(() => {
      if ($("#board") && window.boardLive) window.boardLive.refresh(receiveTasks);
    });
  });
})();
