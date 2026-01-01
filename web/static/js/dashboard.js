// web/static/js/dashboard.js
let loadGenres;
// ─── Constants ───────────────────────────────────────────────
const SORT_ICONS = {
asc: `
<path d="M11 16H17"/>
<path d="M11 20H19"/>
<path d="M11 12H15"/>
<path d="M4 8L7 5L10 8"/>
<path d="M7 20L7 6"/>
`,
desc: `
<path d="M11 9H17"/>
<path d="M11 5H19"/>
<path d="M11 13H15"/>
<path d="M10 17L7 20L4 17"/>
<path d="M7 5V19"/>
`
};

// ─── Volume-aware sorting helpers ─────────────────────────────
function getVolumeKey(vol) {
	if (vol == null || vol === '') return [0, 0];
	vol = String(vol).trim();
	const num = Number(vol);
	if (!isNaN(num)) {
		return [1, num];
	}
	return [2, vol];
}
function compareChapters(a, b) {
	const volA = getVolumeKey(a.volume);
	const volB = getVolumeKey(b.volume);
	if (volA[0] !== volB[0]) return volA[0] - volB[0];
	if (volA[0] === 1) {
		if (volA[1] !== volB[1]) return volA[1] - volB[1];
	} else {
		if (volA[1] !== volB[1]) return volA[1].localeCompare(volB[1]);
	}
	return a.chapter_number - b.chapter_number;
}

// ─── In-Memory State (no URL params) ───────────────────────────
const state = {
	page: 1,
	status: 'reading',
	sort: 'unread_first',
	dir: 'asc',
	type: [],
	genre: [],
	rating: [],
	pubStatus: [],
	readableOn: []  // NEW: Filter for source types (mangadex, kagane)
};

// ─── Bulk Selection State ────────────────────────────────────────
const bulkState = {
	selectedIds: new Set(),
	isBulkMode: false
};

function enterBulkMode() {
	bulkState.isBulkMode = true;
	document.querySelectorAll('.series-card').forEach(card => {
		card.classList.add('bulk-mode');
	});
	document.getElementById('default-actions').style.display = 'none';
	document.getElementById('bulk-actions').style.display = 'flex';
}
function exitBulkMode() {
	bulkState.isBulkMode = false;
	bulkState.selectedIds.clear();
	document.querySelectorAll('.series-card').forEach(card => {
		card.classList.remove('bulk-mode', 'selected');
	});
	document.getElementById('default-actions').style.display = 'flex';
	document.getElementById('bulk-actions').style.display = 'none';
}
function toggleCardSelection(seriesId) {
	const card = document.querySelector(`.series-card[data-series-id="${seriesId}"]`);
	if (!card) return;
	if (bulkState.selectedIds.has(seriesId)) {
		bulkState.selectedIds.delete(seriesId);
		card.classList.remove('selected');
	} else {
		bulkState.selectedIds.add(seriesId);
		card.classList.add('selected');
	}
	if (bulkState.selectedIds.size === 0) {
		exitBulkMode();
	} else if (!bulkState.isBulkMode) {
		enterBulkMode();
	}
}

// ─── Core Functions ──────────────────────────────────────────
function saveChapter(seriesId, chapter) {
	return fetch(`/api/series/${seriesId}`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ current_chapter: chapter })
	});
}

let currentSeriesIdForEdit = null;

function openEditModal(series) {
	currentSeriesIdForEdit = series.id;
	document.getElementById('edit-series-id').value = series.id;
	document.getElementById('edit-title').value = series.title || '';
	document.getElementById('edit-cover-url').value = series.cover_url || '';
	document.getElementById('edit-status').value = series.status || 'plan_to_read';
	// Load sources
	loadSeriesSources(series.id);

	// Load chapters (existing code)
	fetch(`/api/series/${series.id}/chapters`)
		.then(r => r.json())
		.then(chapters => {
			const select = document.getElementById('edit-current-chapter');
			select.innerHTML = '<option value="-1">Not started</option>';
			const hasAnyNullVolume = chapters.some(ch => ch.volume == null || ch.volume === '');
			const useVolumeLabels = !hasAnyNullVolume;
			const comparator = useVolumeLabels
				? (a, b) => {
					const volA = getVolumeKey(a.volume);
					const volB = getVolumeKey(b.volume);
					if (volA[0] !== volB[0]) return volA[0] - volB[0];
					if (volA[0] === 1) {
						if (volA[1] !== volB[1]) return volA[1] - volB[1];
					} else {
						if (volA[1] !== volB[1]) return volA[1].localeCompare(volB[1]);
					}
					return a.chapter_number - b.chapter_number;
				}
				: (a, b) => a.chapter_number - b.chapter_number;
			const sortedChapters = [...chapters].sort(comparator);
			const numeric = sortedChapters.filter(ch => !ch.is_oneshot).reverse();
			const oneshots = sortedChapters.filter(ch => ch.is_oneshot);
			function formatLabel(ch) {
				if (ch.is_oneshot) {
					return oneshots.length === 1 ? "Oneshot" : `Oneshot ${oneshots.indexOf(ch) + 1}`;
				}
				if (useVolumeLabels && ch.volume) {
					return `Vol.${ch.volume} Ch.${ch.chapter_number}`;
				}
				return `Ch.${ch.chapter_number}`;
			}
			numeric.forEach(ch => {
				const opt = document.createElement('option');
				opt.value = ch.chapter_number;
				opt.textContent = formatLabel(ch);
				if (ch.chapter_number === parseFloat(series.current_chapter)) {
					opt.selected = true;
				}
				select.appendChild(opt);
			});
			oneshots.forEach(ch => {
				const opt = document.createElement('option');
				opt.value = ch.chapter_number;
				opt.textContent = formatLabel(ch);
				if (ch.chapter_number === parseFloat(series.current_chapter)) {
					opt.selected = true;
				}
				select.appendChild(opt);
			});
		})
		.catch(err => console.error('Chapter load error:', err));

	document.getElementById('edit-series-modal').classList.remove('hidden');
}

// ─── Source Management Functions ─────────────────────────────
let pendingSourceChanges = {
	hasChanges: false,
	primarySourceId: null,
	originalPrimaryId: null
};

async function loadSeriesSources(seriesId) {
	try {
		const res = await fetch(`/api/series/${seriesId}/sources`);
		if (!res.ok) throw new Error('Failed to load sources');

		const data = await res.json();
		const container = document.getElementById('sources-list');

		if (data.sources.length === 0) {
			container.innerHTML = '<p class="loading-text">No sources found.</p>';
			return;
		}

		// Store original primary source
		const primarySource = data.sources.find(s => s.is_primary);
		pendingSourceChanges.originalPrimaryId = primarySource ? primarySource.id : null;
		pendingSourceChanges.primarySourceId = pendingSourceChanges.originalPrimaryId;
		pendingSourceChanges.hasChanges = false;

		renderSources(data.sources);
		initializeDragAndDrop();

	} catch (err) {
		console.error('Failed to load sources:', err);
		document.getElementById('sources-list').innerHTML =
			'<p class="loading-text">Error loading sources.</p>';
	}
}

function renderSources(sources) {
	const container = document.getElementById('sources-list');

	// Sort so primary is always first
	const sortedSources = [...sources].sort((a, b) => {
		if (a.id === pendingSourceChanges.primarySourceId) return -1;
		if (b.id === pendingSourceChanges.primarySourceId) return 1;
		return 0;
	});

	const helpText = sources.length > 1
		? '<div class="sources-help-text">💡 Drag sources to reorder. The top source is the primary source used for "Go to Source" button.</div>'
		: '';

	container.innerHTML = helpText + sortedSources.map(source => {
		const sourceTypeLabel = {
			'mangadex': 'MangaDex',
			'kagane': 'Kagane',
			'unknown': 'Unknown'
		}[source.source_type] || source.source_type;

		const isPrimary = source.id === pendingSourceChanges.primarySourceId;

		return `
      <div class="source-item ${isPrimary ? 'primary' : ''}" data-source-id="${source.id}" draggable="true">
        <div class="source-drag-handle">
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
            <circle cx="9" cy="5" r="1.5"/>
            <circle cx="9" cy="12" r="1.5"/>
            <circle cx="9" cy="19" r="1.5"/>
            <circle cx="15" cy="5" r="1.5"/>
            <circle cx="15" cy="12" r="1.5"/>
            <circle cx="15" cy="19" r="1.5"/>
          </svg>
        </div>
        <div class="source-info">
          <div class="source-type">
            ${sourceTypeLabel}
            ${isPrimary ? '<span class="primary-badge">PRIMARY</span>' : ''}
          </div>
          <div class="source-url">${source.source_url}</div>
        </div>
        <div class="source-actions">
          <button class="btn-icon" onclick="window.open('${source.source_url}', '_blank')" title="Open Source">
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M15 3h6v6M10 14L21 3M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/>
            </svg>
          </button>
          ${!isPrimary && sortedSources.length > 1 ? `
            <button class="btn-icon danger" onclick="removeSource(${currentSeriesIdForEdit}, ${source.id})" title="Remove Source">
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
              </svg>
            </button>
          ` : ''}
        </div>
      </div>
    `;
	}).join('');
}

function initializeDragAndDrop() {
	const container = document.getElementById('sources-list');
	if (!container) return;

	let draggedElement = null;

	container.addEventListener('dragstart', (e) => {
		if (e.target.classList.contains('source-item')) {
			draggedElement = e.target;
			e.target.classList.add('dragging');
		}
	});

	container.addEventListener('dragend', (e) => {
		if (e.target.classList.contains('source-item')) {
			e.target.classList.remove('dragging');
			draggedElement = null;
		}
	});

	container.addEventListener('dragover', (e) => {
		e.preventDefault();
		const afterElement = getDragAfterElement(container, e.clientY);
		const dragging = document.querySelector('.dragging');

		if (afterElement == null) {
			container.appendChild(dragging);
		} else {
			container.insertBefore(dragging, afterElement);
		}
	});

	container.addEventListener('drop', (e) => {
		e.preventDefault();

		// Get the new order and mark the first as primary
		const sourceItems = container.querySelectorAll('.source-item:not(.sources-help-text)');
		if (sourceItems.length > 0) {
			const newPrimaryId = parseInt(sourceItems[0].dataset.sourceId);

			if (newPrimaryId !== pendingSourceChanges.primarySourceId) {
				pendingSourceChanges.primarySourceId = newPrimaryId;
				pendingSourceChanges.hasChanges = true;

				// Re-render to update visual state
				const currentSources = Array.from(sourceItems).map(item => ({
					id: parseInt(item.dataset.sourceId),
					source_type: item.querySelector('.source-type').textContent.trim().replace(/\s*\(.*?\)/, '').toLowerCase(),
					source_url: item.querySelector('.source-url').textContent,
					is_primary: parseInt(item.dataset.sourceId) === newPrimaryId
				}));

				renderSources(currentSources);
				initializeDragAndDrop(); // Re-initialize after render
			}
		}
	});
}

function getDragAfterElement(container, y) {
	const draggableElements = [...container.querySelectorAll('.source-item:not(.dragging)')];

	return draggableElements.reduce((closest, child) => {
		const box = child.getBoundingClientRect();
		const offset = y - box.top - box.height / 2;

		if (offset < 0 && offset > closest.offset) {
			return { offset: offset, element: child };
		} else {
			return closest;
		}
	}, { offset: Number.NEGATIVE_INFINITY }).element;
}

async function saveSourceChanges(seriesId) {
	if (!pendingSourceChanges.hasChanges) return;

	try {
		const res = await fetch(`/api/series/${seriesId}/sources/${pendingSourceChanges.primarySourceId}/primary`, {
			method: 'POST'
		});

		if (res.ok) {
			pendingSourceChanges.hasChanges = false;
			pendingSourceChanges.originalPrimaryId = pendingSourceChanges.primarySourceId;
		} else {
			throw new Error('Failed to save primary source');
		}
	} catch (err) {
		console.error('Failed to save source changes:', err);
		throw err;
	}
}

async function removeSource(seriesId, sourceId) {
	if (!confirm('Remove this source? Chapters from this source will remain but won\'t be updated.')) {
		return;
	}

	try {
		const res = await fetch(`/api/series/${seriesId}/sources/${sourceId}`, {
			method: 'DELETE'
		});

		if (res.ok) {
			await loadSeriesSources(seriesId);
		} else {
			const data = await res.json();
			alert('Failed to remove source: ' + (data.error || 'Unknown error'));
		}
	} catch (err) {
		console.error('Failed to remove source:', err);
		alert('Error: ' + err.message);
	}
}

function showAddSourceForm() {
	document.getElementById('add-source-form').classList.remove('hidden');
	document.getElementById('new-source-url').focus();
}

function hideAddSourceForm() {
	document.getElementById('add-source-form').classList.add('hidden');
	document.getElementById('new-source-url').value = '';
}

async function addNewSource() {
	const url = document.getElementById('new-source-url').value.trim();

	if (!url) {
		alert('Please enter a source URL');
		return;
	}

	// Validate URL — NOTE: fixed extra spaces in comparison
	if (!url.startsWith('https://mangadex.org/') && !url.startsWith('https://kagane.org/')) {
		alert('Only MangaDex and Kagane sources are supported');
		return;
	}

	try {
		const res = await fetch(`/api/series/${currentSeriesIdForEdit}/sources`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ source_url: url })
		});

		if (res.ok) {
			hideAddSourceForm();
			await loadSeriesSources(currentSeriesIdForEdit);
			alert('✅ Source added! Fetching chapters...');
		} else {
			const data = await res.json();
			alert('Failed to add source: ' + (data.error || 'Unknown error'));
		}
	} catch (err) {
		console.error('Failed to add source:', err);
		alert('Error: ' + err.message);
	}
}

// ─── Card Rendering ──────────────────────────────────────────
function renderSeriesCard(series) {
	const initialCurrent = parseFloat(series.current_chapter);
	const isNotStarted = (initialCurrent === -1);
	const card = document.createElement('div');
	card.className = 'series-card';
	card.dataset.seriesId = series.id;
	let releaseText = '';
	if (series.latest_release) {
		const releaseDate = new Date(series.latest_release);
		const diffMs = Date.now() - releaseDate.getTime();
		const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
		const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
		if (diffDays > 0) releaseText = `${diffDays}d ago`;
		else if (diffHours > 0) releaseText = `${diffHours}h ago`;
		else releaseText = 'Just now';
	}
	const cleanCoverUrl = (series.cover_protected_url || series.cover_url || '/static/placeholder.png').replace(/\s+/g, '');
	card.innerHTML = `
<div class="series-cover-container">
<div class="series-checkbox">
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
<path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/>
</svg>
</div>
<img class="series-cover" src="${cleanCoverUrl}" onerror="this.src='/static/placeholder.png'">
${releaseText ? `<div class="last-release">${releaseText}</div>` : ''}
</div>
<div class="card-info">
<div class="card-title">${series.title}</div>
<div class="card-chapters">
<span class="chapter-not-started">Loading...</span>
<span class="chapter-not-started">Loading...</span>
</div>
<div class="card-button-groups">
<div class="button-group single-group">
<button class="btn-set" title="Settings">
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
<path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"/>
</svg>
</button>
<button class="btn-search-google" title="Search Next Chapter">
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<circle cx="12" cy="12" r="10"></circle>
<path d="M2 12h20"></path>
<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path>
</svg>
</button>
<button class="btn-source" title="Go to Source">
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M15 3h6v6"></path>
<path d="M10 14 21 3"></path>
<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
</svg>
</button>
<button class="btn-dec" title="Previous">
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M5 12h14"></path>
</svg>
</button>
<button class="btn-inc" title="Next">
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M5 12h14"></path>
<path d="M12 5v14"></path>
</svg>
</button>
<button class="btn-accept" title="Confirm">
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M18 6 7 17l-5-5"></path>
<path d="m22 10-7.5 7.5L13 16"></path>
</svg>
</button>
</div>
</div>
<div class="card-buttons-bottom">
<button class="btn-next">Loading...</button>
</div>
</div>
`;
	const btnSet = card.querySelector('.btn-set');
	const btnSearchGoogle = card.querySelector('.btn-search-google');
	const btnSource = card.querySelector('.btn-source');
	const btnDec = card.querySelector('.btn-dec');
	const btnInc = card.querySelector('.btn-inc');
	const btnAccept = card.querySelector('.btn-accept');
	const btnNext = card.querySelector('.btn-next');

	function makeChapterComparator(useVolume) {
		return (a, b) => {
			if (useVolume) {
				const volA = getVolumeKey(a.volume);
				const volB = getVolumeKey(b.volume);
				if (volA[0] !== volB[0]) return volA[0] - volB[0];
				if (volA[0] === 1) {
					if (volA[1] !== volB[1]) return volA[1] - volB[1];
				} else {
					if (volA[1] !== volB[1]) return volA[1].localeCompare(volB[1]);
				}
				return a.chapter_number - b.chapter_number;
			} else {
				return a.chapter_number - b.chapter_number;
			}
		};
	}

	function formatChapterLabel(chapterData, useVolume) {
		if (chapterData.is_oneshot) {
			return 'Oneshot';
		}
		if (useVolume && chapterData.volume) {
			return `Ch.${chapterData.chapter_number} (Vol.${chapterData.volume})`;
		}
		return `Ch.${chapterData.chapter_number}`;
	}

	(async () => {
		try {
			const res = await fetch(`/api/series/${series.id}/chapters`);
			let chapters = res.ok ? await res.json() : [];
			const hasAnyNullVolume = chapters.some(ch => ch.volume == null || ch.volume === '');
			const useVolumeSorting = !hasAnyNullVolume;
			const comparator = makeChapterComparator(useVolumeSorting);
			const sortedChapters = [...chapters].sort(comparator);
			card.sortedChapters = sortedChapters;
			card.useVolumeSorting = useVolumeSorting;
			let pendingIndex = -1;
			if (isNotStarted) {
			pendingIndex = -1;
			} else {
				const targetNum = initialCurrent;
				const matches = sortedChapters
					.map((ch, idx) => ({ ch, idx }))
					.filter(item => item.ch.chapter_number === targetNum);
				if (matches.length === 0) {
					pendingIndex = -1;
				} else if (matches.length === 1) {
					pendingIndex = matches[0].idx;
				} else {
					if (useVolumeSorting) {
						const best = matches.reduce((a, b) => {
							const volA = a.ch.volume ? parseFloat(a.ch.volume) || 0 : 0;
							const volB = b.ch.volume ? parseFloat(b.ch.volume) || 0 : 0;
							return volB > volA ? b : a;
						});
						pendingIndex = best.idx;
					} else {
						pendingIndex = matches[0].idx;
					}
				}
			}
			card.pendingIndex = pendingIndex;
			card.originalIndex = pendingIndex;
			const coverContainer = card.querySelector('.series-cover-container');
			const existingBadge = coverContainer.querySelector('.unread-badge');
			if (existingBadge) existingBadge.remove();
			let unreadCount = 0;
			if (sortedChapters.length > 0) {
				const numeric = sortedChapters.filter(ch => !ch.is_oneshot);
				if (numeric.length > 0) {
					if (pendingIndex === -1) {
						unreadCount = numeric.length;
					} else {
						unreadCount = sortedChapters
							.slice(pendingIndex + 1)
							.filter(ch => !ch.is_oneshot).length;
					}
				} else if (pendingIndex === -1) {
					unreadCount = sortedChapters.length;
				}
			}
			if (unreadCount > 0) {
				const badge = document.createElement('div');
				badge.className = 'unread-badge';
				badge.textContent = String(unreadCount);
				coverContainer.insertBefore(badge, coverContainer.firstChild);
			}
			updateChapterDisplay();
			updateButtonState();
		} catch (e) {
			console.error('Chapter fetch failed:', e);
			card.sortedChapters = [];
			card.useVolumeSorting = false;
			card.pendingIndex = -1;
			updateChapterDisplay();
			updateButtonState();
		}
	})();

	function updateChapterDisplay() {
		const sorted = card.sortedChapters || [];
		const useVolume = card.useVolumeSorting;
		const isNowNotStarted = (card.pendingIndex === -1);
		let currentHtml;
		if (isNowNotStarted) {
			currentHtml = `<span class="chapter-not-started">Not started</span>`;
		} else {
			const currentCh = sorted[card.pendingIndex];
			if (currentCh) {
				const label = formatChapterLabel(currentCh, useVolume);
				currentHtml = `<a href="${currentCh.chapter_url}" target="_blank" class="chapter-link">${label}</a>`;
			} else {
				currentHtml = `<span class="chapter-not-started">Ch.? (invalid)</span>`;
			}
		}
		let latestHtml;
		if (sorted.length === 0) {
			latestHtml = `<span class="chapter-not-started">Ch. ?</span>`;
		} else {
			const latestCh = sorted[sorted.length - 1];
			const label = formatChapterLabel(latestCh, useVolume);
			latestHtml = `<a href="${latestCh.chapter_url}" target="_blank" class="chapter-link">${label}</a>`;
		}
		card.querySelector('.card-chapters').innerHTML = `${currentHtml}${latestHtml}`;
		if (sorted.length === 0) {
			btnNext.textContent = 'No chapters';
			btnNext.disabled = true;
			btnNext.onclick = null;
		} else if (isNowNotStarted) {
			const firstToRead = sorted[0];
			const label = formatChapterLabel(firstToRead, useVolume);
			btnNext.textContent = `Continue to ${label}`;
			btnNext.onclick = () => window.open(firstToRead.chapter_url, '_blank');
			btnNext.disabled = false;
		} else {
			if (card.pendingIndex < sorted.length - 1) {
				const nextCh = sorted[card.pendingIndex + 1];
				const label = formatChapterLabel(nextCh, useVolume);
				btnNext.textContent = `Continue to ${label}`;
				btnNext.onclick = () => window.open(nextCh.chapter_url, '_blank');
				btnNext.disabled = false;
			} else {
				btnNext.textContent = 'No new chapter';
				btnNext.disabled = true;
				btnNext.onclick = null;
			}
		}
	}

	function updateButtonState() {
		const hasChanged = card.pendingIndex !== card.originalIndex;
		btnAccept.disabled = !hasChanged;
	}

	btnInc.addEventListener('click', () => {
		const sorted = card.sortedChapters || [];
		if (sorted.length === 0) return;
		if (card.pendingIndex === -1) {
			card.pendingIndex = 0;
		} else if (card.pendingIndex < sorted.length - 1) {
			card.pendingIndex++;
		}
		updateChapterDisplay();
		updateButtonState();
	});
	btnDec.addEventListener('click', () => {
		if (card.pendingIndex === -1) {
		} else if (card.pendingIndex === 0) {
			card.pendingIndex = -1;
		} else {
			card.pendingIndex--;
		}
		updateChapterDisplay();
		updateButtonState();
	});
	btnAccept.addEventListener('click', () => {
		if (card.pendingIndex === -1) {
			saveChapter(series.id, -1).then(() => loadPage());
		} else {
			const ch = card.sortedChapters[card.pendingIndex];
			saveChapter(series.id, ch.chapter_number).then(() => loadPage());
		}
	});
	btnSet.addEventListener('click', () => openEditModal(series));
	btnSearchGoogle.addEventListener('click', () => {
		const sorted = card.sortedChapters || [];
		let nextChapterNum;
		if (card.pendingIndex === -1) {
			nextChapterNum = sorted.length > 0 ? sorted[0].chapter_number : 1;
		} else if (card.pendingIndex < sorted.length - 1) {
			nextChapterNum = sorted[card.pendingIndex + 1].chapter_number;
		} else {
			nextChapterNum = sorted[sorted.length - 1].chapter_number + 1;
		}
		const query = encodeURIComponent(`${series.title} chapter ${nextChapterNum}`);
		window.open(`https://www.google.com/search?q=${query}`, '_blank');
	});
	btnSource.addEventListener('click', () => {
		// Use primary source URL if available, otherwise fallback
		let sourceUrl = series.source_url;
		if (pendingSourceChanges.primarySourceId && currentSeriesIdForEdit === series.id) {
			// In current modal, get from pending sources
			const container = document.getElementById('sources-list');
			if (container) {
				const primaryEl = container.querySelector('.source-item.primary');
				if (primaryEl) {
					sourceUrl = primaryEl.querySelector('.source-url').textContent;
				}
			}
		}
		window.open(sourceUrl, '_blank');
	});
	const checkbox = card.querySelector('.series-checkbox');
	checkbox.addEventListener('click', (e) => {
		e.stopPropagation();
		toggleCardSelection(series.id);
	});
	card.addEventListener('click', (e) => {
		if (e.target.closest('button') || e.target.closest('a')) return;
		if (bulkState.isBulkMode) {
			toggleCardSelection(series.id);
		}
	});
	return card;
}

// ─── Page Loading & Pagination ───────────────────────────────
function renderPagination(current, total, status, sort) {
	const paginationTop = document.getElementById('pagination');
	const paginationBottom = document.getElementById('pagination-bottom');
	if (total <= 1) {
		paginationTop.innerHTML = '';
		paginationBottom.innerHTML = '';
		paginationTop.style.display = 'none';
		paginationBottom.style.display = 'none';
		return;
	}

	paginationTop.style.display = '';
	paginationBottom.style.display = '';

	function renderNav() {
		const nav = document.createElement('nav');
		nav.className = 'relative z-0 inline-flex shadow-sm rounded-md';
		const prevBtn = document.createElement('a');
		prevBtn.className = 'left-chevron px-3 py-1 flex items-center justify-center';
		prevBtn.href = 'javascript:void(0)';
		if (current <= 1) {
			prevBtn.classList.add('cursor-not-allowed', 'opacity-50');
		} else {
			prevBtn.addEventListener('click', () => {
				state.page = current - 1;
				loadPage();
			});
		}
		const prevSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
		prevSvg.setAttribute('viewBox', '0 0 20 20');
		prevSvg.setAttribute('fill', 'currentColor');
		prevSvg.classList.add('w-5', 'h-5');
		const prevPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
		prevPath.setAttribute('fill-rule', 'evenodd');
		prevPath.setAttribute('d', 'M12.707 5.293a1 1 0 010 1.414L9.414 10l3.293 3.293a1 1 0 01-1.414 1.414l-4-4a1 1 0 010-1.414l4-4a1 1 0 011.414 0z');
		prevPath.setAttribute('clip-rule', 'evenodd');
		prevSvg.appendChild(prevPath);
		prevBtn.appendChild(prevSvg);
		nav.appendChild(prevBtn);
		const delta = 2;
		let start = Math.max(1, current - delta);
		let end = Math.min(total, current + delta);
		const pages = [];
		if (start > 1) {
			pages.push(1);
			if (start > 2) pages.push('gap');
		}
		for (let i = start; i <= end; i++) {
			pages.push(i);
		}
		if (end < total) {
			if (end < total - 1) pages.push('gap');
			pages.push(total);
		}
		pages.forEach(page => {
			if (page === 'gap') {
				const gap = document.createElement('span');
				gap.className = 'gap px-3 py-1 flex items-center justify-center';
				gap.textContent = '…';
				nav.appendChild(gap);
			} else {
				const pageBtn = document.createElement('a');
				pageBtn.className = '-ml-px px-3 py-1 flex items-center justify-center cursor-pointer';
				pageBtn.textContent = String(page);
				pageBtn.href = 'javascript:void(0)';
				if (page === current) {
					pageBtn.classList.add('page__current');
				} else {
					pageBtn.addEventListener('click', () => {
						state.page = page;
						loadPage();
					});
				}
				nav.appendChild(pageBtn);
			}
		});
		const nextBtn = document.createElement('a');
		nextBtn.className = 'right-chevron px-3 py-1 flex items-center justify-center';
		nextBtn.href = 'javascript:void(0)';
		if (current >= total) {
			nextBtn.classList.add('cursor-not-allowed', 'opacity-50');
		} else {
			nextBtn.addEventListener('click', () => {
				state.page = current + 1;
				loadPage();
			});
		}
		const nextSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
		nextSvg.setAttribute('viewBox', '0 0 20 20');
		nextSvg.setAttribute('fill', 'currentColor');
		nextSvg.classList.add('w-5', 'h-5');
		const nextPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
		nextPath.setAttribute('fill-rule', 'evenodd');
		nextPath.setAttribute('d', 'M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z');
		nextPath.setAttribute('clip-rule', 'evenodd');
		nextSvg.appendChild(nextPath);
		nextBtn.appendChild(nextSvg);
		nav.appendChild(nextBtn);
		return nav;
	}
	paginationTop.innerHTML = '';
	paginationBottom.innerHTML = '';
	paginationTop.appendChild(renderNav());
	paginationBottom.appendChild(renderNav());
}

// Fetch and auto-update unread error count
async function updateUnreadErrorCount() {
	try {
		const res = await fetch('/api/unread-error-count');
		if (res.ok) {
			const { count } = await res.json();
			const badge = document.getElementById('unread-error-count');
			if (badge) {
				if (count > 0) {
					badge.textContent = count;
					badge.style.display = 'inline';
				} else {
					badge.style.display = 'none';
				}
			}
		}
	} catch (e) {
		// silent fail
	}
}

// ─── Load Page (Main Logic) ───────────────────────────────────
async function loadPage() {
	const { page, status, sort, dir, type, genre, rating, pubStatus, readableOn } = state;
	const searchInput = document.getElementById('search-input');
	const searchQuery = searchInput ? searchInput.value.trim() : '';
	const iconEl = document.getElementById('sort-direction-icon');
	if (iconEl) {
		iconEl.innerHTML = SORT_ICONS[dir];
	}
	const sortBtn = document.getElementById('btn-sort-direction');
	if (sortBtn) {
		sortBtn.onclick = () => {
			state.dir = state.dir === 'asc' ? 'desc' : 'asc';
			loadPage();
		};
	}
	let url = `/api/series?page=${page}&per_page=50&status=${encodeURIComponent(status)}&sort=${encodeURIComponent(sort)}&dir=${encodeURIComponent(dir)}`;
	if (searchQuery) {
		url += `&search=${encodeURIComponent(searchQuery)}`;
	}
	if (Array.isArray(state.type) && state.type.length > 0) {
		url += `&type=${encodeURIComponent(state.type.join(','))}`;
	}
	if (Array.isArray(state.genre) && state.genre.length > 0) {
		url += `&genre=${encodeURIComponent(state.genre.join(','))}`;
	}
	if (Array.isArray(state.rating) && state.rating.length > 0) {
		url += `&rating=${encodeURIComponent(state.rating.join(','))}`;
	}
	if (Array.isArray(state.pubStatus) && state.pubStatus.length > 0) {
		url += `&pub_status=${encodeURIComponent(state.pubStatus.join(','))}`;
	}
	// NEW: Add readableOn filter
	if (Array.isArray(state.readableOn) && state.readableOn.length > 0) {
		url += `&readable_on=${encodeURIComponent(state.readableOn.join(','))}`;
	}
	try {
		const res = await fetch(url);
		if (!res.ok) throw new Error('Failed to load series');
		const data = await res.json();
		const seriesGrid = document.getElementById('series-grid');
		seriesGrid.innerHTML = data.items.length === 0 ? '<p>No series found.</p>' : '';
		data.items.forEach(series => seriesGrid.appendChild(renderSeriesCard(series)));
		renderPagination(data.current_page, data.total_pages, status, sort);
	} catch (err) {
		document.getElementById('series-grid').innerHTML = `<p>Error: ${err.message}</p>`;
		console.error(err);
	}
}

// ─── Multi-select helpers ─────────────────────────────────────
function updateTriggerText(trigger, selected, labelMap = null, defaultText = 'Select') {
	if (selected.length === 0) {
		trigger.textContent = defaultText;
	} else if (selected.length === 1) {
		const value = selected[0];
		trigger.textContent = labelMap?.[value] || value;
	} else {
		trigger.textContent = `${selected.length} Selected`;
	}
}

function closeAllMultiSelectMenus(exceptMenu = null) {
	document.querySelectorAll('.multi-select-menu, .single-select-menu').forEach(menu => {
		if (menu !== exceptMenu) {
			menu.classList.add('hidden');
		}
	});
}

function setupStaticMultiSelect(trigger, menu, checkboxes, stateKey, labelMap = null, defaultText = 'Select') {
	document.addEventListener('click', (e) => {
		if (!menu.contains(e.target) && e.target !== trigger) {
			menu.classList.add('hidden');
		}
	});
	trigger.addEventListener('click', (e) => {
		e.stopPropagation();
		menu.classList.toggle('hidden');
		if (!menu.classList.contains('hidden')) {
			closeAllMultiSelectMenus(menu);
			const rect = trigger.getBoundingClientRect();
			menu.style.position = 'fixed'; // Changed from absolute
			menu.style.left = rect.left + 'px';
			menu.style.top = (rect.bottom + 4) + 'px';
			menu.style.width = rect.width + 'px';
			menu.style.zIndex = '1001';
		}
	});
	checkboxes.forEach(cb => {
		cb.addEventListener('change', () => {
			const selected = Array.from(checkboxes)
				.filter(cb => cb.checked)
				.map(cb => cb.value);
			state[stateKey] = selected;
			state.page = 1;
			loadPage();
			updateTriggerText(trigger, selected, labelMap, defaultText);
		});
	});
	const btnSelectAll = menu.querySelector('.btn-select-all');
	const btnSelectNone = menu.querySelector('.btn-select-none');
	if (btnSelectAll) {
		btnSelectAll.addEventListener('click', () => {
			checkboxes.forEach(cb => cb.checked = true);
			state[stateKey] = Array.from(checkboxes).map(cb => cb.value);
			state.page = 1;
			loadPage();
			menu.classList.add('hidden');
			updateTriggerText(trigger, state[stateKey], labelMap, defaultText);
		});
	}
	if (btnSelectNone) {
		btnSelectNone.addEventListener('click', () => {
			checkboxes.forEach(cb => cb.checked = false);
			state[stateKey] = [];
			state.page = 1;
			loadPage();
			menu.classList.add('hidden');
			updateTriggerText(trigger, [], labelMap, defaultText);
		});
	}
	checkboxes.forEach(cb => {
		cb.checked = state[stateKey].includes(cb.value);
	});
	updateTriggerText(trigger, state[stateKey], labelMap, defaultText);
}

function setupSingleSelect(trigger, menu, stateKey, labelMap, defaultValue) {
	const options = menu.querySelectorAll('.option-item');
	document.addEventListener('click', (e) => {
		if (!menu.contains(e.target) && e.target !== trigger) {
			menu.classList.add('hidden');
		}
	});
	trigger.addEventListener('click', (e) => {
		e.stopPropagation();
		menu.classList.toggle('hidden');
		if (!menu.classList.contains('hidden')) {
			closeAllMultiSelectMenus(menu);
			const rect = trigger.getBoundingClientRect();
			menu.style.position = 'fixed'; // Changed from absolute
			menu.style.left = rect.left + 'px';
			menu.style.top = (rect.bottom + 4) + 'px';
			menu.style.width = rect.width + 'px';
			menu.style.zIndex = '1001';
		}
	});
	options.forEach(option => {
		option.addEventListener('click', () => {
			const value = option.dataset.value;
			state[stateKey] = value;
			state.page = 1;
			options.forEach(opt => opt.classList.remove('selected'));
			option.classList.add('selected');
			trigger.textContent = labelMap[value] || value;
			menu.classList.add('hidden');
			loadPage();
		});
	});
	trigger.textContent = labelMap[state[stateKey]] || labelMap[defaultValue];
	options.forEach(opt => {
		if (opt.dataset.value === state[stateKey]) {
			opt.classList.add('selected');
		}
	});
}

// ─── Initial Setup ────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
	updateUnreadErrorCount();
	setInterval(updateUnreadErrorCount, 30000);
	if (window.location.search) {
		window.history.replaceState({}, '', '/');
	}
	const btnAddSeries = document.getElementById('btn-add-series');
	const addModal = document.getElementById('add-series-modal');
	const editModal = document.getElementById('edit-series-modal');
	const searchInput = document.getElementById('search-input');

	// Status
	const statusTrigger = document.getElementById('filter-status-trigger');
	const statusMenu = document.querySelector('#filter-status-container .single-select-menu');
	setupSingleSelect(statusTrigger, statusMenu, 'status', {
		'all': 'All Statuses',
		'reading': 'Reading',
		'plan_to_read': 'Plan to Read',
		'on_hold': 'On Hold',
		'dropped': 'Dropped',
		'completed': 'Completed'
	}, 'reading');

	// Sort
	const sortTrigger = document.getElementById('sort-order-trigger');
	const sortMenu = document.querySelector('#sort-order-container .single-select-menu');
	setupSingleSelect(sortTrigger, sortMenu, 'sort', {
		'unread_first': 'Unread First',
		'title': 'Title (A→Z)',
		'latest_release': 'Chapter Released',
		'last_added': 'Last Added',
		'total_chapters': 'Total Chapters'
	}, 'unread_first');

	// Content Type
	const typeTrigger = document.getElementById('filter-type-trigger');
	const typeMenu = document.querySelector('#filter-type-container .multi-select-menu');
	const typeCheckboxes = typeMenu.querySelectorAll('input[type="checkbox"]');
	setupStaticMultiSelect(typeTrigger, typeMenu, typeCheckboxes, 'type', {
		'manga': 'Manga',
		'manhwa': 'Manhwa',
		'manhua': 'Manhua',
		'other': 'Other'
	}, 'Content Type');

	// NEW: Readable On filter
	const readableOnTrigger = document.getElementById('filter-readable-on-trigger');
	const readableOnMenu = document.querySelector('#filter-readable-on-container .multi-select-menu');
	const readableOnCheckboxes = readableOnMenu.querySelectorAll('input[type="checkbox"]');
	setupStaticMultiSelect(readableOnTrigger, readableOnMenu, readableOnCheckboxes, 'readableOn', {
		'mangadex': 'MangaDex',
		'kagane': 'Kagane'
	}, 'Readable On');

	// Genre (Tags) - NOW WITH CONTENT RATING INSIDE THE SAME DROPDOWN
	const genreTrigger = document.getElementById('filter-genre-trigger');
	const genreMenu = document.getElementById('filter-genre-menu');
	const genreListSection = genreMenu.querySelector('.genre-list-section');
	const ratingCheckboxes = genreMenu.querySelectorAll('.rating-checkbox');
	const clearAllBtn = document.getElementById('btn-clear-all-tags');

	// Close menu when clicking outside
	document.addEventListener('click', (e) => {
		if (!genreMenu.contains(e.target) && e.target !== genreTrigger) {
			genreMenu.classList.add('hidden');
		}
	});

	// Toggle menu on trigger click
	genreTrigger.addEventListener('click', (e) => {
		e.stopPropagation();
		genreMenu.classList.toggle('hidden');
		if (!genreMenu.classList.contains('hidden')) {
			closeAllMultiSelectMenus(genreMenu);
			const rect = genreTrigger.getBoundingClientRect();
			genreMenu.style.position = 'fixed'; // Changed from absolute
			genreMenu.style.left = rect.left + 'px';
			genreMenu.style.top = (rect.bottom + 4) + 'px';
			genreMenu.style.width = rect.width + 'px';
			genreMenu.style.zIndex = '1001';
			// Scroll to top when opening
			const scrollContainer = genreMenu.querySelector('.combined-tags-list');
			if (scrollContainer) {
				scrollContainer.scrollTop = 0;
			}
		}
	});
	
	// Update trigger text based on both genres and ratings
	function updateTagsTriggerText() {
		const genreCount = state.genre.length;
		const ratingCount = state.rating.length;
		const totalCount = genreCount + ratingCount;
		
		if (totalCount === 0) {
			genreTrigger.textContent = 'Tags';
		} else if (totalCount === 1) {
			if (genreCount === 1) {
				genreTrigger.textContent = state.genre[0];
			} else {
				const ratingMap = {
					'safe': 'Safe',
					'mild': 'Suggestive',
					'mature': 'Mature',
					'explicit': 'Explicit'
				};
				genreTrigger.textContent = ratingMap[state.rating[0]] || state.rating[0];
			}
		} else {
			genreTrigger.textContent = `${totalCount} Selected`;
		}
	}
	
	// Clear All button (clears both genres and ratings)
	clearAllBtn.addEventListener('click', () => {
		state.genre = [];
		state.rating = [];
		state.page = 1;
		loadPage();
		genreListSection.querySelectorAll('input[type="checkbox"]').forEach(cb => cb.checked = false);
		ratingCheckboxes.forEach(cb => cb.checked = false);
		updateTagsTriggerText();
	});

	// Setup rating checkboxes (OR logic)
	ratingCheckboxes.forEach(cb => {
		cb.checked = state.rating.includes(cb.value);
		cb.addEventListener('change', () => {
			if (cb.checked) {
				if (!state.rating.includes(cb.value)) state.rating.push(cb.value);
			} else {
				state.rating = state.rating.filter(r => r !== cb.value);
			}
			state.page = 1;
			loadPage();
			updateTagsTriggerText();
		});
	});
	
	// Load genres dynamically
	loadGenres = async function() {
		try {
			const res = await fetch('/api/genres');
			if (res.ok) {
				const genres = await res.json();
				genreListSection.innerHTML = '';
				genres.forEach(genre => {
					const label = document.createElement('label');
					const cb = document.createElement('input');
					cb.type = 'checkbox';
					cb.value = genre;
					cb.checked = state.genre.includes(genre);
					cb.addEventListener('change', () => {
						if (cb.checked) {
							if (!state.genre.includes(genre)) state.genre.push(genre);
						} else {
							state.genre = state.genre.filter(g => g !== genre);
						}
						state.page = 1;
						loadPage();
						updateTagsTriggerText();
					});
					label.appendChild(cb);
					label.appendChild(document.createTextNode(genre));
					genreListSection.appendChild(label);
				});
			}
		} catch (e) {
			console.error('Failed to load genres:', e);
		}
	};


	// Publication Status
	const pubStatusTrigger = document.getElementById('filter-pub-status-trigger');
	const pubStatusMenu = document.querySelector('#filter-pub-status-container .multi-select-menu');
	const pubStatusCheckboxes = pubStatusMenu.querySelectorAll('input[type="checkbox"]');
	setupStaticMultiSelect(pubStatusTrigger, pubStatusMenu, pubStatusCheckboxes, 'pubStatus', {
		'reading': 'Reading',
		'completed': 'Completed',
		'on_hold': 'On Hold',
		'dropped': 'Dropped',
		'plan_to_read': 'Plan to Read'
	}, 'Publication Status');

	loadGenres();

	if (searchInput) {
		let searchTimeout;
		searchInput.addEventListener('input', () => {
			clearTimeout(searchTimeout);
			searchTimeout = setTimeout(() => {
				state.page = 1;
				loadPage();
			}, 300);
		});
	}

	// Modals
	if (btnAddSeries) {
		btnAddSeries.addEventListener('click', () => {
			const input = document.getElementById('new-series-url');
			if (input) input.value = '';
			addModal.classList.remove('hidden');
		});
	}

	// ─── Modified Save Button Handler ─────────────────────────────
	document.getElementById('btn-edit-save')?.addEventListener('click', async () => {
		if (!currentSeriesIdForEdit) {
			alert('No series selected');
			return;
		}

		const chapterSelect = document.getElementById('edit-current-chapter');
		let currentChapterValue = chapterSelect?.value;
		let currentChapterNum = -1;
		if (currentChapterValue !== "-1") {
			currentChapterNum = parseFloat(currentChapterValue);
			if (isNaN(currentChapterNum)) {
				alert('Invalid chapter selection');
				return;
			}
		}

		const updates = {
			title: document.getElementById('edit-title')?.value || '',
			source_url: document.getElementById('edit-source-url')?.value || '',
			cover_url: document.getElementById('edit-cover-url')?.value || '',
			status: document.getElementById('edit-status')?.value || 'plan_to_read',
			current_chapter: currentChapterNum
		};

		try {
			// Save source changes first if any
			if (pendingSourceChanges.hasChanges) {
				await saveSourceChanges(currentSeriesIdForEdit);
			}

			// Then save other updates
			const res = await fetch(`/api/series/${currentSeriesIdForEdit}`, {
				method: 'PATCH',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(updates)
			});

			if (res.ok) {
				editModal.classList.add('hidden');
				loadPage();
			} else {
				const err = await res.json().catch(() => ({}));
				alert('Save failed: ' + (err.error || 'Unknown error'));
			}
		} catch (e) {
			console.error('Save error:', e);
			alert('Network error: ' + e.message);
		}
	});

	// ─── Modified Cancel Button Handler ─────────────────────────────
	document.getElementById('btn-edit-cancel')?.addEventListener('click', () => {
		if (pendingSourceChanges.hasChanges) {
			if (!confirm('You have unsaved source changes. Discard them?')) {
				return;
			}
		}
		editModal.classList.add('hidden');
	});

	// ─── Modified Modal Close Handler ─────────────────────────────
	editModal?.addEventListener('click', (e) => {
		if (e.target === editModal) {
			if (pendingSourceChanges.hasChanges) {
				if (!confirm('You have unsaved source changes. Discard them?')) {
					return;
				}
			}
			editModal.classList.add('hidden');
		}
	});

	// UPDATED: Reset with new readableOn field
	document.getElementById('btn-reset-filters')?.addEventListener('click', () => {
		state.status = 'reading';
		state.sort = 'unread_first';
		state.dir = 'asc';
		state.type = [];
		state.genre = [];
		state.rating = [];
		state.pubStatus = [];
		state.readableOn = [];  // NEW
		state.page = 1;
		if (searchInput) searchInput.value = '';
		document.getElementById('filter-status-trigger').textContent = 'Reading';
		document.getElementById('sort-order-trigger').textContent = 'Unread First';
		document.querySelectorAll('.single-select-menu .option-item').forEach(opt => {
			opt.classList.remove('selected');
			if ((opt.dataset.value === 'reading' && opt.closest('#filter-status-container')) ||
				(opt.dataset.value === 'unread_first' && opt.closest('#sort-order-container'))) {
				opt.classList.add('selected');
			}
		});
		// Clear all checkboxes
		document.querySelectorAll(`
#filter-type-container input[type="checkbox"],
#filter-genre-container input[type="checkbox"],
#filter-genre-container .rating-checkbox,
#filter-pub-status-container input[type="checkbox"],
#filter-readable-on-container input[type="checkbox"]
`).forEach(cb => cb.checked = false);
		document.getElementById('filter-type-trigger').textContent = 'Content Type';
		document.getElementById('filter-genre-trigger').textContent = 'Tags';
		document.getElementById('filter-pub-status-trigger').textContent = 'Publication Status';
		document.getElementById('filter-readable-on-trigger').textContent = 'Readable On';  // NEW
		document.querySelectorAll('.multi-select-menu, .single-select-menu').forEach(menu => {
			menu.classList.add('hidden');
		});
		loadPage();
	});

	// Refresh
	document.getElementById('btn-refresh')?.addEventListener('click', async () => {
	const btn = document.getElementById('btn-refresh');
	
	// Add refreshing class to trigger animation
	btn.classList.add('refreshing');
	
	// Disable button during refresh
	btn.disabled = true;
	
	try {
		await loadPage();
	} finally {
		// Remove animation class after page loads
		// Use timeout to ensure animation completes
		setTimeout(() => {
		btn.classList.remove('refreshing');
		btn.disabled = false;
		}, 600); // Match animation duration
	}
	});

	// Add Series
	let isAdding = false;
	document.getElementById('btn-add-submit')?.addEventListener('click', async () => {
		if (isAdding) return;
		const btn = document.getElementById('btn-add-submit');
		const originalText = btn.textContent;
		const urlInput = document.getElementById('new-series-url');
		const url = urlInput?.value.trim();
		const statusSelect = document.getElementById('new-series-status');
		const selectedStatus = statusSelect?.value || 'reading';
		if (!url) {
			alert('Please enter a URL');
			return;
		}
		isAdding = true;
		btn.disabled = true;
		btn.textContent = 'Adding...';
		urlInput.disabled = true;
		statusSelect.disabled = true;
		try {
			const res = await fetch('/api/series', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ source_url: url, status: selectedStatus })
			});
			if (!res.ok) {
				const err = await res.json().catch(() => ({}));
				throw new Error(err.error || 'Failed to start add task');
			}
			const { task_id } = await res.json();
			document.getElementById('add-series-modal').classList.add('hidden');
			let attempt = 0;
			const maxAttempts = 60;
			const poll = async () => {
				if (attempt >= maxAttempts) {
					alert('⚠️ Add timed out. It may still be processing in the background.');
					loadPage();
					isAdding = false;
					return;
				}
				try {
					const statusRes = await fetch(`/api/series/add-status/${task_id}`);
					const statusData = await statusRes.json();
					if (statusData.status === 'pending') {
						attempt++;
						setTimeout(poll, 1000);
					} else {
						if (!statusData.success) {
							alert('Failed: ' + (statusData.error || 'Unknown error'));
						} else {
							loadGenres();
						}
						loadPage();
						isAdding = false;
					}
				} catch (e) {
					if (attempt >= maxAttempts - 1) {
						alert('Network error during add. Please check logs.');
						loadPage();
					}
					isAdding = false;
				}
			};
			poll();
		} catch (e) {
			alert('Error: ' + e.message);
			isAdding = false;
			loadPage();
		} finally {
			setTimeout(() => {
				btn.disabled = false;
				btn.textContent = originalText;
				if (urlInput) urlInput.disabled = false;
				if (statusSelect) statusSelect.disabled = false;
				isAdding = false;
			}, 500);
		}
	});

	// Cancel button handler
	document.getElementById('btn-add-cancel')?.addEventListener('click', () => {
	document.getElementById('add-series-modal').classList.add('hidden');
	});

	// Click-outside-to-close handler for Add Series modal
	document.getElementById('add-series-modal')?.addEventListener('click', (e) => {
	if (e.target.id === 'add-series-modal') {
		document.getElementById('add-series-modal').classList.add('hidden');
	}
	});

	// Delete
	document.getElementById('btn-delete-series')?.addEventListener('click', async () => {
		if (!confirm('Delete this series?')) return;
		try {
			const res = await fetch(`/api/series/${currentSeriesIdForEdit}`, {
				method: 'DELETE',
				headers: { 'Content-Type': 'application/json' }
			});
			if (res.ok) {
				editModal.classList.add('hidden');
				loadPage();
				loadGenres();
			} else {
				alert('Failed to delete series');
			}
		} catch (e) {
			alert('Network error: ' + e.message);
		}
	});

	// Reset to Not Started
	document.getElementById('btn-reset-not-started')?.addEventListener('click', () => {
		if (currentSeriesIdForEdit) {
			saveChapter(currentSeriesIdForEdit, -1).then(() => {
				editModal.classList.add('hidden');
				loadPage();
			});
		}
	});

	// Check Now
	document.getElementById('btn-check-now-modal')?.addEventListener('click', async () => {
		const btn = document.getElementById('btn-check-now-modal');
		btn.disabled = true;
		btn.textContent = 'Checking...';
		try {
			const res = await fetch(`/api/series/${currentSeriesIdForEdit}/check-now`, { method: 'POST' });
			if (res.ok) {
				btn.textContent = '✓ Done';
				setTimeout(() => {
					btn.textContent = 'Check Now';
					btn.disabled = false;
					loadPage();
				}, 1500);
			} else {
				btn.textContent = 'Error';
				setTimeout(() => { btn.disabled = false; btn.textContent = 'Check Now'; }, 1500);
			}
		} catch (e) {
			btn.textContent = 'Fail';
			setTimeout(() => { btn.disabled = false; btn.textContent = 'Check Now'; }, 1500);
		}
	});

	const backToTopBtn = document.getElementById('back-to-top');
	window.addEventListener('scroll', () => {
		if (window.scrollY > 300) {
			backToTopBtn.classList.add('show');
		} else {
			backToTopBtn.classList.remove('show');
		}
	});
	backToTopBtn.addEventListener('click', () => {
		window.scrollTo({ top: 0, behavior: 'smooth' });
	});

	// ─── Bulk Actions ───────────────────────────────────────────────
	// Bulk Read
	document.getElementById('btn-bulk-read')?.addEventListener('click', () => {
		const count = bulkState.selectedIds.size;
		document.getElementById('bulk-read-count').innerHTML =
			`This will mark <strong>${count} series</strong> as caught up to the latest chapter.`;
		document.getElementById('bulk-read-modal').classList.remove('hidden');
	});
	document.getElementById('btn-bulk-read-cancel')?.addEventListener('click', () => {
		document.getElementById('bulk-read-modal').classList.add('hidden');
	});
	document.getElementById('btn-bulk-read-confirm')?.addEventListener('click', async () => {
		const ids = Array.from(bulkState.selectedIds);
		document.getElementById('bulk-read-modal').classList.add('hidden');
		const bulkId = 'bulk_' + Date.now();  // Generate unique bulk ID
		for (const id of ids) {
			try {
				const res = await fetch(`/api/series/${id}/chapters`);
				const chapters = await res.json();
				if (chapters.length > 0) {
					const latestChapter = Math.max(...chapters.map(ch => ch.chapter_number));
					await fetch(`/api/series/${id}`, {
						method: 'PATCH',
						headers: { 'Content-Type': 'application/json' },
						body: JSON.stringify({
							current_chapter: latestChapter,
							_bulk_id: bulkId,
							_is_bulk: true
						})
					});
				}
			} catch (e) {
				console.error(`Failed to update series ${id}:`, e);
			}
		}
		exitBulkMode();
		loadPage();
	});

	// ─── Bulk Edit — REPLACED WITH MODAL FLOW ─────────────────────
	document.getElementById('btn-bulk-edit')?.addEventListener('click', () => {
		document.getElementById('bulk-edit-modal').classList.remove('hidden');
	});
	document.getElementById('btn-bulk-edit-cancel')?.addEventListener('click', () => {
		document.getElementById('bulk-edit-modal').classList.add('hidden');
	});
	document.querySelector('.bulk-edit-option[data-action="change-status"]')?.addEventListener('click', () => {
		document.getElementById('bulk-edit-modal').classList.add('hidden');
		document.getElementById('bulk-status-modal').classList.remove('hidden');
	});
	document.querySelectorAll('.status-option').forEach(option => {
		option.addEventListener('click', async () => {
			const newStatus = option.dataset.value;
			const ids = Array.from(bulkState.selectedIds);
			document.getElementById('bulk-status-modal').classList.add('hidden');
			const bulkId = 'bulk_' + Date.now();  // Generate unique bulk ID
			for (const id of ids) {
				try {
					await fetch(`/api/series/${id}`, {
						method: 'PATCH',
						headers: { 'Content-Type': 'application/json' },
						body: JSON.stringify({
							status: newStatus,
							_bulk_id: bulkId,
							_is_bulk: true
						})
					});
				} catch (e) {
					console.error(`Failed to update series ${id}:`, e);
				}
			}
			exitBulkMode();
			loadPage();
		});
	});
	document.getElementById('btn-status-back')?.addEventListener('click', () => {
		document.getElementById('bulk-status-modal').classList.add('hidden');
		document.getElementById('bulk-edit-modal').classList.remove('hidden');
	});

	// Close modals when clicking outside
	document.getElementById('bulk-edit-modal')?.addEventListener('click', (e) => {
		if (e.target.id === 'bulk-edit-modal') {
			document.getElementById('bulk-edit-modal').classList.add('hidden');
		}
	});
	document.getElementById('bulk-status-modal')?.addEventListener('click', (e) => {
		if (e.target.id === 'bulk-status-modal') {
			document.getElementById('bulk-status-modal').classList.add('hidden');
		}
	});
	document.getElementById('bulk-read-modal')?.addEventListener('click', (e) => {
		if (e.target.id === 'bulk-read-modal') {
			document.getElementById('bulk-read-modal').classList.add('hidden');
		}
	});
	document.getElementById('bulk-delete-modal')?.addEventListener('click', (e) => {
		if (e.target.id === 'bulk-delete-modal') {
			document.getElementById('bulk-delete-modal').classList.add('hidden');
		}
	});

	// Bulk Delete
	document.getElementById('btn-bulk-delete')?.addEventListener('click', () => {
		const count = bulkState.selectedIds.size;
		document.getElementById('bulk-delete-count').innerHTML =
			`This will permanently delete <strong>${count} series</strong>. This action cannot be undone.`;
		document.getElementById('bulk-delete-modal').classList.remove('hidden');
	});
	document.getElementById('btn-bulk-delete-cancel')?.addEventListener('click', () => {
		document.getElementById('bulk-delete-modal').classList.add('hidden');
	});
	document.getElementById('btn-bulk-delete-confirm')?.addEventListener('click', async () => {
		const ids = Array.from(bulkState.selectedIds);
		document.getElementById('bulk-delete-modal').classList.add('hidden');
		const bulkId = 'bulk_' + Date.now();  // Generate unique bulk ID
		for (const id of ids) {
			try {
				await fetch(`/api/series/${id}?bulk_id=${encodeURIComponent(bulkId)}`, {
					method: 'DELETE'
				});
			} catch (e) {
				console.error(`Failed to delete series ${id}:`, e);
			}
		}
		exitBulkMode();
		loadPage();
		loadGenres();
	});

	// Initial load
	loadPage();
});