(function () {
  "use strict";

  function escapeHtml(text) {
    const entities = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
    return text.replace(/[&<>"']/g, (character) => entities[character]);
  }

  window.safeMarkdown = {
    render(text) {
      if (typeof window.marked?.parse !== "function" || !window.marked.Renderer) return null;
      const renderer = new window.marked.Renderer();
      // Escape HTML tokens only: escaping all input would corrupt Markdown code blocks.
      renderer.html = (token) => escapeHtml(token.text);
      const template = document.createElement("template");
      template.innerHTML = window.marked.parse(text, { breaks: true, renderer });
      // Markdown can also produce URLs with executable schemes. Check the decoded
      // DOM attributes before attaching the fragment to the document.
      template.content.querySelectorAll("a[href], img[src]").forEach((element) => {
        const attribute = element.tagName === "A" ? "href" : "src";
        const protocols = attribute === "href" ? ["http:", "https:", "mailto:"] : ["http:", "https:"];
        try {
          const url = new URL(element.getAttribute(attribute), "https://markdown.invalid/");
          if (!protocols.includes(url.protocol)) element.removeAttribute(attribute);
        } catch (_) {
          element.removeAttribute(attribute);
        }
      });
      return template.content;
    }
  };
})();
