(() => {
  const search = document.getElementById('catalog-search');
  const statusFilter = document.getElementById('status-filter');
  const collectionFilter = document.getElementById('collection-filter');
  const selectedOnly = document.getElementById('selected-only');
  const selectVisible = document.getElementById('select-visible');
  const clearSelection = document.getElementById('clear-selection');
  const selectionCount = document.getElementById('selection-count');
  const selectionDuration = document.getElementById('selection-duration');
  const selectionBar = document.getElementById('selection-bar');
  const submitSelection = document.getElementById('analyse-selection');
  if (!search || !statusFilter || !collectionFilter || !selectedOnly || !selectVisible
      || !clearSelection || !selectionCount || !selectionDuration || !selectionBar) return;
  const rows = [...document.querySelectorAll('[data-video-row]')];
  const selects = [...document.querySelectorAll('[data-video-select]')];

  const normalize = value => String(value || '').trim().toLocaleLowerCase('it');
  const duration = value => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
  const formatDuration = seconds => {
    const total = Math.round(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remaining = total % 60;
    return hours
      ? `${hours}:${String(minutes).padStart(2, '0')}:${String(remaining).padStart(2, '0')}`
      : `${String(minutes).padStart(2, '0')}:${String(remaining).padStart(2, '0')}`;
  };
  const rowMatches = (row, isSelected) => {
    const query = normalize(search.value);
    const searchable = `${row.dataset.title || ''} ${row.dataset.description || ''}`;
    return (!query || normalize(searchable).includes(query))
      && (statusFilter.value === 'all' || row.dataset.status === statusFilter.value)
      && (collectionFilter.value === 'all' || row.dataset.collection === collectionFilter.value)
      && (!selectedOnly.checked || isSelected);
  };
  const selectedDuration = () => selects.reduce(
    (total, select, index) => total + (select.checked ? duration(rows[index]?.dataset.duration) : 0), 0,
  );
  const update = () => {
    rows.forEach((row, index) => {
      row.hidden = !rowMatches(row, selects[index]?.checked);
    });
    const count = selects.filter(select => select.checked).length;
    selectionCount.textContent = String(count);
    selectionDuration.textContent = formatDuration(selectedDuration());
    selectionBar.hidden = count === 0;
    if (submitSelection) submitSelection.disabled = count === 0;
  };

  search.addEventListener('input', update);
  statusFilter.addEventListener('change', update);
  collectionFilter.addEventListener('change', update);
  selectedOnly.addEventListener('change', update);
  selects.forEach(select => select.addEventListener('change', update));
  selectVisible.addEventListener('click', () => {
    rows.forEach((row, index) => {
      if (!row.hidden && selects[index]) selects[index].checked = true;
    });
    update();
  });
  clearSelection.addEventListener('click', () => {
    selects.forEach(select => { select.checked = false; });
    update();
  });

  update();
})();
