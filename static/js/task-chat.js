(function () {
  "use strict";
  let current = null;
  let timer = null;
  let controller = null;
  let generation = 0;
  let lastContent = "";
  let lastAgentContent = null;
  let failed = false;
  let pendingData = null;
  let panel, messages, composer, input, button;

  function spinnerActive() {
    return document.querySelector("#task-form")?.dataset.spinnerActive === "true";
  }

  function applyPending() {
    if (pendingData && !spinnerActive() && current) {
      const data = pendingData;
      pendingData = null;
      render(data);
    }
  }

  function setup() {
    if (panel) return;
    const form = document.querySelector("#task-form");
    if (!form) return;
    panel = document.createElement("aside");
    panel.id = "task-chat";
    panel.className = "task-chat";
    panel.setAttribute("aria-label", "Task comments");
    panel.hidden = true;
    panel.innerHTML = '<div id="chat-messages" class="chat-messages" role="log" aria-label="Conversation" aria-live="polite"></div><div id="chat-composer" class="chat-composer"><label for="chat-comment">User comment</label><textarea id="chat-comment" placeholder="user comment" maxlength="32000"></textarea><p class="field-error" id="chat-error" role="alert"></p><div class="form-actions"><button class="button" id="send-comment" type="button">Send comment</button></div></div>';
    form.append(panel);
    messages = panel.querySelector("#chat-messages");
    composer = panel.querySelector("#chat-composer");
    input = panel.querySelector("#chat-comment");
    button = panel.querySelector("#send-comment");
    button.addEventListener("click", send);
  }

  function render(data) {
    if (spinnerActive()) {
      pendingData = data;
      return;
    }
    current.status = data.status;
    const content = JSON.stringify(data.messages);
    const agentContent = JSON.stringify(data.messages.filter(message => message.role === "agent"));
    const agentUpdated = lastAgentContent !== null && agentContent !== lastAgentContent && data.messages.some(message => message.role === "agent");
    lastAgentContent = agentContent;
    if (content !== lastContent) {
      const scrollTop = messages.scrollTop;
      messages.replaceChildren();
      data.messages.forEach((message) => {
        const node = document.createElement("article");
        node.className = `chat-message ${message.role === "user" ? "chat-user" : "chat-agent"}`;
        const body = document.createElement("div");
        body.className = "chat-message-body";
        body.innerHTML = window.marked?.parse(message.text || "", { breaks: true }) || "";
        const actions = document.createElement("div");
        actions.className = "chat-message-actions";
        const copy = document.createElement("button");
        copy.type = "button";
        copy.className = "chat-copy-button";
        copy.title = "Copy Markdown";
        copy.setAttribute("aria-label", "Copy message as Markdown");
        copy.textContent = "⧉";
        copy.addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(message.text || "");
            window.showToast("Message copied");
          } catch (error) {
            window.showToast("Unable to copy message", { type: "error" });
          }
        });
        const time = document.createElement("time");
        time.className = "chat-message-time";
        time.textContent = message.updated_at || "";
        actions.append(copy, time);
        node.append(body, actions);
        messages.append(node);
      });
      lastContent = content;
      messages.scrollTop = scrollTop;
    }
    composer.hidden = !["REVIEW", "WAIT"].includes(data.status);
    panel.hidden = !data.messages.length && composer.hidden;
    document.querySelector(".task-dialog").classList.toggle("has-chat", !panel.hidden);
    document.querySelector(".task-page-editor")?.classList.toggle("has-chat", !panel.hidden);
    current.onStatus(data.status);
    if (agentUpdated) messages.lastElementChild?.scrollIntoView({ block: "end", inline: "nearest" });
  }

  async function readResponse(response) {
    let data;
    try { data = await response.json(); } catch (error) {
      if (!response.ok) { window.showToast("Unable to update comments", { type: "error" }); return null; }
      throw error;
    }
    if (!response.ok) {
      const detail = data && data.detail;
      if (detail && typeof detail === "object") {
        const message = detail.comment || detail.comments;
        if (message) panel.querySelector("#chat-error").textContent = String(message);
      }
      window.showToast(typeof detail === "string" ? detail : "Unable to update comments", { type: "error" });
      return null;
    }
    return data;
  }

  async function poll(token) {
    if (!current || token !== generation) return;
    controller = new AbortController();
    try {
      const response = await fetch(`/api/board/tasks/${encodeURIComponent(current.id)}/chat`, { signal: controller.signal });
      if (token !== generation) return;
      if (!response.ok && failed) return;
      const data = await readResponse(response);
      if (token !== generation) return;
      failed = !data;
      if (data) render(data);
    } catch (error) {
      if (token === generation && error.name !== "AbortError" && !failed) {
        failed = true;
        window.showToast("Unable to load comments; retrying", { type: "error" });
      }
    } finally {
      if (token === generation && current) timer = setTimeout(() => poll(token), 500);
    }
  }

  async function send() {
    const form = document.querySelector("#task-form");
    if (!current || composer.hidden || button.disabled || form.dataset.spinnerActive === "true") return;
    const comment = input.value.trim();
    if (!comment) {
      panel.querySelector("#chat-error").textContent = "Enter a comment";
      input.focus();
      return;
    }
    panel.querySelector("#chat-error").textContent = "";
    if (window.spinner && !window.spinner.start(button)) return;
    clearTimeout(timer);
    controller?.abort();
    const token = ++generation;
    try {
      const response = await fetch(`/api/board/tasks/${encodeURIComponent(current.id)}/chat`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ comment })
      });
      const data = await readResponse(response);
      if (data && token === generation) {
        input.value = "";
        render(data);
        current.onSent();
        window.showToast("Comment sent");
      }
    } catch (error) {
      window.showToast("Unable to send comment", { type: "error" });
    } finally {
      if (window.spinner) window.spinner.stop(button);
      applyPending();
      if (current && token === generation) poll(token);
    }
  }

  function close() {
    generation++;
    clearTimeout(timer);
    controller?.abort();
    current = null;
    if (panel) panel.hidden = true;
    document.querySelector(".task-dialog")?.classList.remove("has-chat");
    document.querySelector(".task-page-editor")?.classList.remove("has-chat");
  }

  window.taskChat = {
    open(task, onStatus, onSent) {
      close();
      setup();
      if (!task || !panel) return;
      current = { id: task.task_id, status: task.status, onStatus, onSent };
      input.value = "";
      panel.querySelector("#chat-error").textContent = "";
      messages.replaceChildren();
      lastContent = "";
      failed = false;
      render({ status: task.status, messages: [] });
      lastAgentContent = null;
      poll(generation);
    },
    close
  };
  window.addEventListener("pagehide", () => { clearTimeout(timer); controller?.abort(); generation++; });
  window.addEventListener("pageshow", (event) => { if (event.persisted && current) poll(generation); });
})();
