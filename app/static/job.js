(() => {
  "use strict";
  const job = document.getElementById("job");
  if (!job) return;
  const terminal = new Set(["completed", "failed", "cancelled"]);
  const report = document.getElementById("report-text");
  const pollError = document.getElementById("poll-error");
  let timer;
  let stopped = false;

  function render(data) {
    job.dataset.state = data.state;
    document.getElementById("job-message").textContent = data.message;
    document.getElementById("job-progress").value = data.progress;
    document.getElementById("progress-label").textContent = `${data.progress}%`;
    const error = document.getElementById("job-error");
    error.textContent = data.error || "";
    error.hidden = !data.error;
    document.getElementById("cancel-form").hidden = terminal.has(data.state);
    if (data.state === "completed" && data.report_text) {
      report.textContent = data.report_text;
      document.getElementById("report-section").hidden = false;
    }
  }

  async function poll() {
    if (stopped) return;
    try {
      const response = await fetch(`/api/jobs/${job.dataset.jobId}`, {cache: "no-store"});
      if (response.status === 401 || response.status === 404) {
        pollError.textContent = response.status === 401
          ? "Accesso scaduto. Ricarica la pagina per autenticarti."
          : "Lavoro non disponibile. Il processo del server potrebbe essere stato riavviato.";
        pollError.hidden = false;
        stopped = true;
        return;
      }
      if (!response.ok) throw new Error("poll");
      render(await response.json());
      pollError.hidden = true;
    } catch {
      pollError.textContent = "Connessione interrotta. Nuovo tentativo tra tre secondi.";
      pollError.hidden = false;
    }
    if (!stopped && !terminal.has(job.dataset.state)) timer = setTimeout(poll, 3000);
  }

  document.getElementById("copy-report").addEventListener("click", async () => {
    const status = document.getElementById("copy-status");
    try {
      await navigator.clipboard.writeText(report.textContent);
      status.textContent = "Report copiato.";
    } catch {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(report);
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = "Copia automatica non disponibile. Testo selezionato: usa Ctrl+C o ⌘C.";
    }
  });
  document.getElementById("print-report").addEventListener("click", () => window.print());
  window.addEventListener("pagehide", () => { stopped = true; clearTimeout(timer); });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      stopped = false;
      if (!terminal.has(job.dataset.state)) poll();
    }
  });
  if (!terminal.has(job.dataset.state)) timer = setTimeout(poll, 3000);
})();
