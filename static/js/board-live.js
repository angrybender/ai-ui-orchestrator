(function () {
  "use strict";

  let source = null;
  let receive = null;
  let disconnected = false;

  function connect() {
    if (source) source.close();
    const connection = new EventSource("/api/board/events");
    source = connection;
    connection.addEventListener("board", (event) => {
      if (source !== connection) return;
      try {
        receive(JSON.parse(event.data).tasks);
        disconnected = false;
      } catch (error) {
        window.showToast("Unable to update Board", { type: "error" });
      }
    });
    connection.onerror = () => {
      if (source !== connection) return;
      if (!disconnected) {
        window.showToast("Board connection lost. Reconnecting…", { type: "error" });
        disconnected = true;
      }
    };
  }

  window.boardLive = {
    refresh(callback) {
      receive = callback;
      connect();
    }
  };
  window.addEventListener("pagehide", () => { if (source) source.close(); });
  window.addEventListener("pageshow", (event) => { if (event.persisted && receive) connect(); });
})();
