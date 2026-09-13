(() => {
  "use strict";
  const batch = document.getElementById("batch");
  const pollStatus = document.getElementById("batch-poll-status");
  if (!batch || !pollStatus) return;
  const terminal = new Set(["completed", "failed", "cancelled"]);
  const rows = [...document.querySelectorAll("[data-batch-job]")];
  const rowsById = new Map(rows.map(row => [row.dataset.jobId, row]));
  let timer;
  let stopped = false;
  let generation = 0;

  function allTerminal() {
    return rows.every(row => terminal.has(row.dataset.jobState));
  }

  function render(jobs) {
    jobs.forEach(data => {
      const row = rowsById.get(data.id);
      if (!row) return;
      row.dataset.jobState = data.state;
      const message = row.querySelector("[data-job-message]");
      const label = row.querySelector("[data-job-label]");
      const progress = row.querySelector("[data-job-progress]");
      if (message) message.textContent = data.message;
      if (label) label.textContent = `${data.progress}%`;
      if (progress) progress.value = data.progress;
    });
  }

  async function poll() {
    if (stopped) return;
    const current = generation;
    try {
      const response = await fetch(`/api/batches/${batch.dataset.batchId}`, {cache: "no-store"});
      if (stopped || current !== generation) return;
      if (response.status === 401 || response.status === 404) {
        pollStatus.textContent = response.status === 401
          ? "Accesso scaduto. Ricarica la pagina per autenticarti."
          : "Gruppo non disponibile.";
        pollStatus.hidden = false;
        stopped = true;
        return;
      }
      if (!response.ok) throw new Error("poll");
      const data = await response.json();
      if (stopped || current !== generation) return;
      render(data.jobs);
      pollStatus.hidden = true;
    } catch {
      if (stopped || current !== generation) return;
      pollStatus.textContent = "Connessione interrotta. Nuovo tentativo tra tre secondi.";
      pollStatus.hidden = false;
    }
    if (!stopped && current === generation && !allTerminal()) timer = setTimeout(poll, 3000);
  }

  window.addEventListener("pagehide", () => {
    generation++;
    stopped = true;
    clearTimeout(timer);
  });
  window.addEventListener("pageshow", event => {
    if (event.persisted) {
      generation++;
      clearTimeout(timer);
      stopped = false;
      if (!allTerminal()) poll();
    }
  });
  if (!allTerminal()) timer = setTimeout(poll, 3000);
})();
