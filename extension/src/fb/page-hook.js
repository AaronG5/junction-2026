// Runs in Facebook's own page context (manifest: "world": "MAIN"), before Facebook's code.
// It passes a copy of every GraphQL answer that contains posts to src/fb/content.js. It reads
// answers only: it never changes, blocks or sends any request.

(() => {
  const SOURCE = "ppg-fb";
  const isGraphql = (url) => typeof url === "string" && url.includes("/api/graphql");
  const forward = (body) => {
    if (typeof body === "string" && body.includes('"Story"')) window.postMessage({ source: SOURCE, body }, location.origin);
  };

  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__fbPostSender = isGraphql(String(url));
    return open.call(this, method, url, ...rest);
  };
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...args) {
    if (this.__fbPostSender) {
      this.addEventListener("load", () => {
        try {
          if (this.responseType === "" || this.responseType === "text") forward(this.responseText);
        } catch { /* never disturb Facebook */ }
      });
    }
    return send.apply(this, args);
  };

  const originalFetch = window.fetch;
  window.fetch = function (input, init) {
    const answer = originalFetch.apply(this, arguments);
    try {
      const url = typeof input === "string" ? input : input && input.url;
      if (isGraphql(url)) answer.then((res) => res.clone().text()).then(forward).catch(() => {});
    } catch { /* never disturb Facebook */ }
    return answer;
  };
})();
