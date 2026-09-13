(() => {
  const form = document.getElementById('course-review-form');
  const source = document.getElementById('course-report-data');
  const status = document.getElementById('review-status');
  if (!form || !source || !status) return;
  let report;
  try { report = JSON.parse(source.textContent); } catch (_) { return; }
  const parseTime = value => {
    const match = /^(\d+):([0-5]\d):([0-5]\d)$/.exec(value);
    return match ? Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]) : null;
  };
  const collect = () => {
    [...document.querySelectorAll('[data-intervention-row]')].forEach(row => {
      const item = report.video[Number(row.dataset.videoIndex)].interventi[Number(row.dataset.interventionIndex)];
      const value = name => row.querySelector(`[data-field="${name}"]`).value;
      item.inizio = value('inizio').trim();
      item.fine = value('fine').trim();
      item.tipo = value('tipo');
      item.relatori = value('relatori').split(',').map(name => name.trim()).filter(Boolean);
      item.titolo = value('titolo').trim();
      item.sintesi = value('sintesi').trim();
      item.punti_chiave = value('punti_chiave').split('\n').map(point => point.trim()).filter(Boolean);
      item.confidenza = Number(value('confidenza'));
    });
  };
  const validate = () => {
    for (const video of report.video) {
      let cursor = 0;
      for (const item of video.interventi) {
        const start = parseTime(item.inizio), end = parseTime(item.fine);
        if (start === null || end === null || start !== cursor || end <= start) {
          return 'Gli interventi devono usare h:mm:ss e coprire il video senza buchi o sovrapposizioni.';
        }
        cursor = end;
      }
      if (cursor !== video.durata_secondi) return 'Gli interventi devono coprire tutta la durata del video.';
    }
    return null;
  };
  form.addEventListener('submit', async event => {
    event.preventDefault();
    collect();
    const error = validate();
    if (error) { status.textContent = error; status.className = 'form-error'; return; }
    status.textContent = 'Salvataggio in corso…'; status.className = '';
    try {
      const response = await fetch(`/api/courses/${encodeURIComponent(form.dataset.courseId)}/report`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': form.dataset.csrfToken},
        body: JSON.stringify(report),
      });
      if (!response.ok) throw new Error('save');
      await response.json();
      status.textContent = 'Correzioni salvate.';
    } catch (_) {
      status.textContent = 'Impossibile salvare: controlla tempi e campi.';
      status.className = 'form-error';
    }
  });
})();
