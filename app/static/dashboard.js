(() => {
  const processed = new WeakSet();
  const refreshMap = {
    "#summary-panel": "/dashboard/fragments/summary",
    "#queue-panel": "/dashboard/fragments/queue",
    "#active-panel": "/dashboard/fragments/active",
    "#health-panel": "/dashboard/fragments/health",
    "#resources-panel": "/dashboard/fragments/resources"
  };

  function toast(message) {
    const node = document.querySelector("#toast");
    if (!node) return;
    node.textContent = message;
    node.classList.add("visible");
    window.setTimeout(() => node.classList.remove("visible"), 2600);
  }

  function csrfToken() {
    const meta = document.querySelector("meta[name='csrf-token']");
    return meta ? meta.getAttribute("content") : "";
  }

  async function requestAndSwap(element, method, url) {
    const confirmMessage = element.getAttribute("hx-confirm");
    if (confirmMessage && !window.confirm(confirmMessage)) return;
    element.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(url, {
        method,
        headers: {
          "HX-Request": "true",
          "Accept": "text/html",
          "X-CSRF-Token": csrfToken()
        }
      });
      if (!response.ok) throw new Error(`Error ${response.status}`);
      const targetSelector = element.getAttribute("hx-target");
      const target = targetSelector ? document.querySelector(targetSelector) : element;
      const swap = element.getAttribute("hx-swap") || "innerHTML";
      if (response.status !== 204 && target) {
        const html = await response.text();
        if (swap === "outerHTML") target.outerHTML = html;
        else target.innerHTML = html;
        bind(document);
      }
      if (method !== "GET") {
        await refreshDashboard();
        toast("Panel actualizado");
      }
    } catch (error) {
      toast(error.message || "No se pudo actualizar el panel");
    } finally {
      element.removeAttribute("aria-busy");
    }
  }

  function intervalFrom(trigger) {
    const match = /every\s+(\d+)s/.exec(trigger || "");
    return match ? Number(match[1]) * 1000 : null;
  }

  function bind(root) {
    root.querySelectorAll("[hx-get], [hx-post], [hx-delete]").forEach((element) => {
      if (processed.has(element)) return;
      processed.add(element);
      const get = element.getAttribute("hx-get");
      const post = element.getAttribute("hx-post");
      const remove = element.getAttribute("hx-delete");
      const method = post ? "POST" : remove ? "DELETE" : "GET";
      const url = post || remove || get;
      const trigger = element.getAttribute("hx-trigger") || "click";
      const interval = intervalFrom(trigger);
      if (interval) {
        const schedule = () => window.setTimeout(async () => {
          if (!document.contains(element)) return;
          await requestAndSwap(element, method, url);
          schedule();
        }, interval);
        schedule();
      }
      if (!interval || trigger.includes("click")) {
        element.addEventListener("click", (event) => {
          if (element.tagName === "A" && method === "GET") return;
          event.preventDefault();
          requestAndSwap(element, method, url);
        });
      }
    });
    root.querySelectorAll("[data-refresh-target]").forEach((button) => {
      if (processed.has(button)) return;
      processed.add(button);
      button.addEventListener("click", () => refresh(button.dataset.refreshTarget));
    });
  }

  async function refresh(selector) {
    const url = refreshMap[selector];
    const target = document.querySelector(selector);
    if (!url || !target) return;
    try {
      const response = await fetch(url, {headers: {"HX-Request": "true"}});
      if (!response.ok) throw new Error(`Error ${response.status}`);
      target.outerHTML = await response.text();
      bind(document);
    } catch (error) {
      toast(error.message || "No se pudo actualizar");
    }
  }

  async function refreshDashboard() {
    await Promise.all(Object.keys(refreshMap).map(refresh));
  }

  document.addEventListener("DOMContentLoaded", () => {
    bind(document);
    document.querySelectorAll("[data-refresh-dashboard]").forEach((button) => {
      button.addEventListener("click", refreshDashboard);
    });
  });
})();
