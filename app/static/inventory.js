(() => {
  const inventory = document.getElementById('inventory');
  if (inventory) {
    const rows = [...document.querySelectorAll('[data-course-row]')];
    const buttons = [...document.querySelectorAll('[data-inventory-filter]')];
    const search = document.getElementById('inventory-search');
    const status = document.getElementById('inventory-status');
    let filter = 'all';
    const matchesState = state => filter === 'all' ||
      (filter === 'review' && ['proposta', 'da_verificare', 'non_abbinato'].includes(state)) ||
      (filter === 'ready' && state === 'pronto_academy');
    const update = () => {
      const query = (search?.value || '').trim().toLocaleLowerCase('it');
      let visible = 0;
      rows.forEach(row => {
        const match = matchesState(row.dataset.state) &&
          (!query || (row.dataset.search || '').toLocaleLowerCase('it').includes(query));
        row.hidden = !match;
        if (match) visible++;
      });
      if (status) status.textContent = `${visible} corsi visibili`;
    };
    buttons.forEach(button => button.addEventListener('click', () => {
      filter = button.dataset.inventoryFilter;
      buttons.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
      update();
    }));
    search?.addEventListener('input', update);
  }

  const courseStatus = document.getElementById('course-status');
  if (courseStatus && courseStatus.dataset.state === 'in_elaborazione') {
    const poll = async () => {
      try {
        const response = await fetch(`/api/courses/${encodeURIComponent(courseStatus.dataset.courseId)}`,
          {headers: {'Accept': 'application/json'}});
        if (!response.ok) throw new Error('status');
        const data = await response.json();
        data.jobs.forEach(job => {
          const progress = document.querySelector(`[data-job-progress="${job.id}"]`);
          const message = document.querySelector(`[data-job-message="${job.id}"]`);
          if (progress) progress.value = job.progress;
          if (message) message.textContent = job.message;
        });
        if (data.intermedio_disponibile || ['fallito', 'annullato', 'da_rianalizzare'].includes(data.stato)) {
          window.location.reload();
          return;
        }
      } catch (_) {
        // A transient polling failure leaves the persisted page usable.
      }
      setTimeout(poll, 3000);
    };
    setTimeout(poll, 3000);
  }
})();
