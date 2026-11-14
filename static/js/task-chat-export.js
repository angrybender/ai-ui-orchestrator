(function () {
  "use strict";

  const styles = `
    * { box-sizing:border-box; }
    body { margin:0; padding:24px; color:#282735; background:#f7f7fa; font:14px/1.5 system-ui,sans-serif; }
    main { max-width:960px; margin:auto; }
    h1 { margin:0 0 24px; font-size:22px; overflow-wrap:anywhere; }
    .conversation { display:flex; flex-direction:column; gap:20px; }
    article { width:fit-content; max-width:90%; padding:12px 16px; border-radius:7px; background:#f2f2f5; overflow-wrap:anywhere; }
    .user { align-self:flex-end; background:#e7efff; }
    .message-meta { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:8px; color:#555364; font-size:12px; }
    .message-meta time { margin-left:auto; }
    .message-body > :first-child { margin-top:0; }
    .message-body > :last-child { margin-bottom:0; }
    .plain-text { white-space:pre-wrap; }
    pre { max-width:100%; overflow:auto; padding:10px; border-radius:5px; background:#e7e7eb; }
    code { font-family:ui-monospace,monospace; }
    a { color:#5343bd; }
    table { display:block; max-width:100%; overflow-x:auto; border-collapse:collapse; margin:10px 0; }
    th, td { border:1px solid #cccbd7; padding:7px 10px; vertical-align:top; min-width:80px; }
    th:not([align]), td:not([align]) { text-align:left; }
    th { background:#e7e7ef; }
    tbody tr:nth-child(even) { background:rgba(255,255,255,.45); }
    @media (max-width:600px) { body { padding:16px; } article { max-width:100%; min-width:0; } }
    @media print { body { padding:0; background:white; } article { break-inside:avoid; } pre { white-space:pre-wrap; } }
  `;

  function createDocument(task) {
    const title = `${task.id} — ${task.title}`;
    const output = document.implementation.createHTMLDocument(title);
    output.documentElement.lang = document.documentElement.lang || "en";
    const charset = output.createElement("meta");
    charset.setAttribute("charset", "utf-8");
    output.head.prepend(charset);
    const viewport = output.createElement("meta");
    viewport.name = "viewport";
    viewport.content = "width=device-width, initial-scale=1";
    const policy = output.createElement("meta");
    policy.httpEquiv = "Content-Security-Policy";
    policy.content = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'";
    const style = output.createElement("style");
    style.textContent = styles;
    output.head.append(viewport, policy, style);
    const main = output.createElement("main");
    const heading = output.createElement("h1");
    heading.textContent = title;
    const conversation = output.createElement("div");
    conversation.className = "conversation";
    task.messages.forEach((message) => {
      const article = output.createElement("article");
      article.className = message.role === "user" ? "user" : "agent";
      const meta = output.createElement("div");
      meta.className = "message-meta";
      const role = output.createElement("strong");
      role.textContent = { agent: "Agent", user: "User", system: "System" }[message.role] || "Message";
      const time = output.createElement("time");
      time.textContent = message.updated_at || message.created_at || "";
      meta.append(role, time);
      const body = output.createElement("div");
      body.className = "message-body";
      const rendered = message.role !== "system" && window.safeMarkdown ? window.safeMarkdown.render(message.text || "") : null;
      if (rendered) {
        // Keep the file independent of image servers and preserve image sources as links.
        rendered.querySelectorAll("img").forEach((image) => {
          const link = output.createElement("a");
          link.textContent = image.alt || "Image";
          if (image.hasAttribute("src")) link.setAttribute("href", image.getAttribute("src"));
          image.replaceWith(link);
        });
        rendered.querySelectorAll("a[href]").forEach((link) => {
          link.setAttribute("href", new URL(link.getAttribute("href"), document.baseURI).href);
          link.setAttribute("rel", "noopener noreferrer");
        });
        body.append(rendered);
      } else {
        body.classList.add("plain-text");
        body.textContent = message.text || "";
      }
      article.append(meta, body);
      conversation.append(article);
    });
    main.append(heading, conversation);
    output.body.append(main);
    return "<!doctype html>\n" + output.documentElement.outerHTML;
  }

  window.taskChatExport = {
    download(task) {
      const blob = new Blob([createDocument(task)], { type: "text/html;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${task.id.replace(/[^a-zA-Z0-9_-]/g, "_") || "task"}-chat.html`;
      try {
        document.body.append(link);
        link.click();
      } finally {
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    }
  };
})();
