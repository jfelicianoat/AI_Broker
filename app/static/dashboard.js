(() => {
  const processed = new WeakSet();
  const refreshMap = {
    "#summary-panel": "/dashboard/fragments/summary",
    "#queue-panel": "/dashboard/fragments/queue",
    "#active-panel": "/dashboard/fragments/active",
    "#health-panel": "/dashboard/fragments/health",
    "#resources-panel": "/dashboard/fragments/resources",
    "#history-panel": "/dashboard/fragments/history"
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

  function progressId() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return `probe-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function setProbeProgress(panel, progress) {
    if (!panel) return;
    const label = panel.querySelector("[data-probe-progress-label]");
    const detail = panel.querySelector("[data-probe-progress-detail]");
    const bar = panel.querySelector("[data-probe-progress-bar]");
    const completed = Number(progress.completed || 0);
    const total = Number(progress.total || 0);
    const percent = total > 0 ? Math.max(0, Math.min(100, Math.round((completed / total) * 100))) : 8;
    panel.hidden = false;
    panel.dataset.phase = progress.phase || "running";
    if (bar) bar.style.width = `${percent}%`;
    if (label) {
      if (progress.phase === "completed") label.textContent = `Tanda completada: ${completed}/${total || completed}`;
      else if (progress.phase === "failed") label.textContent = "Analisis detenido";
      else if (total > 0) label.textContent = `Progreso de tanda: ${completed}/${total}`;
      else label.textContent = "Preparando catalogo...";
    }
    if (detail) {
      if (progress.error) detail.textContent = progress.error;
      else if (progress.current_model) detail.textContent = `Modelo actual: ${progress.current_model}`;
      else if (progress.last_result && progress.last_result.name) {
        detail.textContent = `Ultimo: ${progress.last_result.name} - ${progress.last_result.compatibility || "sin clasificar"}`;
      } else detail.textContent = "Esperando primera respuesta del proveedor.";
    }
  }

  async function pollProbeProgress(providerId, id, panel, stop) {
    while (!stop.done) {
      try {
        const response = await fetch(`/dashboard/actions/providers/${encodeURIComponent(providerId)}/probe/progress?progress_id=${encodeURIComponent(id)}`, {
          headers: {"Accept": "application/json"}
        });
        if (response.ok) {
          const progress = await response.json();
          setProbeProgress(panel, progress);
          if (progress.phase === "completed" || progress.phase === "failed") return;
        }
      } catch (_) {
        // The form submission result will surface the real failure if polling misses once.
      }
      await new Promise((resolve) => window.setTimeout(resolve, 850));
    }
  }

  function submitterProviderId(submitter) {
    return submitter ? submitter.getAttribute("data-provider-probe") : null;
  }

  async function submitProbeForm(form, submitter) {
    const providerId = submitterProviderId(submitter);
    if (!providerId) return false;
    const id = progressId();
    const escapedProvider = window.CSS && CSS.escape ? CSS.escape(providerId) : providerId.replace(/"/g, '\\"');
    const panel = form.querySelector(`[data-probe-progress="${escapedProvider}"]`);
    const stop = {done: false};
    const data = new FormData(form);
    data.set("probe_progress_id", id);
    submitter.disabled = true;
    submitter.setAttribute("aria-busy", "true");
    setProbeProgress(panel, {phase: "preparing", completed: 0, total: null, current_model: null});
    const poll = pollProbeProgress(providerId, id, panel, stop);
    try {
      const response = await fetch(submitter.getAttribute("formaction") || form.action, {
        method: "POST",
        body: new URLSearchParams(data),
        headers: {
          "Accept": "text/html",
          "X-CSRF-Token": csrfToken()
        }
      });
      stop.done = true;
      await poll;
      if (!response.ok) throw new Error(`Error ${response.status}`);
      const html = await response.text();
      document.open();
      document.write(html);
      document.close();
      return true;
    } catch (error) {
      stop.done = true;
      setProbeProgress(panel, {phase: "failed", error: error.message || "No se pudo analizar compatibilidad"});
      toast(error.message || "No se pudo analizar compatibilidad");
      submitter.disabled = false;
      submitter.removeAttribute("aria-busy");
      return true;
    }
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

  function refreshPaused(element) {
    const panel = element.closest("[data-refresh-pauseable]") || element;
    const active = document.activeElement;
    return Boolean(
      panel.matches("[data-refresh-pauseable]") &&
      (panel.matches(":hover") || (active && panel.contains(active)))
    );
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
          if (refreshPaused(element)) {
            schedule();
            return;
          }
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
    root.querySelectorAll("form.config-form").forEach((form) => {
      if (processed.has(form)) return;
      processed.add(form);
      form.addEventListener("submit", async (event) => {
        if (!submitterProviderId(event.submitter)) return;
        event.preventDefault();
        await submitProbeForm(form, event.submitter);
      });
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
