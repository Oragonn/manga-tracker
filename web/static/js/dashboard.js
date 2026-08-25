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
	rating: [
		{ name: 'mature', mode: 'exclude' },
		{ name: 'explicit', mode: 'exclude' }
	],
	pubStatus: [],
	readableOn: [],
	allSeries: [], // Store fetched series
	hasLoadedOnce: false, // Track if initial load completed
	lastPage: undefined // Track page changes for scroll behavior
};

// Loading state to prevent multiple simultaneous loads
let isLoadingPage = false;

// Track default excluded ratings to hide from count
const DEFAULT_EXCLUDED_RATINGS = ['mature', 'explicit'];

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
function saveChapter(seriesId, chapter, oldChapter = null) {
  return fetch(`/api/series/${seriesId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ current_chapter: chapter })
  }).then(res => {
    // ADDED: Show notification on successful chapter update
    if (res.ok) {
      // Find series title and old chapter from card if not provided
      const card = document.querySelector(`.series-card[data-series-id="${seriesId}"]`);
      const seriesTitle = card?.querySelector('.card-title')?.textContent || 'Series';
      
      // Get old chapter if not provided
      if (oldChapter === null) {
        // Try to find it from state.allSeries
        const series = state.allSeries?.find(s => s.id === seriesId);
        oldChapter = series?.current_chapter ?? null;
      }
      
      // Format chapter text
      const formatChapter = (ch) => ch === -1 ? 'Not started' : `Ch.${ch}`;
      const newChapterText = formatChapter(chapter);
      
      // Show notification with or without old chapter
      if (oldChapter !== null && oldChapter !== chapter) {
        const oldChapterText = formatChapter(oldChapter);
        showNotification(`${seriesTitle} updated from ${oldChapterText} to ${newChapterText}`, 'read');
      } else {
        showNotification(`${seriesTitle} updated to ${newChapterText}`, 'read');
      }
    }
    return res;
  });
}

let currentSeriesIdForEdit = null;
// Store original values for comparison
let originalSeriesValues = null;

function openEditModal(series) {
	currentSeriesIdForEdit = series.id;
	
	// Store original values including current chapter
	originalSeriesValues = {
		title: series.title || '',
		cover_url: series.cover_url || '',
		status: series.status || 'plan_to_read',
		current_chapter: series.current_chapter
	};
	
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
	document.body.style.overflow = 'hidden';
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

  container.innerHTML = sortedSources.map(source => {
		// FIXED: Always use source.source_type directly (don't read from DOM)
		const sourceTypeLabel = {
			'mangadex': 'MangaDex',
			'kagane': 'Kagane',
			'unknown': 'Unknown'
		}[source.source_type.toLowerCase()] || source.source_type;

		const isPrimary = source.id === pendingSourceChanges.primarySourceId;

    return `
      <div class="source-item ${isPrimary ? 'primary' : ''}" data-source-id="${source.id}" data-source-type="${source.source_type}" draggable="true">
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
				source_type: item.dataset.sourceType, // CHANGED: Read from data attribute
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
		// ADDED: Show notification for source removed
		const seriesTitle = document.getElementById('edit-title')?.value || 'Series';
		showNotification(`Source removed from ${seriesTitle}`, 'delete');
		await loadSeriesSources(seriesId);
		} else {
		const data = await res.json();
		showNotification('Failed to remove source: ' + (data.error || 'Unknown error'), 'error'); // CHANGED
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
			// ADDED: Show notification for source removed
			const seriesTitle = document.getElementById('edit-title')?.value || 'Series';
			showNotification(`Source removed from ${seriesTitle}`, 'delete');
			await loadSeriesSources(seriesId);
		} else {
			const data = await res.json();
			showNotification('Failed to remove source: ' + (data.error || 'Unknown error'), 'error'); // CHANGED
		}
	} catch (err) {
		console.error('Failed to remove source:', err);
		showNotification('Error: ' + err.message, 'error');
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

	// Check if source already exists in the UI
	const existingSources = document.querySelectorAll('.source-item');
	for (const sourceItem of existingSources) {
		const existingUrl = sourceItem.querySelector('.source-url')?.textContent?.trim();
		if (existingUrl === url) {
			showNotification('Source already exists', 'error');
			return;
		}
	}

	try {
		const res = await fetch(`/api/series/${currentSeriesIdForEdit}/sources`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ source_url: url })
		});

		if (res.ok) {
			// ADDED: Show notification for source added
			const seriesTitle = document.getElementById('edit-title')?.value || 'Series';
			showNotification(`Source added to ${seriesTitle}`, 'added');
			hideAddSourceForm();
			await loadSeriesSources(currentSeriesIdForEdit);
		} else {
			const data = await res.json().catch(() => ({}));
			const errorMsg = data.error || 'Unknown error';
			
			// Since backend doesn't give specific errors, provide helpful message
			if (res.status === 500) {
				showNotification('Failed to add source - please check if it already exists or try again', 'error');
			} else if (errorMsg.toLowerCase().includes('already exists') || errorMsg.toLowerCase().includes('duplicate')) {
				showNotification('Source already exists', 'error');
			} else {
				showNotification(errorMsg, 'error');
			}
		}
	} catch (err) {
		console.error('Failed to add source:', err);
		showNotification('Network error - please try again', 'error');
	}
}

// ─── Skeleton Card Creation ──────────────────────────────────────
function createSkeletonCard() {
	const skeleton = document.createElement('div');
	skeleton.className = 'skeleton-card';
	skeleton.innerHTML = `
		<div class="skeleton-cover"></div>
		<div class="skeleton-content">
			<div class="skeleton-line title"></div>
			<div class="skeleton-line subtitle"></div>
			<div class="skeleton-line buttons"></div>
			<div class="skeleton-line btn-bottom"></div>
		</div>
	`;
	return skeleton;
}

// ─── Lazy Loading Image Observer ─────────────────────────────────
const imageObserver = new IntersectionObserver((entries) => {
	entries.forEach(entry => {
		if (entry.isIntersecting) {
			const img = entry.target;
			const src = img.dataset.src;
			if (src) {
				img.classList.add('loading');
				img.src = src;
				img.onload = () => {
					img.classList.remove('loading');
					img.classList.add('loaded');
				};
				img.onerror = () => {
					img.classList.remove('loading');
					img.src = '/static/placeholder.png';
				};
				imageObserver.unobserve(img);
			}
		}
	});
}, {
	rootMargin: '50px' // Start loading 50px before entering viewport
});

// ─── Card Rendering ──────────────────────────────────────────
function renderSeriesCard(series, chapters = null) {
	const initialCurrent = parseFloat(series.current_chapter);
	const isNotStarted = (initialCurrent === -1);
	const card = document.createElement('div');
	card.className = 'series-card fade-in';
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
	const placeholderUrl = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 221 331"%3E%3Crect fill="%231a2436" width="221" height="331"/%3E%3C/svg%3E';
	card.innerHTML = `
<div class="series-cover-container">
<div class="series-checkbox">
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
<path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/>
</svg>
</div>
<img class="series-cover loading" src="${placeholderUrl}" data-src="${cleanCoverUrl}" loading="lazy" onerror="this.src='/static/placeholder.png'">
${releaseText ? `<div class="last-release">${releaseText}</div>` : ''}
${isMobileDevice() ? `<div class="mobile-card-title"><span>${series.title}</span></div>` : ''}
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
			// Use provided chapters or fetch if not available
			let chaptersData;
			if (chapters !== null) {
				chaptersData = chapters;
			} else {
				const res = await fetch(`/api/series/${series.id}/chapters`);
				chaptersData = res.ok ? await res.json() : [];
			}
			
			const hasAnyNullVolume = chaptersData.some(ch => ch.volume == null || ch.volume === '');
			const useVolumeSorting = !hasAnyNullVolume;
			const comparator = makeChapterComparator(useVolumeSorting);
			const sortedChapters = [...chaptersData].sort(comparator);
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
		
		// FIX: Get the button container and replace the entire button
		const buttonContainer = card.querySelector('.card-buttons-bottom');
		if (!buttonContainer) return; // Safety check
		
		const oldBtn = buttonContainer.querySelector('.btn-next');
		if (!oldBtn) return; // Safety check
		
		// Create new button element
		const newBtn = document.createElement('button');
		newBtn.className = 'btn-next';
		
		if (sorted.length === 0) {
			newBtn.textContent = 'No chapters';
			newBtn.disabled = true;
		} else if (isNowNotStarted) {
			const firstToRead = sorted[0];
			const label = formatChapterLabel(firstToRead, useVolume);
			newBtn.textContent = `Continue to ${label}`;
			newBtn.disabled = false;
			
			// Handle left-click
			newBtn.addEventListener('click', (e) => {
				e.preventDefault();
				window.open(firstToRead.chapter_url, '_blank');
			});
			
			// Handle middle-click
			newBtn.addEventListener('auxclick', (e) => {
				if (e.button === 1) {
					e.preventDefault();
					window.open(firstToRead.chapter_url, '_blank');
				}
			});
		} else {
			if (card.pendingIndex < sorted.length - 1) {
				const nextCh = sorted[card.pendingIndex + 1];
				const label = formatChapterLabel(nextCh, useVolume);
				newBtn.textContent = `Continue to ${label}`;
				newBtn.disabled = false;
				
				// Handle left-click
				newBtn.addEventListener('click', (e) => {
					e.preventDefault();
					window.open(nextCh.chapter_url, '_blank');
				});
				
				// Handle middle-click
				newBtn.addEventListener('auxclick', (e) => {
					if (e.button === 1) {
						e.preventDefault();
						window.open(nextCh.chapter_url, '_blank');
					}
				});
			} else {
				newBtn.textContent = 'No new chapter';
				newBtn.disabled = true;
			}
		}
		
		// Replace the button
		buttonContainer.replaceChild(newBtn, oldBtn);
	}

	function updateButtonState() {
		const hasChanged = card.pendingIndex !== card.originalIndex;
		btnAccept.disabled = !hasChanged;
	}

	// Hold-to-repeat functionality
	let holdInterval = null;
	let holdTimeout = null;

	function startHoldRepeat(callback, initialDelay = 500, repeatInterval = 100) {
	// Clear any existing intervals
	if (holdInterval) clearInterval(holdInterval);
	if (holdTimeout) clearTimeout(holdTimeout);
	
	// Wait for initial delay, then start repeating
	holdTimeout = setTimeout(() => {
		callback(); // Execute first time after delay
		holdInterval = setInterval(callback, repeatInterval);
	}, initialDelay);
	}

	function stopHoldRepeat() {
	if (holdInterval) {
		clearInterval(holdInterval);
		holdInterval = null;
	}
	if (holdTimeout) {
		clearTimeout(holdTimeout);
		holdTimeout = null;
	}
	}

	// REPLACE the existing btnDec.addEventListener('click', ...) with:
	btnDec.addEventListener('mousedown', (e) => {
	e.preventDefault();
	startHoldRepeat(() => {
		if (card.pendingIndex === -1) {
		// Already at "Not started", do nothing
		} else if (card.pendingIndex === 0) {
		card.pendingIndex = -1;
		} else {
		card.pendingIndex--;
		}
		updateChapterDisplay();
		updateButtonState();
	}, 300, 50);
	});

	// Add click handler for single clicks
	btnDec.addEventListener('click', (e) => {
	e.preventDefault();
	if (card.pendingIndex === -1) {
		// Already at "Not started", do nothing
	} else if (card.pendingIndex === 0) {
		card.pendingIndex = -1;
	} else {
		card.pendingIndex--;
	}
	updateChapterDisplay();
	updateButtonState();
	});

	btnDec.addEventListener('mouseup', stopHoldRepeat);
	btnDec.addEventListener('mouseleave', stopHoldRepeat);

	// REPLACE the existing btnInc.addEventListener('click', ...) with:
	btnInc.addEventListener('mousedown', (e) => {
	e.preventDefault();
	startHoldRepeat(() => {
		const sorted = card.sortedChapters || [];
		if (sorted.length === 0) return;
		if (card.pendingIndex === -1) {
		card.pendingIndex = 0;
		} else if (card.pendingIndex < sorted.length - 1) {
		card.pendingIndex++;
		}
		updateChapterDisplay();
		updateButtonState();
	}, 300, 50);
	});

	// Add click handler for single clicks
	btnInc.addEventListener('click', (e) => {
	e.preventDefault();
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

	btnInc.addEventListener('mouseup', stopHoldRepeat);
	btnInc.addEventListener('mouseleave', stopHoldRepeat);

	// Also add touch support for mobile devices
	btnDec.addEventListener('touchstart', (e) => {
	e.preventDefault();
	startHoldRepeat(() => {
		if (card.pendingIndex === -1) {
		// Already at "Not started", do nothing
		} else if (card.pendingIndex === 0) {
		card.pendingIndex = -1;
		} else {
		card.pendingIndex--;
		}
		updateChapterDisplay();
		updateButtonState();
	}, 300, 50);
	});

	btnDec.addEventListener('touchend', stopHoldRepeat);
	btnDec.addEventListener('touchcancel', stopHoldRepeat);

	btnInc.addEventListener('touchstart', (e) => {
	e.preventDefault();
	startHoldRepeat(() => {
		const sorted = card.sortedChapters || [];
		if (sorted.length === 0) return;
		if (card.pendingIndex === -1) {
		card.pendingIndex = 0;
		} else if (card.pendingIndex < sorted.length - 1) {
		card.pendingIndex++;
		}
		updateChapterDisplay();
		updateButtonState();
	}, 300, 50);
	});

	btnInc.addEventListener('touchend', stopHoldRepeat);
	btnInc.addEventListener('touchcancel', stopHoldRepeat);

	btnAccept.addEventListener('click', () => {
		if (card.pendingIndex === -1) {
			saveChapter(series.id, -1, series.current_chapter).then(() => loadPage());
		} else {
			const ch = card.sortedChapters[card.pendingIndex];
			saveChapter(series.id, ch.chapter_number, series.current_chapter).then(() => loadPage());
		}
	});
	btnSet.addEventListener('click', () => openEditModal(series));
	
	// *** UPDATED: Use auxclick for proper middle-click detection ***
	// Search Google button - handle left-click
	btnSearchGoogle.addEventListener('click', (e) => {
		e.preventDefault();
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
	
	// Search Google button - handle middle-click
	btnSearchGoogle.addEventListener('auxclick', (e) => {
		if (e.button === 1) { // Middle click
			e.preventDefault();
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
		}
	});
	
	// Go to Source button - handle left-click
	btnSource.addEventListener('click', (e) => {
		e.preventDefault();
		let sourceUrl = series.source_url;
		if (pendingSourceChanges.primarySourceId && currentSeriesIdForEdit === series.id) {
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
	
	// Go to Source button - handle middle-click
	btnSource.addEventListener('auxclick', (e) => {
		if (e.button === 1) { // Middle click
			e.preventDefault();
			let sourceUrl = series.source_url;
			if (pendingSourceChanges.primarySourceId && currentSeriesIdForEdit === series.id) {
				const container = document.getElementById('sources-list');
				if (container) {
					const primaryEl = container.querySelector('.source-item.primary');
					if (primaryEl) {
						sourceUrl = primaryEl.querySelector('.source-url').textContent;
					}
				}
			}
			window.open(sourceUrl, '_blank');
		}
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
	
	// Setup lazy loading for cover image
	const coverImg = card.querySelector('.series-cover');
	if (coverImg && coverImg.dataset.src) {
		imageObserver.observe(coverImg);
	}
	
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
				if (!isLoadingPage) {
					state.page = current - 1;
					loadPage();
				}
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
						if (!isLoadingPage) {
							state.page = page;
							loadPage();
						}
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
				if (!isLoadingPage) {
					state.page = current + 1;
					loadPage();
				}
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
	// Prevent concurrent loads
	if (isLoadingPage) return;
	isLoadingPage = true;
	
	const { page, status, sort, dir, type, genre, rating, pubStatus, readableOn } = state;

	// FIX: Check both desktop and mobile search inputs
	const desktopSearch = document.getElementById('search-input');
	const mobileSearch = document.getElementById('mobile-search-input');
	const searchQuery = (desktopSearch?.value || mobileSearch?.value || '').trim();
	
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
	
	// Show skeleton cards immediately
	const seriesGrid = document.getElementById('series-grid');
	const isInitialLoad = !state.hasLoadedOnce;
	const skeletonCount = isInitialLoad ? 12 : 6;
	
	// Clear grid and show skeletons
	seriesGrid.innerHTML = '';
	for (let i = 0; i < skeletonCount; i++) {
		seriesGrid.appendChild(createSkeletonCard());
	}
	
	// Scroll to top smoothly on pagination
	if (state.page !== state.lastPage && state.lastPage !== undefined) {
		window.scrollTo({ top: 0, behavior: 'smooth' });
	}
	state.lastPage = state.page;
	
	let url = `/api/series?page=${page}&per_page=50&status=${encodeURIComponent(status)}&sort=${encodeURIComponent(sort)}&dir=${encodeURIComponent(dir)}`;
	if (searchQuery) {
		url += `&search=${encodeURIComponent(searchQuery)}`;
	}
	if (Array.isArray(state.type) && state.type.length > 0) {
		url += `&type=${encodeURIComponent(state.type.join(','))}`;
	}
	// Handle genre and rating with include/exclude modes
	if (Array.isArray(state.genre) && state.genre.length > 0) {
		const genreNames = state.genre.map(g => g.name).join(',');
		const genreModes = state.genre.map(g => g.mode).join(',');
		url += `&genre=${encodeURIComponent(genreNames)}`;
		url += `&genre_modes=${encodeURIComponent(genreModes)}`;
	}
	
	if (Array.isArray(state.rating) && state.rating.length > 0) {
		const ratingNames = state.rating.map(r => r.name).join(',');
		const ratingModes = state.rating.map(r => r.mode).join(',');
		url += `&rating=${encodeURIComponent(ratingNames)}`;
		url += `&rating_modes=${encodeURIComponent(ratingModes)}`;
	}
	if (Array.isArray(state.pubStatus) && state.pubStatus.length > 0) {
		url += `&pub_status=${encodeURIComponent(state.pubStatus.join(','))}`;
	}
	// NEW: Add readableOn filter
	if (Array.isArray(state.readableOn) && state.readableOn.length > 0) {
		url += `&readable_on=${encodeURIComponent(state.readableOn.join(','))}`;
	}
	
	try {
		// Fetch data (preload)
		const res = await fetch(url);
		if (!res.ok) throw new Error('Failed to load series');
		const data = await res.json();
		
		// Store all series for reference
		state.allSeries = data.items;
		state.hasLoadedOnce = true;
		
		// Clear grid and render new cards with stagger effect
		seriesGrid.innerHTML = '';
		
		if (data.items.length === 0) {
			seriesGrid.innerHTML = '<p>No series found.</p>';
		} else {
			// Render cards with chapters included, with slight stagger for visual polish
			data.items.forEach((series, index) => {
				const chapters = series.chapters || [];
				const card = renderSeriesCard(series, chapters);
				card.style.animationDelay = `${index * 0.03}s`;
				seriesGrid.appendChild(card);
			});
		}
		
		renderPagination(data.current_page, data.total_pages, status, sort);
	} catch (err) {
		seriesGrid.innerHTML = `
			<div style="grid-column: 1 / -1; text-align: center; padding: 60px 20px; color: #ef4444;">
				<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="margin: 0 auto 16px; display: block;">
					<circle cx="12" cy="12" r="10"></circle>
					<line x1="12" y1="8" x2="12" y2="12"></line>
					<line x1="12" y1="16" x2="12.01" y2="16"></line>
				</svg>
				<p style="font-size: 18px; font-weight: 600; margin-bottom: 8px; color: white;">Failed to load series</p>
				<p style="font-size: 14px; color: #94a3b8; margin-bottom: 16px;">${err.message}</p>
				<button onclick="loadPage()" style="padding: 10px 20px; background: #1665f4; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 500;">
					Retry
				</button>
			</div>
		`;
		console.error(err);
	} finally {
		// Always reset loading state
		isLoadingPage = false;
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
  const isMobileDrawer = trigger.id.startsWith('mobile-');
  
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
      
	// Only set width for desktop (position handled by CSS)
      if (!isMobileDrawer) {
        const rect = trigger.getBoundingClientRect();
        menu.style.width = rect.width + 'px';
      }
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
			menu.style.width = rect.width + 'px';
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
		'total_chapters': 'Total Chapters',
		'available_chapters': 'Available Chapters'
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
			genreMenu.style.width = (rect.width * 1.6) + 'px';
			// Scroll to top when opening
			const scrollContainer = genreMenu.querySelector('.combined-tags-list');
			if (scrollContainer) {
				scrollContainer.scrollTop = 0;
			}
		}
	});
	
	function updateTagsTriggerText() {
		const genreCount = state.genre.length;
		
		// Count only non-default ratings
		const nonDefaultRatings = state.rating.filter(r => {
			// If it's a default excluded rating in exclude mode, don't count it
			if (DEFAULT_EXCLUDED_RATINGS.includes(r.name) && r.mode === 'exclude') {
				return false;
			}
			return true;
		});
		const ratingCount = nonDefaultRatings.length;
		
		const totalCount = genreCount + ratingCount;
		
		if (totalCount === 0) {
			genreTrigger.textContent = 'Tags';
		} else {
			genreTrigger.textContent = `${totalCount} Selected`;
		}
	}
		
	// ADDED: Tags mode toggle button
	const tagsModeBtn = document.getElementById('btn-tags-mode');
	if (tagsModeBtn) {
		tagsModeBtn.addEventListener('click', (e) => {
			e.stopPropagation();
			const currentMode = state.tagsMode;
			const newMode = currentMode === 'include' ? 'exclude' : 'include';
			state.tagsMode = newMode;
			
			// Update button appearance
			tagsModeBtn.dataset.mode = newMode;
			const icon = tagsModeBtn.querySelector('svg');
			const text = tagsModeBtn.querySelector('span');
			
			if (newMode === 'exclude') {
				// X icon for exclude
				icon.innerHTML = '<line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line>';
				text.textContent = 'Exclude Mode';
			} else {
				// Checkmark icon for include
				icon.innerHTML = '<path d="M20 6L9 17l-5-5"/>';
				text.textContent = 'Include Mode';
			}
			
			// Reload if there are active filters
			if (state.genre.length > 0 || state.rating.length > 0) {
				state.page = 1;
				loadPage();
			}
			
			// Update trigger text
			updateTagsTriggerText();
		});
	}

	// Clear All button (clears both genres and ratings)
	clearAllBtn.addEventListener('click', () => {
		state.genre = [];
		state.rating = [];
		state.page = 1;
		
		// Reset all checkboxes data-mode
		genreListSection.querySelectorAll('input[type="checkbox"]').forEach(cb => {
			delete cb.dataset.mode;
		});
		ratingCheckboxes.forEach(cb => {
			delete cb.dataset.mode;
		});
		
		loadPage();
		updateTagsTriggerText();
	});

	// Setup rating checkboxes (3-state toggle: disabled → include → exclude → disabled)
	ratingCheckboxes.forEach(cb => {
		// Initialize state
		const existing = state.rating.find(r => r.name === cb.value);
		if (existing) {
			cb.dataset.mode = existing.mode;
		}
		
		// 3-state click handler
		cb.parentElement.addEventListener('click', (e) => {
			e.preventDefault();
			const current = state.rating.find(r => r.name === cb.value);
			
			// Remove current state
			state.rating = state.rating.filter(r => r.name !== cb.value);
			delete cb.dataset.mode;
			
			// Cycle: disabled → include → exclude → disabled
			if (!current) {
				// disabled → include
				state.rating.push({ name: cb.value, mode: 'include' });
				cb.dataset.mode = 'include';
			} else if (current.mode === 'include') {
				// include → exclude
				state.rating.push({ name: cb.value, mode: 'exclude' });
				cb.dataset.mode = 'exclude';
			}
			// else: exclude → disabled (already removed above)
			
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
					
					// Initialize state
					const existing = state.genre.find(g => g.name === genre);
					if (existing) {
						cb.dataset.mode = existing.mode;
					}
					
					label.appendChild(cb);
					label.appendChild(document.createTextNode(genre));
					
					// 3-state click handler
					label.addEventListener('click', (e) => {
						e.preventDefault();
						const current = state.genre.find(g => g.name === genre);
						
						// Remove current state
						state.genre = state.genre.filter(g => g.name !== genre);
						delete cb.dataset.mode;
						
						// Cycle: disabled → include → exclude → disabled
						if (!current) {
							// disabled → include
							state.genre.push({ name: genre, mode: 'include' });
							cb.dataset.mode = 'include';
						} else if (current.mode === 'include') {
							// include → exclude
							state.genre.push({ name: genre, mode: 'exclude' });
							cb.dataset.mode = 'exclude';
						}
						// else: exclude → disabled (already removed above)
						
						state.page = 1;
						loadPage();
						updateTagsTriggerText();
					});
					
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
	const clearBtn = document.getElementById('search-clear-btn'); // ADDED
	
	searchInput.addEventListener('input', () => {
		clearTimeout(searchTimeout);
		searchTimeout = setTimeout(() => {
		state.page = 1;
		loadPage();
		}, 300);
		
		// ADDED: Show/hide clear button
		if (searchInput.value.trim()) {
		clearBtn?.classList.add('show');
		} else {
		clearBtn?.classList.remove('show');
		}
	});
	
	// ADDED: Clear button handler
	if (clearBtn) {
		clearBtn.addEventListener('click', () => {
		searchInput.value = '';
		clearBtn.classList.remove('show');
		state.page = 1;
		loadPage();
		});
	}
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
				const seriesTitle = updates.title || 'Series';
				
				// Only show notifications for actual changes (compare with original values)
				if (originalSeriesValues) {
					// Check chapter change
					if (updates.current_chapter !== originalSeriesValues.current_chapter) {
					const formatChapter = (ch) => ch === -1 ? 'Not started' : `Ch.${ch}`;
					const oldChapterText = formatChapter(originalSeriesValues.current_chapter);
					const newChapterText = formatChapter(updates.current_chapter);
					showNotification(`${seriesTitle} updated from ${oldChapterText} to ${newChapterText}`, 'read');
					}
					
					// Check status change
					if (updates.status !== originalSeriesValues.status) {
					const statusMap = {
						'reading': 'Reading',
						'plan_to_read': 'Plan to Read',
						'on_hold': 'On Hold',
						'dropped': 'Dropped',
						'completed': 'Completed'
					};
					const statusText = statusMap[updates.status] || updates.status;
					showNotification(`${seriesTitle} marked as ${statusText}`, 'edit');
					}
					
					// Check title change
					if (updates.title !== originalSeriesValues.title) {
					showNotification(`${seriesTitle} updated`, 'edit');
					}
					
					// Check cover change
					if (updates.cover_url !== originalSeriesValues.cover_url) {
					showNotification(`${seriesTitle} cover image updated`, 'edit');
					}
				}
				
				editModal.classList.add('hidden');


				document.body.style.overflow = ''; // ADDED
				loadPage();
			} else {
				const err = await res.json().catch(() => ({}));
				showNotification('Save failed: ' + (err.error || 'Unknown error'), 'error'); // CHANGED
			}
		} catch (e) {
			console.error('Save error:', e);
			showNotification('Network error: ' + e.message, 'error');
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
		document.body.style.overflow = ''; // ADDED
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
			document.body.style.overflow = ''; // ADDED
		}
	});

	// UPDATED: Reset with new readableOn field and tagsMode
	document.getElementById('btn-reset-filters')?.addEventListener('click', () => {
		state.status = 'reading';
		state.sort = 'unread_first';
		state.dir = 'asc';
		state.type = [];
		state.genre = [];
		state.rating = [];
		state.pubStatus = [];
		state.readableOn = [];
		state.tagsMode = 'include';  // ADDED: Reset to include mode
		state.page = 1;
		
		// ADDED: Reset tags mode button
		const tagsModeBtn = document.getElementById('btn-tags-mode');
		if (tagsModeBtn) {
			tagsModeBtn.dataset.mode = 'include';
			const icon = tagsModeBtn.querySelector('svg');
			const text = tagsModeBtn.querySelector('span');
			icon.innerHTML = '<path d="M20 6L9 17l-5-5"/>';
			text.textContent = 'Include Mode';
		}
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
	if (isLoadingPage) return; // Prevent refresh during load
	
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
				const errorMsg = err.error || 'Unknown error';
				showNotification(`Failed to add series: ${errorMsg}`, 'error');
				isAdding = false;
				loadPage();
				return; // Don't throw, just return
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
							// ADDED: Show error notification
							showNotification('Failed to add series: ' + (statusData.error || 'Unknown error'), 'error');
						} else if (statusData.duplicate) {
							// ADDED: Show duplicate notification
							showNotification('Series already exists in your library', 'error');
						} else {
							// Success - series was added
							const status = statusData.series?.status || document.getElementById('new-series-status')?.value || 'plan_to_read';
							const statusMap = {
								'reading': 'Reading',
								'plan_to_read': 'Plan to Read',
								'on_hold': 'On Hold',
								'dropped': 'Dropped',
								'completed': 'Completed'
							};			
							const statusText = statusMap[status] || status;
							showNotification(`Series added to ${statusText}`, 'added');
							loadGenres();
						}
						loadPage();
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
			showNotification('Connection error - please try again', 'error');
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
				// ADDED: Show notification for deletion
				const seriesTitle = document.getElementById('edit-title')?.value || 'Series';
				showNotification(`${seriesTitle} deleted`, 'delete');
				
				editModal.classList.add('hidden');
				document.body.style.overflow = '';
				document.body.style.position = '';
				document.body.style.width = '';
				document.body.style.top = '';
				window.scrollTo(0, mobileState.scrollY || 0);
				loadPage();
				loadGenres();
			} else {
				showNotification('Failed to delete series', 'error'); // CHANGED
			}
		} catch (e) {
			showNotification('Network error: ' + e.message, 'error');
		}
	});

	// Reset to Not Started
	document.getElementById('btn-reset-not-started')?.addEventListener('click', () => {
		if (currentSeriesIdForEdit) {
			const currentSeries = state.allSeries?.find(s => s.id === currentSeriesIdForEdit);
			saveChapter(currentSeriesIdForEdit, -1, currentSeries?.current_chapter).then(() => {
				editModal.classList.add('hidden');
				document.body.style.overflow = '';
				document.body.style.position = '';
				document.body.style.width = '';
				document.body.style.top = '';
				window.scrollTo(0, mobileState.scrollY || 0);
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
		// ADDED: Show notification for bulk read
		const count = ids.length;
		showNotification(`${count} series marked as read`, 'read');
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
			// ADDED: Show notification for bulk status change
			const count = ids.length;
			const statusMap = {
			'reading': 'Reading',
			'plan_to_read': 'Plan to Read',
			'on_hold': 'On Hold',
			'dropped': 'Dropped',
			'completed': 'Completed'
			};
			const statusText = statusMap[newStatus] || newStatus;
			showNotification(`${count} series status changed to ${statusText}`, 'edit');
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
		// ADDED: Show notification for bulk delete
		const count = ids.length;
		showNotification(`${count} series deleted`, 'delete');
		exitBulkMode();
		loadPage();
		loadGenres();
	});

	// Initial load
	loadPage();
});

// ================================
// MOBILE UI FUNCTIONALITY
// Add this to the end of dashboard.js
// ================================

// Mobile state
const mobileState = {
  menuOpen: false,
  filterDrawerOpen: false,
  bottomSheetOpen: false,
  currentSeries: null,
  lastScrollY: 0,
  pendingChapter: null  // ADDED: Track unsaved chapter changes
};

// Bulk edit mode state
const bulkEditMobileState = {
  isMenuOpen: false,
  scrollY: 0
};

// ================================
// MOBILE DETECTION & INITIALIZATION
// ================================
function isMobileDevice() {
  return window.innerWidth <= 768;
}

// Helper function to format timestamps as "X time ago"
function formatTimeAgo(dateString) {
  if (!dateString) return 'Unknown';
  
  try {
    const date = new Date(dateString);
    const now = new Date();
    const diffMs = now - date;
    
    // Calculate time differences
    const diffMinutes = Math.floor(diffMs / (1000 * 60));
    const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
    const diffMonths = Math.floor(diffDays / 30);
    const diffYears = Math.floor(diffDays / 365);
    
    // Return appropriate format with pluralization
    if (diffMinutes < 1) {
      return 'Just now';
    } else if (diffMinutes < 60) {
      return diffMinutes === 1 ? '1 minute ago' : `${diffMinutes} minutes ago`;
    } else if (diffHours < 24) {
      return diffHours === 1 ? '1 hour ago' : `${diffHours} hours ago`;
    } else if (diffDays < 30) {
      return diffDays === 1 ? '1 day ago' : `${diffDays} days ago`;
    } else if (diffMonths < 12) {
      return diffMonths === 1 ? '1 month ago' : `${diffMonths} months ago`;
    } else {
      return diffYears === 1 ? '1 year ago' : `${diffYears} years ago`;
    }
  } catch (e) {
    console.error('Failed to parse date:', dateString, e);
    return 'Unknown';
  }
}

function initMobile() {
  if (!isMobileDevice()) return;

  // Create mobile header
  createMobileHeader();

  // Create mobile menu
  createMobileMenu();

  // Create filter drawer
  createFilterDrawer();

  // Create bottom sheet
  createBottomSheet();

  // Create bulk toolbar
  createBulkToolbar();

  // Create FAB buttons
  createFABButtons();

  // **ADD THIS LINE:**
  setupMobileControlPanel();

  // Setup scroll behavior
  setupMobileScroll();

  console.log('[Mobile] UI initialized');
}

// ================================
// MOBILE HEADER
// ================================
function createMobileHeader() {
  const header = document.querySelector('.header-full');
  if (!header) return;

  const navContainer = header.querySelector('.nav-container');
  if (!navContainer) return;

  navContainer.innerHTML = `
    <div class="mobile-header">
      <button class="hamburger-btn" id="mobile-menu-btn">
        <svg class="icon-menu" viewBox="0 0 24 24">
          <line x1="3" y1="12" x2="21" y2="12"></line>
          <line x1="3" y1="6" x2="21" y2="6"></line>
          <line x1="3" y1="18" x2="21" y2="18"></line>
        </svg>
      </button>
      <h1 style="font-size: 18px; font-weight: 600; margin: 0;">Manga Tracker</h1>
      <div style="width: 44px;"></div>
    </div>
  `;

  document.getElementById('mobile-menu-btn')?.addEventListener('click', toggleMobileMenu);
}

// ================================
// HAMBURGER MENU
// ================================
function createMobileMenu() {
  const existingMenu = document.getElementById('mobile-menu-overlay');
  if (existingMenu) return;

  const menuHTML = `
    <div class="mobile-menu-overlay" id="mobile-menu-overlay"></div>
    <div class="mobile-menu" id="mobile-menu">
      <div class="mobile-menu-header">
        <h2>Menu</h2>
        <button class="hamburger-btn" id="mobile-menu-close">
          <svg class="icon-close" viewBox="0 0 24 24">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <div class="mobile-menu-links">
        <a href="/dashboard">Dashboard</a>
        <a href="https://kenmei.co/discovery" target="_blank">
          Discovery
          <svg style="width:16px;height:16px;" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6M15 3h6v6M10 14L21 3"/>
          </svg>
        </a>
        <a href="/stats">Stats</a>
        <a href="/logs">Activity Log</a>
        <a href="/errors">
          Errors
          <span id="mobile-error-badge" style="display:none;background:#e53e3e;color:white;font-size:11px;font-weight:bold;padding:2px 6px;border-radius:10px;"></span>
        </a>
        <a href="/backups">Backups</a>
      </div>
    </div>
  `;

  document.body.insertAdjacentHTML('beforeend', menuHTML);

  document.getElementById('mobile-menu-overlay')?.addEventListener('click', closeMobileMenu);
  document.getElementById('mobile-menu-close')?.addEventListener('click', closeMobileMenu);
}

function toggleMobileMenu() {
  if (mobileState.menuOpen) {
    closeMobileMenu();
  } else {
    openMobileMenu();
  }
}

function openMobileMenu() {
  mobileState.menuOpen = true;
  document.getElementById('mobile-menu-overlay')?.classList.add('active');
  document.getElementById('mobile-menu')?.classList.add('active');
  document.body.style.overflow = 'hidden';
}

function closeMobileMenu() {
  mobileState.menuOpen = false;
  document.getElementById('mobile-menu-overlay')?.classList.remove('active');
  document.getElementById('mobile-menu')?.classList.remove('active');
	document.body.style.overflow = '';
	document.body.style.position = '';
	document.body.style.width = '';
	document.body.style.top = '';
	window.scrollTo(0, mobileState.scrollY || 0);

}

// ================================
// FILTER DRAWER
// ================================
function createFilterDrawer() {
  const existingDrawer = document.getElementById('filter-drawer-overlay');
  if (existingDrawer) return;

  const drawerHTML = `
    <div class="filter-drawer-overlay" id="filter-drawer-overlay"></div>
    <div class="filter-drawer" id="filter-drawer">
      <div class="filter-drawer-header">
        <h2 style="font-size:18px;font-weight:600;margin:0;">Filters</h2>
        <button class="hamburger-btn" id="filter-drawer-close">
          <svg class="icon-close" viewBox="0 0 24 24">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <div class="filter-drawer-content">
        
        <!-- Content Type -->
        <div class="filter-section">
          <h3>Content Type</h3>
          <div class="multi-select" id="mobile-filter-type-container">
            <button class="control-input multi-select-trigger" id="mobile-filter-type-trigger" style="width: 100%;">
              Content Type
            </button>
            <div class="multi-select-menu hidden">
              <label><input type="checkbox" value="manga"> Manga</label>
              <label><input type="checkbox" value="manhwa"> Manhwa</label>
              <label><input type="checkbox" value="manhua"> Manhua</label>
              <label><input type="checkbox" value="other"> Other</label>
              <button class="btn-select-all">Select All</button>
              <button class="btn-select-none">Clear</button>
            </div>
          </div>
        </div>

		<!-- Tags (Genres) -->
        <div class="filter-section">
          <h3>Tags</h3>
          <div class="multi-select" id="mobile-filter-genre-container">
            <button class="control-input multi-select-trigger" id="mobile-filter-genre-trigger" style="width: 100%;">
              Tags
            </button>
			<div class="multi-select-menu hidden" id="mobile-filter-genre-menu">
			<!-- ADDED: Mobile tags mode toggle -->
			<div class="tags-mode-toggle">
				<button class="btn-tags-mode" id="mobile-btn-tags-mode" data-mode="include">
				<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
					<path d="M20 6L9 17l-5-5"/>
				</svg>
				<span>Include Mode</span>
				</button>
			</div>
			
			<div class="combined-tags-list">
				<div class="genre-list-section"></div>
				<div style="border-top: 1px solid #334155; margin: 8px 0;"></div>
				<div class="rating-section">
				<label><input type="checkbox" value="safe" class="rating-checkbox"> Safe</label>
				<label><input type="checkbox" value="mild" class="rating-checkbox"> Suggestive</label>
				<label><input type="checkbox" value="mature" class="rating-checkbox"> Mature</label>
				<label><input type="checkbox" value="explicit" class="rating-checkbox"> Explicit</label>
				</div>
			</div>
			<button class="btn-select-none" id="mobile-btn-clear-all-tags">Clear</button>
			</div>
          </div>
        </div>

        <!-- Publication Status -->
        <div class="filter-section">
          <h3>Publication Status</h3>
          <div class="multi-select" id="mobile-filter-pub-status-container">
            <button class="control-input multi-select-trigger" id="mobile-filter-pub-status-trigger" style="width: 100%;">
              Publication Status
            </button>
            <div class="multi-select-menu hidden">
              <label><input type="checkbox" value="reading"> Reading</label>
              <label><input type="checkbox" value="completed"> Completed</label>
              <label><input type="checkbox" value="on_hold"> On Hold</label>
              <label><input type="checkbox" value="dropped"> Dropped</label>
              <button class="btn-select-all">Select All</button>
              <button class="btn-select-none">Clear</button>
            </div>
          </div>
        </div>

        <!-- Readable On -->
        <div class="filter-section">
          <h3>Readable On</h3>
          <div class="multi-select" id="mobile-filter-readable-on-container">
            <button class="control-input multi-select-trigger" id="mobile-filter-readable-on-trigger" style="width: 100%;">
              Readable On
            </button>
            <div class="multi-select-menu hidden">
              <label><input type="checkbox" value="mangadex"> MangaDex</label>
              <label><input type="checkbox" value="kagane"> Kagane</label>
              <button class="btn-select-all">Select All</button>
              <button class="btn-select-none">Clear</button>
            </div>
          </div>
        </div>

      </div>
      <div class="filter-drawer-footer">
        <button class="btn-reset-filters-mobile" id="mobile-reset-filters">Reset All Filters</button>
      </div>
    </div>
  `;

  document.body.insertAdjacentHTML('beforeend', drawerHTML);

  // Setup filter drawer events
  document.getElementById('filter-drawer-overlay')?.addEventListener('click', closeFilterDrawer);
  document.getElementById('filter-drawer-close')?.addEventListener('click', closeFilterDrawer);
  document.getElementById('mobile-reset-filters')?.addEventListener('click', resetMobileFilters);

  // Setup multi-select dropdowns (same as desktop)
  
  // Content Type
  const mobileTypeTrigger = document.getElementById('mobile-filter-type-trigger');
  const mobileTypeMenu = document.querySelector('#mobile-filter-type-container .multi-select-menu');
  const mobileTypeCheckboxes = mobileTypeMenu.querySelectorAll('input[type="checkbox"]');
  setupStaticMultiSelect(mobileTypeTrigger, mobileTypeMenu, mobileTypeCheckboxes, 'type', {
    'manga': 'Manga',
    'manhwa': 'Manhwa',
    'manhua': 'Manhua',
    'other': 'Other'
  }, 'Content Type');

  // Genres + Rating (Combined Tags)
  const mobileGenreTrigger = document.getElementById('mobile-filter-genre-trigger');
  const mobileGenreMenu = document.getElementById('mobile-filter-genre-menu');
  const mobileGenreListSection = mobileGenreMenu.querySelector('.genre-list-section');
  const mobileRatingCheckboxes = mobileGenreMenu.querySelectorAll('.rating-checkbox');
  const mobileClearAllBtn = document.getElementById('mobile-btn-clear-all-tags');

  // Close menu when clicking outside
  document.addEventListener('click', (e) => {
    if (!mobileGenreMenu.contains(e.target) && e.target !== mobileGenreTrigger) {
      mobileGenreMenu.classList.add('hidden');
    }
  });

  // Toggle menu on trigger click
  mobileGenreTrigger.addEventListener('click', (e) => {
    e.stopPropagation();
    mobileGenreMenu.classList.toggle('hidden');
    if (!mobileGenreMenu.classList.contains('hidden')) {
      closeAllMultiSelectMenus(mobileGenreMenu);
      const scrollContainer = mobileGenreMenu.querySelector('.combined-tags-list');
      if (scrollContainer) {
        scrollContainer.scrollTop = 0;
      }
    }
  });

	// Update trigger text based on both genres and ratings
	function updateMobileTagsTriggerText() {
		const genreCount = state.genre.length;
		const ratingCount = state.rating.length;
		const totalCount = genreCount + ratingCount;
		
		if (totalCount === 0) {
		mobileGenreTrigger.textContent = 'Tags';
		} else {
		mobileGenreTrigger.textContent = `${totalCount} Selected`;
		}
	}

	// Clear All button (clears both genres and ratings)
  mobileClearAllBtn.addEventListener('click', () => {
    state.genre = [];
    state.rating = [];
    state.page = 1;
    
    // Reset all checkboxes data-mode
    mobileGenreListSection.querySelectorAll('input[type="checkbox"]').forEach(cb => {
      delete cb.dataset.mode;
    });
    mobileRatingCheckboxes.forEach(cb => {
      delete cb.dataset.mode;
    });
    
    loadPage();
    updateMobileTagsTriggerText();
  });

	// Setup rating checkboxes (3-state toggle: disabled → include → exclude → disabled)
  mobileRatingCheckboxes.forEach(cb => {
    // Initialize state
    const existing = state.rating.find(r => r.name === cb.value);
    if (existing) {
      cb.dataset.mode = existing.mode;
    }
    
    // 3-state click handler
    cb.parentElement.addEventListener('click', (e) => {
      e.preventDefault();
      const current = state.rating.find(r => r.name === cb.value);
      
      // Remove current state
      state.rating = state.rating.filter(r => r.name !== cb.value);
      delete cb.dataset.mode;
      
      // Cycle: disabled → include → exclude → disabled
      if (!current) {
        // disabled → include
        state.rating.push({ name: cb.value, mode: 'include' });
        cb.dataset.mode = 'include';
      } else if (current.mode === 'include') {
        // include → exclude
        state.rating.push({ name: cb.value, mode: 'exclude' });
        cb.dataset.mode = 'exclude';
      }
      // else: exclude → disabled (already removed above)
      
      state.page = 1;
      loadPage();
      updateMobileTagsTriggerText();
    });
  });

// Load genres dynamically (mobile version)
  loadMobileGenres = async function() {
    try {
      const res = await fetch('/api/genres');
      if (res.ok) {
        const genres = await res.json();
        mobileGenreListSection.innerHTML = '';
        genres.forEach(genre => {
          const label = document.createElement('label');
          const cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.value = genre;
          
          // Initialize state
          const existing = state.genre.find(g => g.name === genre);
          if (existing) {
            cb.dataset.mode = existing.mode;
          }
          
          label.appendChild(cb);
          label.appendChild(document.createTextNode(genre));
          
          // 3-state click handler
          label.addEventListener('click', (e) => {
            e.preventDefault();
            const current = state.genre.find(g => g.name === genre);
            
            // Remove current state
            state.genre = state.genre.filter(g => g.name !== genre);
            delete cb.dataset.mode;
            
            // Cycle: disabled → include → exclude → disabled
            if (!current) {
              // disabled → include
              state.genre.push({ name: genre, mode: 'include' });
              cb.dataset.mode = 'include';
            } else if (current.mode === 'include') {
              // include → exclude
              state.genre.push({ name: genre, mode: 'exclude' });
              cb.dataset.mode = 'exclude';
            }
            // else: exclude → disabled (already removed above)
            
            state.page = 1;
            loadPage();
            updateMobileTagsTriggerText();
          });
          
          mobileGenreListSection.appendChild(label);
        });
      }
    } catch (e) {
      console.error('Failed to load genres:', e);
    }
  };

  loadMobileGenres();

  // Publication Status
  const mobilePubStatusTrigger = document.getElementById('mobile-filter-pub-status-trigger');
  const mobilePubStatusMenu = document.querySelector('#mobile-filter-pub-status-container .multi-select-menu');
  const mobilePubStatusCheckboxes = mobilePubStatusMenu.querySelectorAll('input[type="checkbox"]');
  setupStaticMultiSelect(mobilePubStatusTrigger, mobilePubStatusMenu, mobilePubStatusCheckboxes, 'pubStatus', {
    'reading': 'Reading',
    'completed': 'Completed',
    'on_hold': 'On Hold',
    'dropped': 'Dropped',
    'plan_to_read': 'Plan to Read'
  }, 'Publication Status');

  // Readable On
  const mobileReadableOnTrigger = document.getElementById('mobile-filter-readable-on-trigger');
  const mobileReadableOnMenu = document.querySelector('#mobile-filter-readable-on-container .multi-select-menu');
  const mobileReadableOnCheckboxes = mobileReadableOnMenu.querySelectorAll('input[type="checkbox"]');
  setupStaticMultiSelect(mobileReadableOnTrigger, mobileReadableOnMenu, mobileReadableOnCheckboxes, 'readableOn', {
    'mangadex': 'MangaDex',
    'kagane': 'Kagane'
  }, 'Readable On');
}

function setupMobileControlPanel() {
  if (!isMobileDevice()) return;

  const row2 = document.querySelector('.control-row:nth-child(2)');
  if (!row2) return;

  // Check if mobile row already exists
  if (document.querySelector('.mobile-control-row-2')) return;

  // Create mobile row 2 container
  const mobileRow2 = document.createElement('div');
  mobileRow2.className = 'mobile-control-row-2';
  mobileRow2.style.cssText = 'display: flex; gap: 8px; width: 100%;';

  // Filter button
  const filterBtn = document.createElement('button');
  filterBtn.id = 'mobile-filter-btn';
  filterBtn.className = 'control-button square-button';
  filterBtn.innerHTML = `
    <svg class="icon-filter" viewBox="0 0 24 24" style="width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2;">
      <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>
    </svg>
  `;
  filterBtn.addEventListener('click', openFilterDrawer);

  // Sort direction button (clone from row 1)
  const sortDirBtn = document.createElement('button');
  sortDirBtn.id = 'mobile-sort-direction';
  sortDirBtn.className = 'control-button square-button';
  sortDirBtn.innerHTML = `
    <svg width="16" height="16" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="2.5">
      ${SORT_ICONS[state.dir]}
    </svg>
  `;
  sortDirBtn.addEventListener('click', () => {
    state.dir = state.dir === 'asc' ? 'desc' : 'asc';
    sortDirBtn.querySelector('svg').innerHTML = SORT_ICONS[state.dir];
    loadPage();
  });

	// Search input
	const searchContainer = document.createElement('div');
	searchContainer.className = 'control-search';
	searchContainer.style.flex = '1';
	searchContainer.innerHTML = `
	<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
		<circle cx="11" cy="11" r="8"></circle>
		<path d="m21 21-4.35-4.35"></path>
	</svg>
	<input type="text" id="mobile-search-input" placeholder="Search..." />
	<!-- ADDED: Clear button for mobile search -->
	<button class="search-clear-btn" id="mobile-search-clear-btn" title="Clear search">
		<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" class="h-5 w-5 text-gray-400 hover_text-gray-500"><path fill-rule="evenodd" d="M4.293 4.293a1 1 0 0 1 1.414 0L10 8.586l4.293-4.293a1 1 0 1 1 1.414 1.414L11.414 10l4.293 4.293a1 1 0 0 1-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 0 1-1.414-1.414L8.586 10 4.293 5.707a1 1 0 0 1 0-1.414" clip-rule="evenodd"/></svg>
	</button>
	`;

	// Setup search functionality
	const searchInput = searchContainer.querySelector('input');
	const mobileClearBtn = searchContainer.querySelector('.search-clear-btn'); // ADDED
	let searchTimeout;

	searchInput.addEventListener('input', () => {
	clearTimeout(searchTimeout);
	searchTimeout = setTimeout(() => {
		state.page = 1;
		loadPage();
	}, 300);
	
	// ADDED: Show/hide clear button
	if (searchInput.value.trim()) {
		mobileClearBtn?.classList.add('show');
	} else {
		mobileClearBtn?.classList.remove('show');
	}
	});

	// ADDED: Mobile clear button handler
	if (mobileClearBtn) {
	mobileClearBtn.addEventListener('click', () => {
		searchInput.value = '';
		mobileClearBtn.classList.remove('show');
		
		// ADDED: Also clear desktop search to keep them synced
		const desktopSearch = document.getElementById('search-input');
		if (desktopSearch) {
		desktopSearch.value = '';
		document.getElementById('search-clear-btn')?.classList.remove('show');
		}
		
		state.page = 1;
		loadPage();
	});
	}

  // Add elements to mobile row
  mobileRow2.appendChild(filterBtn);
  mobileRow2.appendChild(sortDirBtn);
  mobileRow2.appendChild(searchContainer);

  // Insert mobile row at the start of row 2
  row2.insertBefore(mobileRow2, row2.firstChild);

  console.log('[Mobile] Control panel configured');
}

// Add this to initMobile() function:
// setupMobileControlPanel();

// Also update the sort icon when state changes
function updateMobileSortIcon() {
  const mobileSortBtn = document.getElementById('mobile-sort-direction');
  if (mobileSortBtn && isMobileDevice()) {
    mobileSortBtn.querySelector('svg').innerHTML = SORT_ICONS[state.dir];
  }
}


async function loadMobileGenres() {
  try {
    const res = await fetch('/api/genres');
    if (res.ok) {
      const genres = await res.json();
      const dropdown = document.getElementById('mobile-tags');
      if (dropdown) {
        // Add genres as options
        genres.forEach(genre => {
          const option = document.createElement('option');
          option.value = genre;
          option.textContent = genre;
          dropdown.appendChild(option);
        });
      }
    }
  } catch (e) {
    console.error('Failed to load genres:', e);
  }
}

function syncMobileFilters(type) {
  // Not needed anymore - dropdowns handle this directly in their change events
}

function openFilterDrawer() {
  mobileState.filterDrawerOpen = true;
  document.getElementById('filter-drawer-overlay')?.classList.add('active');
  document.getElementById('filter-drawer')?.classList.add('active');
  document.body.style.overflow = 'hidden';

  // Sync dropdowns with current state
  const typeDropdown = document.getElementById('mobile-content-type');
  if (typeDropdown) {
    typeDropdown.value = state.type.length > 0 ? state.type[0] : '';
  }

  const tagsDropdown = document.getElementById('mobile-tags');
  if (tagsDropdown) {
    tagsDropdown.value = state.genre.length > 0 ? state.genre[0] : '';
  }

  const ratingDropdown = document.getElementById('mobile-rating');
  if (ratingDropdown) {
    ratingDropdown.value = state.rating.length > 0 ? state.rating[0] : '';
  }

  const pubStatusDropdown = document.getElementById('mobile-pub-status');
  if (pubStatusDropdown) {
    pubStatusDropdown.value = state.pubStatus.length > 0 ? state.pubStatus[0] : '';
  }

  const readableOnDropdown = document.getElementById('mobile-readable-on');
  if (readableOnDropdown) {
    readableOnDropdown.value = state.readableOn.length > 0 ? state.readableOn[0] : '';
  }
}

function closeFilterDrawer() {
  mobileState.filterDrawerOpen = false;
  document.getElementById('filter-drawer-overlay')?.classList.remove('active');
  document.getElementById('filter-drawer')?.classList.remove('active');
	document.body.style.overflow = '';
	document.body.style.position = '';
	document.body.style.width = '';
	document.body.style.top = '';
	window.scrollTo(0, mobileState.scrollY || 0);
}

function resetMobileFilters() {
  // Reset ALL state (including Status and Sort from control panel)
  state.status = 'reading';
  state.sort = 'unread_first';
  state.dir = 'asc';
  state.type = [];
  state.genre = [];
  state.rating = [];
  state.pubStatus = [];
  state.readableOn = [];
  state.tagsMode = 'include';  // ADDED: Reset tags mode
  state.page = 1;
  
  // ADDED: Reset desktop tags mode button
  const tagsModeBtn = document.getElementById('btn-tags-mode');
  if (tagsModeBtn) {
    tagsModeBtn.dataset.mode = 'include';
    const icon = tagsModeBtn.querySelector('svg');
    const text = tagsModeBtn.querySelector('span');
    icon.innerHTML = '<path d="M20 6L9 17l-5-5"/>';
    text.textContent = 'Include Mode';
  }
  
  // ADDED: Reset mobile tags mode button
  const mobileTagsBtn = document.getElementById('mobile-btn-tags-mode');  // CHANGED: Different variable name
  if (mobileTagsBtn) {
    mobileTagsBtn.dataset.mode = 'include';
    const mobileIcon = mobileTagsBtn.querySelector('svg');
    const mobileText = mobileTagsBtn.querySelector('span');
    mobileIcon.innerHTML = '<path d="M20 6L9 17l-5-5"/>';
    mobileText.textContent = 'Include Mode';
  }
  
  // Reset ALL multi-select checkboxes in mobile drawer
  document.querySelectorAll(`
    #mobile-filter-type-container input[type="checkbox"],
    #mobile-filter-genre-container input[type="checkbox"],
    #mobile-filter-genre-container .rating-checkbox,
    #mobile-filter-pub-status-container input[type="checkbox"],
    #mobile-filter-readable-on-container input[type="checkbox"]
  `).forEach(cb => cb.checked = false);
  
  // Reset trigger texts
  document.getElementById('mobile-filter-type-trigger').textContent = 'Content Type';
  document.getElementById('mobile-filter-genre-trigger').textContent = 'Tags';
  document.getElementById('mobile-filter-pub-status-trigger').textContent = 'Publication Status';
  document.getElementById('mobile-filter-readable-on-trigger').textContent = 'Readable On';
  
  // Reset desktop search if it exists
  const desktopSearch = document.getElementById('search-input');
  const mobileSearch = document.getElementById('mobile-search-input');
  if (desktopSearch) desktopSearch.value = '';
  if (mobileSearch) mobileSearch.value = '';
  
  // Update desktop UI elements
  const statusTrigger = document.getElementById('filter-status-trigger');
  const sortTrigger = document.getElementById('sort-order-trigger');
  if (statusTrigger) statusTrigger.textContent = 'Reading';
  if (sortTrigger) sortTrigger.textContent = 'Unread First';
  
  // Update sort direction icon
  const mobileSortBtn = document.getElementById('mobile-sort-direction');
  if (mobileSortBtn) {
    mobileSortBtn.querySelector('svg').innerHTML = SORT_ICONS['asc'];
  }
  
  // Update desktop status/sort selected states
  document.querySelectorAll('.single-select-menu .option-item').forEach(opt => {
    opt.classList.remove('selected');
    if ((opt.dataset.value === 'reading' && opt.closest('#filter-status-container')) ||
        (opt.dataset.value === 'unread_first' && opt.closest('#sort-order-container'))) {
      opt.classList.add('selected');
    }
  });
  
  // Close all menus
  document.querySelectorAll('.multi-select-menu, .single-select-menu').forEach(menu => {
    menu.classList.add('hidden');
  });
  
  loadPage();
  closeFilterDrawer();
}

// ================================
// BOTTOM SHEET
// ================================
function createBottomSheet() {
  const existingSheet = document.getElementById('bottom-sheet-overlay');
  if (existingSheet) return;

  const sheetHTML = `
    <div class="bottom-sheet-overlay" id="bottom-sheet-overlay"></div>
    <div class="bottom-sheet" id="bottom-sheet">
      <div class="bottom-sheet-handle"></div>
      <div class="bottom-sheet-content">
		<div class="bottom-sheet-header">
		<h2 id="sheet-title"></h2>
		<div class="settings-wrapper">
			<button class="btn-sheet-settings" id="sheet-settings-btn">
				<svg class="icon-settings" viewBox="0 0 24 24">
				<circle cx="12" cy="12" r="1"></circle>
				<circle cx="12" cy="5" r="1"></circle>
				<circle cx="12" cy="19" r="1"></circle>
				</svg>
			</button>
			<div class="sheet-settings-menu" id="sheet-settings-menu">
			<button class="sheet-settings-option" data-action="edit">
				<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
				<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
				<path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"/>
				</svg>
				Edit
			</button>
			
			<button class="sheet-settings-option" data-action="settings">
				<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 48 48">
				<path fill="currentColor" d="M24 4c-1.577 0-3.097.2-4.549.537a1.5 1.5 0 0 0-1.15 1.299l-.319 2.902a2.997 2.997 0 0 1-4.189 2.418h-.002l-2.666-1.174a1.5 1.5 0 0 0-1.7.348 20 20 0 0 0-4.566 7.871 1.5 1.5 0 0 0 .55 1.645l2.364 1.734a3 3 0 0 1 0 4.84l-2.365 1.732a1.5 1.5 0 0 0-.549 1.645 19.96 19.96 0 0 0 4.567 7.873 1.5 1.5 0 0 0 1.699.346l2.666-1.174a3 3 0 0 1 4.191 2.42l.319 2.902a1.5 1.5 0 0 0 1.148 1.297C20.901 43.8 22.423 44 24 44s3.097-.2 4.549-.537a1.5 1.5 0 0 0 1.15-1.299l.319-2.902a3 3 0 0 1 4.19-2.42l2.667 1.174a1.5 1.5 0 0 0 1.7-.346 20 20 0 0 0 4.566-7.873 1.5 1.5 0 0 0-.55-1.645l-2.364-1.732A3 3 0 0 1 39 24c0-.958.454-1.853 1.227-2.42l2.365-1.732a1.5 1.5 0 0 0 .549-1.645 20 20 0 0 0-4.567-7.873 1.5 1.5 0 0 0-1.699-.346l-2.668 1.174a3 3 0 0 1-4.19-2.42L29.7 5.836a1.5 1.5 0 0 0-1.148-1.297A20 20 0 0 0 24 4m0 3c.974 0 1.91.175 2.848.34l.187 1.724a6.003 6.003 0 0 0 8.38 4.84l1.587-.697a16.9 16.9 0 0 1 2.855 4.924l-1.406 1.031A6 6 0 0 0 36 24c0 1.91.912 3.708 2.451 4.838l1.406 1.031a16.9 16.9 0 0 1-2.855 4.924l-1.586-.697a6.003 6.003 0 0 0-8.38 4.84l-.188 1.724c-.938.165-1.874.34-2.848.34s-1.91-.175-2.848-.34l-.187-1.724a6.003 6.003 0 0 0-8.38-4.84l-1.587.697a16.9 16.9 0 0 1-2.855-4.924l1.406-1.031a6.003 6.003 0 0 0 0-9.678l-1.406-1.03A16.9 16.9 0 0 1 11 13.205l1.584.697a6.002 6.002 0 0 0 8.38-4.838l.188-1.724C22.09 7.175 23.026 7 24 7m0 9c-4.4 0-8 3.6-8 8s3.6 8 8 8 8-3.6 8-8-3.6-8-8-8m0 3c2.78 0 5 2.22 5 5s-2.22 5-5 5-5-2.22-5-5 2.22-5 5-5"/>
				</svg>
				Settings
			</button>
			
			<button class="sheet-settings-option" data-action="go-to-source">
				<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
				<path d="M15 3h6v6M10 14L21 3M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/>
				</svg>
				Go to Source
			</button>
			
			<button class="sheet-settings-option" data-action="copy-name">
				<svg width="20" height="20" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="currentColor">
				<path d="M2.5 1C1.676 1 1 1.676 1 2.5v8c0 .824.676 1.5 1.5 1.5H4v.5c0 .824.676 1.5 1.5 1.5h8c.824 0 1.5-.676 1.5-1.5v-8c0-.824-.676-1.5-1.5-1.5H12v-.5c0-.824-.676-1.5-1.5-1.5Zm0 1h8c.281 0 .5.219.5.5v8c0 .281-.219.5-.5.5h-8a.494.494 0 0 1-.5-.5v-8c0-.281.219-.5.5-.5M12 4h1.5c.281 0 .5.219.5.5v8c0 .281-.219.5-.5.5h-8a.494.494 0 0 1-.5-.5V12h5.5c.824 0 1.5-.676 1.5-1.5Z" transform="translate(.56 1.275)scale(1.43)"/>
				</svg>
				Copy Name
			</button>
			
			<button class="sheet-settings-option danger" data-action="delete">
				<svg width="20" height="20" viewBox="0 0 64 64" stroke-width="3" stroke="currentColor" fill="none">
				<path d="M45.49,54.87h-27a1,1,0,0,1-1-1l-2-36H48.46l-2,36A1,1,0,0,1,45.49,54.87Z"/>
				<path d="M51,17.86H13c-.28,0-.5-.16-.5-.35l.93-4.35a.49.49,0,0,1,.5-.3H50.07a.49.49,0,0,1,.5.3l.93,4.35C51.5,17.7,51.28,17.86,51,17.86Z"/>
				<line x1="24" y1="23.44" x2="24" y2="48.44"/>
				<line x1="32" y1="23.44" x2="32" y2="48.44"/>
				<line x1="40" y1="23.44" x2="40" y2="48.44"/>
				<path d="M25.73,12.86V7.57a1,1,0,0,1,1-1H37.27a1,1,0,0,1,1,1v5.29"/>
				</svg>
				Delete
			</button>
			</div>
		</div>
		</div>
        
		<div class="sheet-progress-section">
		<div class="sheet-chapter-row">
			<div class="sheet-chapter-value">
			<button class="btn-chapter-adjust" id="sheet-chapter-minus">
				<svg viewBox="0 0 24 24" style="width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:2;">
				<line x1="5" y1="12" x2="19" y2="12"></line>
				</svg>
			</button>
			<span id="sheet-current-chapter">Ch.45</span>
			<span style="color:#64748b;margin:0 4px;">/</span>
			<span id="sheet-latest-chapter">Ch.50</span>
			<button class="btn-chapter-adjust" id="sheet-chapter-plus">
				<svg viewBox="0 0 24 24" style="width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:2;">
				<line x1="12" y1="5" x2="12" y2="19"></line>
				<line x1="5" y1="12" x2="19" y2="12"></line>
				</svg>
			</button>
			</div>
		</div>
          
          <div class="sheet-progress-bar">
            <div class="sheet-progress-fill" id="sheet-progress-fill"></div>
          </div>
          
          <div class="sheet-metadata">
            <span id="sheet-updated">Updated: 2d ago</span>
            <span id="sheet-behind">5 behind</span>
          </div>
        </div>
        
        <div class="sheet-actions">
          <button class="btn-sheet-action secondary" id="sheet-search-btn">
            Search Ch.<span id="sheet-search-chapter">46</span>
          </button>
          <button class="btn-sheet-action primary" id="sheet-continue-btn">
            Continue to Ch.<span id="sheet-search-chapter">46</span>
          </button>
        </div>
      </div>
    </div>
  `;

  document.body.insertAdjacentHTML('beforeend', sheetHTML);

	document.getElementById('bottom-sheet-overlay')?.addEventListener('click', (e) => {
	e.stopPropagation();
	closeBottomSheet();
	});
	
	// CHANGED: Toggle settings menu instead of opening edit modal
	document.getElementById('sheet-settings-btn')?.addEventListener('click', (e) => {
	e.stopPropagation();
	const menu = document.getElementById('sheet-settings-menu');
	menu?.classList.toggle('active');
	});

	// ADDED: Close settings menu when clicking outside
	document.addEventListener('click', (e) => {
	const menu = document.getElementById('sheet-settings-menu');
	const settingsBtn = document.getElementById('sheet-settings-btn');
	
	if (menu && !menu.contains(e.target) && e.target !== settingsBtn && !settingsBtn?.contains(e.target)) {
		menu.classList.remove('active');
	}
	});

	// ADDED: Settings menu actions
	document.addEventListener('click', async (e) => {
	const option = e.target.closest('.sheet-settings-option');
	if (!option || option.classList.contains('disabled')) return;

	const action = option.dataset.action;
	const series = mobileState.currentSeries;
	if (!series) return;

	// CHANGED: Only close menu for certain actions (not copy-name)
	if (action !== 'copy-name') {
	document.getElementById('sheet-settings-menu')?.classList.remove('active');
	}

	switch (action) {		
		case 'edit':
		// ADDED: Open mobile edit modal
		openMobileEditModal(series);
		closeBottomSheet();
		break;
		
		case 'settings':
		// CHANGED: Open settings WITHOUT closing bottom sheet first
		openMobileSettingsModal(series);
		// THEN close bottom sheet WITHOUT unlocking scroll
		const bottomSheet = document.getElementById('bottom-sheet');
		const overlay = document.getElementById('bottom-sheet-overlay');
		bottomSheet?.classList.remove('active');
		overlay?.classList.remove('active');
		bottomSheet.style.transform = '';
		mobileState.bottomSheetOpen = false;
		// DON'T restore scroll - keep it locked for Settings modal
		break;

		case 'go-to-source':		// CHANGED: Use pre-fetched URL
		const sourceUrl = mobileState.primarySourceUrl || series.source_url;
		window.open(sourceUrl, '_blank');
		break;

		case 'copy-name':
		const option = e.target.closest('.sheet-settings-option');
		const originalHTML = option.innerHTML;

		// CHANGED: Try multiple copy methods with fallback
		let copySuccess = false;

		// Method 1: Modern Clipboard API
		if (navigator.clipboard && navigator.clipboard.writeText) {
		try {
			await navigator.clipboard.writeText(series.title);
			copySuccess = true;
		} catch (err) {
			console.log('Clipboard API failed, trying fallback:', err);
		}
		}

		// Method 2: Fallback for older browsers/mobile
		if (!copySuccess) {
		try {
			const textArea = document.createElement('textarea');
			textArea.value = series.title;
			textArea.style.position = 'fixed';
			textArea.style.left = '-999999px';
			textArea.style.top = '-999999px';
			document.body.appendChild(textArea);
			textArea.focus();
			textArea.select();
			
			const successful = document.execCommand('copy');
			document.body.removeChild(textArea);
			
			if (successful) {
			copySuccess = true;
			}
		} catch (err) {
			console.error('Fallback copy failed:', err);
		}
		}

		if (copySuccess) {
		option.innerHTML = `
		<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<path d="M20 6L9 17l-5-5"/>
		</svg>
		Copied!
		`;

		setTimeout(() => {
			option.innerHTML = originalHTML;
		}, 1500);
		} else {
		alert('Failed to copy to clipboard');
		}

		// ADDED: Prevent menu from closing by stopping event propagation
		e.stopPropagation();
		return; // ADDED: Exit early without closing menu
		break;
		
		case 'delete':
		if (confirm(`Delete "${series.title}"? This action cannot be undone.`)) {
			try {
			const res = await fetch(`/api/series/${series.id}`, {
				method: 'DELETE'
			});
			
			if (res.ok) {
				// ADDED: Show notification
				showNotification(`${series.title} deleted`, 'delete');
				
				closeBottomSheet();
				loadPage();
			if (typeof loadGenres === 'function') {
					loadGenres();
				}
			} else {
				showNotification('Failed to delete series', 'error'); // CHANGED
			}
			} catch (err) {
			console.error('Delete error:', err);
			showNotification('Error: ' + err.message, 'error'); 
			}
		}
		break;
	}
	});

  // Add swipe-down gesture with velocity detection
  let touchStartY = 0;
  let touchStartTime = 0;
  let lastTouchY = 0;
  let lastTouchTime = 0;
  let currentTranslateY = 0;
  let isDragging = false;
  const sheet = document.getElementById('bottom-sheet');
  
  sheet?.addEventListener('touchstart', (e) => {
    // Don't drag if touching a button
    if (e.target.closest('button')) {
      return;
    }
    
    // Allow dragging from anywhere in the sheet
    touchStartY = e.touches[0].clientY;
    touchStartTime = Date.now();
    lastTouchY = touchStartY;
    lastTouchTime = touchStartTime;
    currentTranslateY = 0;
    isDragging = true;
    sheet.style.transition = 'none'; // Disable transition during drag
  }, { passive: true });

  sheet?.addEventListener('touchmove', (e) => {
    if (!isDragging) return;
    
    const touchY = e.touches[0].clientY;
    const now = Date.now();
    const diff = touchY - touchStartY;
    
    // Track for velocity calculation
    lastTouchY = touchY;
    lastTouchTime = now;
    
    // Only allow downward movement
    if (diff > 0) {
      currentTranslateY = diff;
      sheet.style.transform = `translateY(${diff}px)`;
      
      // Add resistance effect when pulling far
      if (diff > 200) {
        const resistance = 200 + (diff - 200) * 0.3;
        sheet.style.transform = `translateY(${resistance}px)`;
      }
    }
  }, { passive: true });

  sheet?.addEventListener('touchend', (e) => {
    if (!isDragging) return;
    
    isDragging = false;
    sheet.style.transition = ''; // Re-enable transition
    
    // Calculate velocity (pixels per millisecond)
    const timeDiff = Date.now() - lastTouchTime;
    const distance = lastTouchY - touchStartY;
    const velocity = timeDiff > 0 ? distance / timeDiff : 0;
    
    // Close if:
    // 1. Dragged more than 100px, OR
    // 2. Fast swipe down (velocity > 0.5 px/ms) with at least 30px movement
    const shouldClose = currentTranslateY > 100 || (velocity > 0.5 && distance > 30);
    
    if (shouldClose) {
      closeBottomSheet();
    } else {
      // Snap back to original position
      sheet.style.transform = '';
    }
    
    currentTranslateY = 0;
  }, { passive: true });
  
  sheet?.addEventListener('touchcancel', (e) => {
    if (!isDragging) return;
    
    isDragging = false;
    sheet.style.transition = '';
    sheet.style.transform = '';
    currentTranslateY = 0;
  }, { passive: true });

}

function openBottomSheet(series) {
  mobileState.bottomSheetOpen = true;
  mobileState.currentSeries = series;
  mobileState.pendingChapter = series.current_chapter;
  
  // ADDED: Fetch and store primary source URL
  fetch(`/api/series/${series.id}/sources`)
    .then(res => res.json())
    .then(data => {
      const primarySource = data.sources.find(s => s.is_primary);
      mobileState.primarySourceUrl = primarySource ? primarySource.source_url : series.source_url;
    })
    .catch(err => {
      console.error('Failed to get sources:', err);
      mobileState.primarySourceUrl = series.source_url;
    });

  // ADDED: Lock scrolling - MORE AGGRESSIVE (same as bulk edit menu)
  if (!mobileState.scrollY) { // CHANGED: Only save if not already saved
    mobileState.scrollY = window.scrollY;
  }
  document.body.style.overflow = 'hidden';
  document.body.style.position = 'fixed';
  document.body.style.width = '100%';
  document.body.style.top = `-${mobileState.scrollY}px`;
  document.getElementById('sheet-title').textContent = series.title;

  document.getElementById('sheet-current-chapter').textContent = 
  series.current_chapter === -1 ? 'Not started' : `Ch.${series.current_chapter}`;

  // ADDED: Set latest chapter
  document.getElementById('sheet-latest-chapter').textContent = 
  series.latest_chapter ? `Ch.${series.latest_chapter}` : 'Ch.?';
  
	// CHANGED: Format time as "X time ago" with proper pluralization
	const timeAgo = formatTimeAgo(series.latest_release);
	document.getElementById('sheet-updated').textContent = `Updated: ${timeAgo}`;

	// FIXED: Calculate behind count with proper rounding for floating-point precision
	const behindElement = document.getElementById('sheet-behind');
	let behindCount = 0;

	if (series.current_chapter === -1) {
	behindCount = series.latest_chapter || 0;
	} else {
	// Round to 1 decimal place to fix floating-point errors
	const rawDiff = (series.latest_chapter || 0) - series.current_chapter;
	behindCount = Math.round(rawDiff * 10) / 10;
	}

	if (behindCount === 0 && series.current_chapter !== -1) {
	// Show green "Up to date" with checkmark
	behindElement.innerHTML = `
		<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#4ade80" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:middle;margin-right:4px;">
		<path d="M20 6 9 17l-5-5"/>
		</svg>
		<span style="color:#4ade80;font-weight:500;">Up to date</span>
	`;
	} else {
	// Format the number nicely (remove .0 if it's a whole number)
	const displayCount = behindCount % 1 === 0 ? Math.floor(behindCount) : behindCount;
	const chapterWord = behindCount === 1 ? 'chapter' : 'chapters';
	behindElement.textContent = `${displayCount} ${chapterWord} behind`;
	}


  const progress = series.current_chapter === -1 ? 0 : (series.current_chapter / series.latest_chapter) * 100;
  const progressFill = document.getElementById('sheet-progress-fill');
  progressFill.style.width = `${Math.min(progress, 100)}%`;

  // ADDED: Change color to green when caught up
  if (series.unread_count === 0 && series.current_chapter !== -1) {
    progressFill.style.background = '#4ade80';
  } else {
    progressFill.style.background = '#3b82f6';
  }

  // FIXED: Calculate nextChapter BEFORE passing to setupBottomSheetButtons
  const nextChapter = series.current_chapter === -1 ? 1 : series.current_chapter + 1;
  document.getElementById('sheet-search-chapter').textContent = nextChapter;

  // FIXED: Now pass the calculated nextChapter value
  setupBottomSheetButtons(series, nextChapter);

  document.getElementById('bottom-sheet-overlay')?.classList.add('active');
  document.getElementById('bottom-sheet')?.classList.add('active');
  document.body.style.overflow = 'hidden';
}

function setupBottomSheetButtons(series, nextChapter) {
  // Search button - opens Google search for next chapter
	const searchBtn = document.getElementById('sheet-search-btn');
	if (searchBtn) {
	// Remove old listeners by cloning
	const newSearchBtn = searchBtn.cloneNode(true);
	searchBtn.parentNode.replaceChild(newSearchBtn, searchBtn);
	
	// Always show search button with appropriate chapter number
	newSearchBtn.style.display = 'flex';
	const searchChapterSpan = newSearchBtn.querySelector('#sheet-search-chapter');
	if (searchChapterSpan) {
		searchChapterSpan.textContent = nextChapter || 1;
	}
	
	newSearchBtn.addEventListener('click', () => {
		const chapterNum = searchChapterSpan ? searchChapterSpan.textContent : (nextChapter || 1);
		const query = encodeURIComponent(`${series.title} chapter ${chapterNum}`);
		window.open(`https://www.google.com/search?q=${query}`, '_blank');
	});
	}

  // Continue button - opens next chapter URL or first chapter if not started
  const continueBtn = document.getElementById('sheet-continue-btn');
  if (continueBtn) {
    // Remove old listeners by cloning
    const newContinueBtn = continueBtn.cloneNode(true);
    continueBtn.parentNode.replaceChild(newContinueBtn, continueBtn);
    
    // Fetch chapters to get the correct URL
    fetch(`/api/series/${series.id}/chapters`)
      .then(r => r.json())
      .then(chapters => {
        if (chapters.length === 0) {
          newContinueBtn.textContent = 'No chapters';
          newContinueBtn.disabled = true;
          // ADDED: Search button still works for chapter 1
          if (searchBtn) {
            const searchChapterSpan = searchBtn.querySelector('#sheet-search-chapter');
            if (searchChapterSpan) {
              searchChapterSpan.textContent = '1';
            }
          }
          return;
        }

        // Sort chapters (reuse existing logic)
        const hasAnyNullVolume = chapters.some(ch => ch.volume == null || ch.volume === '');
        const useVolume = !hasAnyNullVolume;
        const comparator = useVolume ? compareChapters : (a, b) => a.chapter_number - b.chapter_number;
        const sorted = [...chapters].sort(comparator);

        let targetChapter = null;

        if (series.current_chapter === -1) {
          // Not started - go to first chapter
          targetChapter = sorted[0];
          newContinueBtn.textContent = `Continue to Ch.${targetChapter.chapter_number}`;
        } else {
          // Find current chapter index
          const currentIndex = sorted.findIndex(ch => ch.chapter_number === series.current_chapter);
          
          if (currentIndex >= 0 && currentIndex < sorted.length - 1) {
            // Go to next chapter
            targetChapter = sorted[currentIndex + 1];
            newContinueBtn.textContent = `Continue to Ch.${targetChapter.chapter_number}`;
          } else {
            // Already caught up
            newContinueBtn.textContent = 'All caught up';
            newContinueBtn.disabled = true;
          }
        }

        if (targetChapter) {
          newContinueBtn.disabled = false;
          newContinueBtn.addEventListener('click', () => {
            window.open(targetChapter.chapter_url, '_blank');
          });
        }
      })
      .catch(err => {
        console.error('Failed to load chapters:', err);
        newContinueBtn.textContent = 'Error loading';
        newContinueBtn.disabled = true;
      });
  }

// Hold-to-repeat variables
let holdInterval = null;
let holdTimeout = null;

function startHoldRepeat(callback, initialDelay = 300, repeatInterval = 50) {
  // Clear any existing intervals
  if (holdInterval) clearInterval(holdInterval);
  if (holdTimeout) clearTimeout(holdTimeout);
  
  // Execute immediately on first press
  callback();
  
  // Wait for initial delay, then start repeating
  holdTimeout = setTimeout(() => {
    holdInterval = setInterval(callback, repeatInterval);
  }, initialDelay);
}

function stopHoldRepeat() {
  if (holdInterval) {
    clearInterval(holdInterval);
    holdInterval = null;
  }
  if (holdTimeout) {
    clearTimeout(holdTimeout);
    holdTimeout = null;
  }
}

	// Fetch and store chapters for navigation
	let sortedChapters = [];
	let currentChapterIndex = -1;

	fetch(`/api/series/${series.id}/chapters`)
	.then(r => r.json())
	.then(chapters => {
		// Sort chapters using same logic as desktop
		const hasAnyNullVolume = chapters.some(ch => ch.volume == null || ch.volume === '');
		const useVolume = !hasAnyNullVolume;
		const comparator = useVolume ? compareChapters : (a, b) => a.chapter_number - b.chapter_number;
		sortedChapters = [...chapters].sort(comparator);
		
		// Find current chapter index
		if (mobileState.pendingChapter === -1) {
		currentChapterIndex = -1;
		} else {
		const matches = sortedChapters
			.map((ch, idx) => ({ ch, idx }))
			.filter(item => item.ch.chapter_number === mobileState.pendingChapter);
		
		if (matches.length === 0) {
			currentChapterIndex = -1;
		} else if (matches.length === 1) {
			currentChapterIndex = matches[0].idx;
		} else {
			// Multiple matches - pick highest volume if using volumes
			if (useVolume) {
			const best = matches.reduce((a, b) => {
				const volA = a.ch.volume ? parseFloat(a.ch.volume) || 0 : 0;
				const volB = b.ch.volume ? parseFloat(b.ch.volume) || 0 : 0;
				return volB > volA ? b : a;
			});
			currentChapterIndex = best.idx;
			} else {
			currentChapterIndex = matches[0].idx;
			}
		}
		}
		
		// Update chapter display format
		updateChapterDisplay();
	})
	.catch(err => {
		console.error('Failed to load chapters:', err);
	});

	function updateChapterDisplay() {
	const currentChapterEl = document.getElementById('sheet-current-chapter');
	if (currentChapterIndex === -1 || sortedChapters.length === 0) {
		currentChapterEl.textContent = 'Not started';
	} else {
		const ch = sortedChapters[currentChapterIndex];
		if (ch.is_oneshot) {
		currentChapterEl.textContent = 'Oneshot';
		} else if (ch.volume) {
		currentChapterEl.textContent = `Vol.${ch.volume} Ch.${ch.chapter_number}`;
		} else {
		currentChapterEl.textContent = `Ch.${ch.chapter_number}`;
		}
	}
	
	// ADDED: Update Search and Continue buttons
	updateSheetButtons();
	}

	function updateSheetButtons() {
	const searchBtn = document.getElementById('sheet-search-btn');
	const continueBtn = document.getElementById('sheet-continue-btn');
	const searchChapterSpan = document.getElementById('sheet-search-chapter');
	
	// Determine next chapter to read
	let nextChapterNumber = null;
	let nextChapterUrl = null;
	
	if (sortedChapters.length === 0) {
		// No chapters available
		if (continueBtn) {
		continueBtn.textContent = 'No chapters';
		continueBtn.disabled = true;
		}
		// CHANGED: Always show search button, default to chapter 1
		if (searchBtn && searchChapterSpan) {
		searchBtn.style.display = 'flex';
		searchChapterSpan.textContent = '1';
		}
		return;
	}
	
	if (currentChapterIndex === -1) {
		// Not started - next is first chapter
		nextChapterNumber = sortedChapters[0].chapter_number;
		nextChapterUrl = sortedChapters[0].chapter_url;
	} else if (currentChapterIndex < sortedChapters.length - 1) {
		// Next chapter exists
		nextChapterNumber = sortedChapters[currentChapterIndex + 1].chapter_number;
		nextChapterUrl = sortedChapters[currentChapterIndex + 1].chapter_url;
	} else {
		// Already at last chapter
		if (continueBtn) {
		continueBtn.textContent = 'All caught up';
		continueBtn.disabled = true;
		}
		if (searchBtn) {
		searchBtn.style.display = 'flex';
		// Search for next chapter number (current + 1)
		const searchNext = sortedChapters[currentChapterIndex].chapter_number + 1;
		searchChapterSpan.textContent = searchNext;
		}
		return;
	}
	
	// Update Search button
	if (searchBtn && searchChapterSpan) {
		searchBtn.style.display = 'flex';
		searchChapterSpan.textContent = nextChapterNumber;
	}
	
	// Update Continue button
	if (continueBtn) {
		continueBtn.textContent = `Continue to Ch.${nextChapterNumber}`;
		continueBtn.disabled = false;
		
		// Update click handler
		const newContinueBtn = continueBtn.cloneNode(true);
		continueBtn.parentNode.replaceChild(newContinueBtn, continueBtn);
		
		newContinueBtn.addEventListener('click', () => {
		window.open(nextChapterUrl, '_blank');
		});
	}
	}

	// Chapter - button - decrement (NO SAVE, with hold-to-repeat)
	const minusBtn = document.getElementById('sheet-chapter-minus');
	if (minusBtn) {
	const newMinusBtn = minusBtn.cloneNode(true);
	minusBtn.parentNode.replaceChild(newMinusBtn, minusBtn);
	
	const decrementChapter = () => {
		if (currentChapterIndex === -1) {
		stopHoldRepeat();
		return; // Already at "Not started"
		}
		
		if (currentChapterIndex === 0) {
		// Go to "Not started"
		currentChapterIndex = -1;
		mobileState.pendingChapter = -1;
		} else {
		// Go to previous chapter in list
		currentChapterIndex--;
		mobileState.pendingChapter = sortedChapters[currentChapterIndex].chapter_number;
		}
		
		updateChapterDisplay();
		updateSheetProgress(series);
	};
	
	// Mouse events
	newMinusBtn.addEventListener('mousedown', (e) => {
		e.preventDefault();
		startHoldRepeat(decrementChapter);
	});
	newMinusBtn.addEventListener('mouseup', stopHoldRepeat);
	newMinusBtn.addEventListener('mouseleave', stopHoldRepeat);
	
	// Touch events
	newMinusBtn.addEventListener('touchstart', (e) => {
		e.preventDefault();
		startHoldRepeat(decrementChapter);
	});
	newMinusBtn.addEventListener('touchend', stopHoldRepeat);
	newMinusBtn.addEventListener('touchcancel', stopHoldRepeat);
	}

	// Chapter + button - increment (NO SAVE, with hold-to-repeat)
	const plusBtn = document.getElementById('sheet-chapter-plus');
	if (plusBtn) {
	const newPlusBtn = plusBtn.cloneNode(true);
	plusBtn.parentNode.replaceChild(newPlusBtn, plusBtn);
	
	const incrementChapter = () => {
		if (sortedChapters.length === 0) {
		stopHoldRepeat();
		return; // No chapters available
		}
		
		if (currentChapterIndex === -1) {
		// Go to first chapter
		currentChapterIndex = 0;
		mobileState.pendingChapter = sortedChapters[0].chapter_number;
		} else if (currentChapterIndex < sortedChapters.length - 1) {
		// Go to next chapter in list
		currentChapterIndex++;
		mobileState.pendingChapter = sortedChapters[currentChapterIndex].chapter_number;
		} else {
		// Already at last chapter
		stopHoldRepeat();
		return;
		}
		
		updateChapterDisplay();
		updateSheetProgress(series);
	};
	
	// Mouse events
	newPlusBtn.addEventListener('mousedown', (e) => {
		e.preventDefault();
		startHoldRepeat(incrementChapter);
	});
	newPlusBtn.addEventListener('mouseup', stopHoldRepeat);
	newPlusBtn.addEventListener('mouseleave', stopHoldRepeat);
	
	// Touch events
	newPlusBtn.addEventListener('touchstart', (e) => {
		e.preventDefault();
		startHoldRepeat(incrementChapter);
	});
	newPlusBtn.addEventListener('touchend', stopHoldRepeat);
	newPlusBtn.addEventListener('touchcancel', stopHoldRepeat);
	}

}

function updateSheetProgress(series) {
  const progress = mobileState.pendingChapter === -1 ? 0 : (mobileState.pendingChapter / series.latest_chapter) * 100;
  const progressFill = document.getElementById('sheet-progress-fill');
  progressFill.style.width = `${Math.min(progress, 100)}%`;

  // FIXED: Calculate unread using subtraction but round to fix floating-point precision
  let unreadCount = 0;
  if (mobileState.pendingChapter === -1) {
    // Not started - use latest chapter as count
    unreadCount = series.latest_chapter || 0;
  } else {
    // Calculate difference and round to 1 decimal place to fix floating-point errors
    const rawDiff = series.latest_chapter - mobileState.pendingChapter;
    unreadCount = Math.round(rawDiff * 10) / 10;
  }
  
  // Change color to green when caught up
  if (unreadCount === 0 && mobileState.pendingChapter !== -1) {
    progressFill.style.background = '#4ade80';
  } else {
    progressFill.style.background = '#3b82f6';
  }
  
  // Update "behind" text
  const behindElement = document.getElementById('sheet-behind');
  if (unreadCount === 0 && mobileState.pendingChapter !== -1) {
    behindElement.innerHTML = `
      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#4ade80" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:middle;margin-right:4px;">
        <path d="M20 6 9 17l-5-5"/>
      </svg>
      <span style="color:#4ade80;font-weight:500;">Up to date</span>
    `;
  } else {
    // Format the number nicely (remove .0 if it's a whole number)
    const displayCount = unreadCount % 1 === 0 ? Math.floor(unreadCount) : unreadCount;
    const chapterWord = unreadCount === 1 ? 'chapter' : 'chapters';
    behindElement.textContent = `${displayCount} ${chapterWord} behind`;
  }
}

async function closeBottomSheet(keepScrollLocked = false) {
  // ADDED: Save pending chapter changes before closing
  if (mobileState.currentSeries && mobileState.pendingChapter !== null && 
      mobileState.pendingChapter !== mobileState.currentSeries.current_chapter) {
    try {
		await saveChapter(mobileState.currentSeries.id, mobileState.pendingChapter, mobileState.currentSeries.current_chapter);      
		loadPage(); // Reload to reflect changes
    } catch (err) {
      console.error('Failed to save chapter:', err);
      alert('Failed to save chapter');
      return; // Don't close if save failed
    }
  }
  
  mobileState.bottomSheetOpen = false;
  mobileState.currentSeries = null;
  mobileState.pendingChapter = null;

  const bottomSheet = document.getElementById('bottom-sheet');
  const overlay = document.getElementById('bottom-sheet-overlay');
  
  overlay?.classList.remove('active');
  bottomSheet?.classList.remove('active');
  if (bottomSheet) bottomSheet.style.transform = '';
  
  // FIXED: Only unlock scrolling if not keeping it locked
  if (!keepScrollLocked) {
    const savedScrollY = mobileState.scrollY || 0;
    
    // Clear the saved scroll position
    mobileState.scrollY = 0;
    
    // Unlock body styles
    document.body.style.overflow = '';
    document.body.style.position = '';
    document.body.style.width = '';
    document.body.style.top = '';
    
    // Force reflow to ensure styles are applied
    document.body.offsetHeight;
    
    // Restore scroll position
    window.scrollTo(0, savedScrollY);
  }
}

// ================================
// MOBILE EDIT MODAL
// ================================
let mobilePendingSourceChanges = {
  hasChanges: false,
  primarySourceId: null,
  originalPrimaryId: null
};

async function openMobileEditModal(series) {
  // ADDED: Store series in mobileState for notifications
  mobileState.currentSeries = series;
  
  const modal = document.getElementById('mobile-edit-modal');
  document.getElementById('mobile-edit-series-id').value = series.id;
  document.getElementById('mobile-edit-status').value = series.status || 'plan_to_read';
  
  // Load sources
  await loadMobileSeriesSources(series.id);
  
  // Load chapters
  fetch(`/api/series/${series.id}/chapters`)
    .then(r => r.json())
    .then(chapters => {
      const select = document.getElementById('mobile-edit-current-chapter');
      select.innerHTML = '<option value="-1">Not started</option>';
      
      const hasAnyNullVolume = chapters.some(ch => ch.volume == null || ch.volume === '');
      const useVolumeLabels = !hasAnyNullVolume;
      const comparator = useVolumeLabels ? compareChapters : (a, b) => a.chapter_number - b.chapter_number;
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
  
  // FIXED: Lock scrolling - SAME AS BOTTOM SHEET
  mobileState.scrollY = window.scrollY;
  document.body.style.overflow = 'hidden';
  document.body.style.position = 'fixed';
  document.body.style.width = '100%';
  document.body.style.top = `-${mobileState.scrollY}px`;
  
  modal.classList.remove('hidden');
  
  // Scroll modal to top
  const modalContent = modal.querySelector('.modal-content');
  if (modalContent) {
    modalContent.scrollTop = 0;
  }
}

async function loadMobileSeriesSources(seriesId) {
  try {
    const res = await fetch(`/api/series/${seriesId}/sources`);
    if (!res.ok) throw new Error('Failed to load sources');
    
    const data = await res.json();
    const container = document.getElementById('mobile-sources-list');
    
    if (data.sources.length === 0) {
      container.innerHTML = '<p class="loading-text">No sources found.</p>';
      return;
    }
    
    const primarySource = data.sources.find(s => s.is_primary);
    mobilePendingSourceChanges.originalPrimaryId = primarySource ? primarySource.id : null;
    mobilePendingSourceChanges.primarySourceId = mobilePendingSourceChanges.originalPrimaryId;
    mobilePendingSourceChanges.hasChanges = false;
    
    renderMobileSources(data.sources);
    initializeMobileDragAndDrop();
  } catch (err) {
    console.error('Failed to load sources:', err);
    document.getElementById('mobile-sources-list').innerHTML = '<p class="loading-text">Error loading sources.</p>';
  }
}

function renderMobileSources(sources) {
  const container = document.getElementById('mobile-sources-list');
  
  const sortedSources = [...sources].sort((a, b) => {
    if (a.id === mobilePendingSourceChanges.primarySourceId) return -1;
    if (b.id === mobilePendingSourceChanges.primarySourceId) return 1;
    return 0;
  });
    
  container.innerHTML = sortedSources.map(source => {    
	// FIXED: Always use source.source_type directly (don't read from DOM)
	const sourceTypeLabel = {
      'mangadex': 'MangaDex',
      'kagane': 'Kagane',
      'unknown': 'Unknown'
    }[source.source_type.toLowerCase()] || source.source_type;
    
    const isPrimary = source.id === mobilePendingSourceChanges.primarySourceId;
    
    return `
      <div class="source-item ${isPrimary ? 'primary' : ''}" data-source-id="${source.id}" data-source-type="${source.source_type}" draggable="true">
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
            <button class="btn-icon danger" onclick="removeMobileSource(${document.getElementById('mobile-edit-series-id').value}, ${source.id})" title="Remove Source">
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

function initializeMobileDragAndDrop() {
  const container = document.getElementById('mobile-sources-list');
  if (!container) return;
  
  let draggedElement = null;
  
  // ADDED: Touch support for mobile drag-and-drop
  let touchStartY = 0;
  let touchCurrentY = 0;
  let isDragging = false;
  let draggedTouchElement = null;
  
  container.addEventListener('touchstart', (e) => {
    const sourceItem = e.target.closest('.source-item');
    if (!sourceItem) return;
    
    // Don't drag if touching a button
    if (e.target.closest('button')) return;
    
    draggedTouchElement = sourceItem;
    touchStartY = e.touches[0].clientY;
    isDragging = true;
    
    sourceItem.classList.add('dragging');
  }, { passive: true });
  
  container.addEventListener('touchmove', (e) => {
    if (!isDragging || !draggedTouchElement) return;
    
    touchCurrentY = e.touches[0].clientY;
    const afterElement = getDragAfterElement(container, touchCurrentY);
    
    if (afterElement == null) {
      container.appendChild(draggedTouchElement);
    } else {
      container.insertBefore(draggedTouchElement, afterElement);
    }
  }, { passive: true });
  
  container.addEventListener('touchend', (e) => {
    if (!isDragging || !draggedTouchElement) return;
    
    draggedTouchElement.classList.remove('dragging');
    isDragging = false;
    
    // Update primary source after drag
    const sourceItems = container.querySelectorAll('.source-item:not(.sources-help-text)');
    if (sourceItems.length > 0) {
      const newPrimaryId = parseInt(sourceItems[0].dataset.sourceId);
      
      if (newPrimaryId !== mobilePendingSourceChanges.primarySourceId) {
        mobilePendingSourceChanges.primarySourceId = newPrimaryId;
        mobilePendingSourceChanges.hasChanges = true;
        
        const currentSources = Array.from(sourceItems).map(item => ({
          id: parseInt(item.dataset.sourceId),
          source_type: item.dataset.sourceType, // CHANGED: Read from data attribute
          source_url: item.querySelector('.source-url').textContent,
          is_primary: parseInt(item.dataset.sourceId) === newPrimaryId
        }));
        
        renderMobileSources(currentSources);
        initializeMobileDragAndDrop();
      }
    }
    
    draggedTouchElement = null;
  }, { passive: true });
  
  container.addEventListener('touchcancel', () => {
    if (draggedTouchElement) {
      draggedTouchElement.classList.remove('dragging');
    }
    isDragging = false;
    draggedTouchElement = null;
  }, { passive: true });
  
  // Mouse drag events (for desktop testing)
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
    
    const sourceItems = container.querySelectorAll('.source-item:not(.sources-help-text)');
    if (sourceItems.length > 0) {
      const newPrimaryId = parseInt(sourceItems[0].dataset.sourceId);
      
      if (newPrimaryId !== mobilePendingSourceChanges.primarySourceId) {
        mobilePendingSourceChanges.primarySourceId = newPrimaryId;
        mobilePendingSourceChanges.hasChanges = true;
        
        const currentSources = Array.from(sourceItems).map(item => ({
          id: parseInt(item.dataset.sourceId),
          source_type: item.dataset.sourceType, // CHANGED: Read from data attribute
          source_url: item.querySelector('.source-url').textContent,
          is_primary: parseInt(item.dataset.sourceId) === newPrimaryId
        }));
        
        renderMobileSources(currentSources);
        initializeMobileDragAndDrop();
      }
    }
  });
}

async function removeMobileSource(seriesId, sourceId) {
  if (!confirm('Remove this source? Chapters from this source will remain but won\'t be updated.')) {
    return;
  }
  
  try {
    const res = await fetch(`/api/series/${seriesId}/sources/${sourceId}`, {
      method: 'DELETE'
    });
    
    if (res.ok) {
      // Get series title - use same approach as saveMobileEditModal
      let series = null;
      
      // Try mobileState.currentSeries first
      if (mobileState.currentSeries && mobileState.currentSeries.id == seriesId) {
        series = mobileState.currentSeries;
      }
      
      // Fallback: Fetch from API to get accurate series data
      if (!series) {
        const tempRes = await fetch(`/api/series?page=1&per_page=9999`);
        const tempData = await tempRes.json();
        series = tempData.items.find(s => s.id == seriesId);
      }
      
      const seriesTitle = series?.title || 'Series';
      showNotification(`Source removed from ${seriesTitle}`, 'delete');
      
      await loadMobileSeriesSources(seriesId);
    } else {
      const data = await res.json();
      showNotification('Failed to remove source: ' + (data.error || 'Unknown error'), 'error'); // CHANGED

    }
  } catch (err) {
    console.error('Failed to remove source:', err);
    showNotification('Error: ' + err.message, 'error');
  }
}

function showMobileAddSourceForm() {
  document.getElementById('mobile-add-source-form').classList.remove('hidden');
  document.getElementById('mobile-new-source-url').focus();
}

function hideMobileAddSourceForm() {
  document.getElementById('mobile-add-source-form').classList.add('hidden');
  document.getElementById('mobile-new-source-url').value = '';
}

async function addMobileNewSource() {
  const url = document.getElementById('mobile-new-source-url').value.trim();
  const seriesId = document.getElementById('mobile-edit-series-id').value;
  
  if (!url) {
    alert('Please enter a source URL');
    return;
  }
  
  if (!url.startsWith('https://mangadex.org/') && !url.startsWith('https://kagane.org/')) {
    alert('This source is not supported');
    return;
  }
  
  // Check if source already exists in the UI
  const existingSources = document.querySelectorAll('#mobile-sources-list .source-item');
  for (const sourceItem of existingSources) {
    const existingUrl = sourceItem.querySelector('.source-url')?.textContent?.trim();
    if (existingUrl === url) {
      showNotification('Source already exists', 'error');
      return;
    }
  }
  
  try {
    const res = await fetch(`/api/series/${seriesId}/sources`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_url: url })
    });
    
if (res.ok) {
      // Get series title - use same approach as saveMobileEditModal
      let series = null;
      
      // Try mobileState.currentSeries first
      if (mobileState.currentSeries && mobileState.currentSeries.id == seriesId) {
        series = mobileState.currentSeries;
      }
      
      // Fallback: Fetch from API to get accurate series data
      if (!series) {
        const tempRes = await fetch(`/api/series?page=1&per_page=9999`);
        const tempData = await tempRes.json();
        series = tempData.items.find(s => s.id == seriesId);
      }
      
      const seriesTitle = series?.title || 'Series';
      showNotification(`Source added to ${seriesTitle}`, 'added');
      
      hideMobileAddSourceForm();
      await loadMobileSeriesSources(seriesId);

    } else {
      const data = await res.json().catch(() => ({}));
      const errorMsg = data.error || 'Unknown error';
      
      if (res.status === 500) {
        showNotification('Failed to add source - please check if it already exists or try again', 'error');
      } else if (errorMsg.toLowerCase().includes('already exists') || errorMsg.toLowerCase().includes('duplicate')) {
        showNotification('Source already exists', 'error');
      } else {
        showNotification(errorMsg, 'error');
      }
    }
  } catch (err) {
    console.error('Failed to add source:', err);
    showNotification('Network error - please try again', 'error');
  }
}

// Mobile Edit Save/Cancel handlers
document.getElementById('mobile-btn-edit-save')?.addEventListener('click', async () => {
  const seriesId = document.getElementById('mobile-edit-series-id').value;
  if (!seriesId) {
    alert('No series selected');
    return;
  }
  
  const chapterSelect = document.getElementById('mobile-edit-current-chapter');
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
    status: document.getElementById('mobile-edit-status')?.value || 'plan_to_read',
    current_chapter: currentChapterNum
  };
  
  try {
    // FIXED: Fetch specific series by ID to get accurate current values
    // Don't use page list API as it may be filtered by status
    let series = null;
    
    // First try to get from mobileState if it has fresh data
    if (mobileState.currentSeries && mobileState.currentSeries.id == seriesId) {
      series = mobileState.currentSeries;
    }
    
    // Fallback: Fetch ALL series without status filter to find this one
    if (!series) {
      const tempRes = await fetch(`/api/series?page=1&per_page=9999&status=all`);
      const tempData = await tempRes.json();
      series = tempData.items.find(s => s.id == seriesId);
    }
    
    const seriesTitle = series?.title || 'Series';
    const originalStatus = series?.status;
    const originalChapter = series?.current_chapter;
    
    // ADDED: Save source changes first and show notification
    if (mobilePendingSourceChanges.hasChanges) {
      const res = await fetch(`/api/series/${seriesId}/sources/${mobilePendingSourceChanges.primarySourceId}/primary`, {
        method: 'POST'
      });
      
      if (res.ok) {
        showNotification(`Primary source changed for ${seriesTitle}`, 'edit');
        mobilePendingSourceChanges.hasChanges = false;
        mobilePendingSourceChanges.originalPrimaryId = mobilePendingSourceChanges.primarySourceId;
      }
    }
    
	const res = await fetch(`/api/series/${seriesId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates)
    });
    
	if (res.ok) {
		// ADDED: Show notifications ONLY for actual changes
		let hasChanges = false;
		
		// Check status change
		if (originalStatus !== undefined && updates.status !== originalStatus) {
			const statusMap = {
			'reading': 'Reading',
			'plan_to_read': 'Plan to Read',
			'on_hold': 'On Hold',
			'dropped': 'Dropped',
			'completed': 'Completed'
			};
			const statusText = statusMap[updates.status] || updates.status;
			showNotification(`${seriesTitle} marked as ${statusText}`, 'edit');
			hasChanges = true;
		}
		
		// Check chapter change
		if (originalChapter !== undefined && updates.current_chapter !== originalChapter) {
			const formatChapter = (ch) => ch === -1 ? 'Not started' : `Ch.${ch}`;
			const oldChapterText = formatChapter(originalChapter);
			const newChapterText = formatChapter(updates.current_chapter);
			showNotification(`${seriesTitle} updated from ${oldChapterText} to ${newChapterText}`, 'read');
			hasChanges = true;
		}
		
		// ADDED: Update mobileState.currentSeries with new values to prevent stale data
		if (mobileState.currentSeries && mobileState.currentSeries.id == seriesId) {
			mobileState.currentSeries.status = updates.status;
			mobileState.currentSeries.current_chapter = updates.current_chapter;
		}
		
		document.getElementById('mobile-edit-modal').classList.add('hidden');
		document.body.style.overflow = '';
		document.body.style.position = '';
		document.body.style.width = '';
		document.body.style.top = '';
		window.scrollTo(0, mobileState.scrollY || 0);
		loadPage();
	} else {
		const err = await res.json().catch(() => ({}));
		showNotification('Save failed: ' + (err.error || 'Unknown error'), 'error'); // CHANGED
	}
  } catch (e) {
    console.error('Save error:', e);
    showNotification('Network error: ' + e.message, 'error'); // CHANGED
  }
});

document.getElementById('mobile-btn-edit-cancel')?.addEventListener('click', () => {
  if (mobilePendingSourceChanges.hasChanges) {
    if (!confirm('You have unsaved source changes. Discard them?')) {
      return;
    }
  }
  document.getElementById('mobile-edit-modal').classList.add('hidden');
  // FIXED: Unlock scrolling - SAME AS BOTTOM SHEET
  document.body.style.overflow = '';
  document.body.style.position = '';
  document.body.style.width = '';
  document.body.style.top = '';
  window.scrollTo(0, mobileState.scrollY || 0);
});

document.getElementById('mobile-btn-reset-not-started')?.addEventListener('click', () => {
  const seriesId = document.getElementById('mobile-edit-series-id').value;
  if (seriesId && mobileState.currentSeries) {
    saveChapter(seriesId, -1, mobileState.currentSeries.current_chapter).then(() => {
      document.getElementById('mobile-edit-modal').classList.add('hidden');
      // FIXED: Unlock scrolling - SAME AS BOTTOM SHEET
      document.body.style.overflow = '';
      document.body.style.position = '';
      document.body.style.width = '';
      document.body.style.top = '';
      window.scrollTo(0, mobileState.scrollY || 0);
      loadPage();
    });
  }
});

// Click outside to close Edit modal
document.getElementById('mobile-edit-modal')?.addEventListener('click', (e) => {
  if (e.target.id === 'mobile-edit-modal') {
    if (mobilePendingSourceChanges.hasChanges) {
      if (!confirm('You have unsaved source changes. Discard them?')) {
        return;
      }
    }
    document.getElementById('mobile-edit-modal').classList.add('hidden');
    // FIXED: Unlock scrolling - SAME AS BOTTOM SHEET
    document.body.style.overflow = '';
    document.body.style.position = '';
    document.body.style.width = '';
    document.body.style.top = '';
    window.scrollTo(0, mobileState.scrollY || 0);
  }
});

// ================================
// MOBILE SETTINGS MODAL
// ================================
async function openMobileSettingsModal(series) {
  const modal = document.getElementById('mobile-settings-modal');
  document.getElementById('mobile-settings-series-id').value = series.id;
  document.getElementById('mobile-settings-title').value = series.title || '';
  document.getElementById('mobile-settings-cover-url').value = series.cover_url || '';
  
  // FIXED: Check if already locked (coming from bottom sheet)
  const isAlreadyLocked = document.body.style.overflow === 'hidden';
  
  if (!isAlreadyLocked) {
    // Save current scroll position if not already saved
    mobileState.scrollY = window.scrollY;
    
    // Lock scrolling - SIMPLE VERSION
    document.body.style.overflow = 'hidden';
    document.body.style.position = 'fixed';
    document.body.style.width = '100%';
    document.body.style.top = `-${mobileState.scrollY}px`;
  }
  // If already locked, keep existing scroll lock from bottom sheet
  
  modal.classList.remove('hidden');
  
  // Scroll modal to top
  const modalContent = modal.querySelector('.modal-content');
  if (modalContent) {
    modalContent.scrollTop = 0;
  }
}

// Mobile Settings Save/Cancel handlers
document.getElementById('mobile-btn-settings-save')?.addEventListener('click', async () => {
  const seriesId = document.getElementById('mobile-settings-series-id').value;
  if (!seriesId) {
    alert('No series selected');
    return;
  }
  
  const updates = {
    title: document.getElementById('mobile-settings-title')?.value || '',
    cover_url: document.getElementById('mobile-settings-cover-url')?.value || ''
  };
  
  try {
    // CHANGED: Fetch current series data to get accurate original values
    let series = null;
    
    if (state.allSeries) {
      series = state.allSeries.find(s => s.id == seriesId);
    }
    
    if (!series && mobileState.currentSeries && mobileState.currentSeries.id == seriesId) {
      series = mobileState.currentSeries;
    }
    
    if (!series) {
      const tempRes = await fetch(`/api/series?page=1&per_page=9999`);
      const tempData = await tempRes.json();
      series = tempData.items.find(s => s.id == seriesId);
    }
    
    const originalTitle = series?.title || '';
    const originalCover = series?.cover_url || '';
    
    const res = await fetch(`/api/series/${seriesId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates)
    });
    
	if (res.ok) {
		// CHANGED: Show notifications ONLY for actual changes
		const seriesTitle = updates.title || originalTitle || 'Series';
		
		// Check title change - only if actually different
		if (originalTitle && updates.title && updates.title !== originalTitle) {
			showNotification(`${seriesTitle} updated`, 'edit');
		}
		
		// Check cover change - only if actually different
		if (updates.cover_url !== originalCover) {
			showNotification(`${seriesTitle} cover image updated`, 'edit');
		}
		
		const savedScrollY = mobileState.scrollY || 0;
		document.getElementById('mobile-settings-modal').classList.add('hidden');
		
		document.body.style.overflow = '';
		document.body.style.position = '';
		document.body.style.width = '';
		document.body.style.top = '';
		
		setTimeout(() => {
			window.scrollTo(0, savedScrollY);
		}, 0);
		
		loadPage();
	} else {
		const err = await res.json().catch(() => ({}));
		showNotification('Save failed: ' + (err.error || 'Unknown error'), 'error'); // CHANGED
	}  
  } catch (e) {
    console.error('Save error:', e);
    showNotification('Network error: ' + e.message, 'error');
  }
});

document.getElementById('mobile-btn-settings-cancel')?.addEventListener('click', () => {
  const savedScrollY = mobileState.scrollY || 0; // CHANGED: Save before clearing
  document.getElementById('mobile-settings-modal').classList.add('hidden');
  
  // FIXED: Unlock scrolling - SAME AS BOTTOM SHEET
  document.body.style.overflow = '';
  document.body.style.position = '';
  document.body.style.width = '';
  document.body.style.top = '';
  
  // CHANGED: Restore scroll after unlocking
  setTimeout(() => {
    window.scrollTo(0, savedScrollY);
  }, 0);
});

document.getElementById('mobile-btn-check-now')?.addEventListener('click', async () => {
  const seriesId = document.getElementById('mobile-settings-series-id').value;
  const btn = document.getElementById('mobile-btn-check-now');
  btn.disabled = true;
  btn.textContent = 'Checking...';
  
  try {
    const res = await fetch(`/api/series/${seriesId}/check-now`, { method: 'POST' });
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

document.getElementById('mobile-btn-delete-series')?.addEventListener('click', async () => {
  const seriesId = document.getElementById('mobile-settings-series-id').value;
  const title = document.getElementById('mobile-settings-title').value;
  
  if (!confirm(`Delete "${title}"? This action cannot be undone.`)) return;
  
  try {
    const res = await fetch(`/api/series/${seriesId}`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' }
    });
    
	if (res.ok) {
		// ADDED: Show notification
		showNotification(`${title} deleted`, 'delete');
	
		const savedScrollY = mobileState.scrollY || 0;
		document.getElementById('mobile-settings-modal').classList.add('hidden');
	
		document.body.style.overflow = '';
		document.body.style.position = '';
		document.body.style.width = '';
		document.body.style.top = '';
	
		setTimeout(() => {
			window.scrollTo(0, savedScrollY);
		}, 0);
	
		loadPage();
		if (typeof loadGenres === 'function') {
			loadGenres();
		}
	} else {
		showNotification('Failed to delete series', 'error'); // CHANGED
	}
  } catch (e) {
    showNotification('Network error: ' + e.message, 'error');
  }
});

// Click outside to close Settings modal
document.getElementById('mobile-settings-modal')?.addEventListener('click', (e) => {
  if (e.target.id === 'mobile-settings-modal') {
    const savedScrollY = mobileState.scrollY || 0; // CHANGED: Save before clearing
    document.getElementById('mobile-settings-modal').classList.add('hidden');
    
    // FIXED: Unlock scrolling - SAME AS BOTTOM SHEET
    document.body.style.overflow = '';
    document.body.style.position = '';
    document.body.style.width = '';
    document.body.style.top = '';
    
    // CHANGED: Restore scroll after unlocking
    setTimeout(() => {
      window.scrollTo(0, savedScrollY);
    }, 0);
  }
});

// ================================
// BULK TOOLBAR
// ================================
function createBulkToolbar() {
  const existingToolbar = document.getElementById('bulk-toolbar-overlay');
  if (existingToolbar) return;

  const toolbarHTML = `
    <div class="bulk-toolbar-overlay" id="bulk-toolbar-overlay"></div>
    <div class="bulk-toolbar" id="bulk-toolbar">
      <div class="bulk-toolbar-header">
        <button class="btn-close-bulk" id="close-bulk-toolbar">
          <svg class="icon-close" viewBox="0 0 24 24">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <div class="bulk-toolbar-actions">
        <button class="btn-bulk-action read" id="mobile-bulk-read">
          <svg viewBox="0 0 24 24" style="width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:2;">
            <polyline points="9 11 12 14 22 4"></polyline>
            <path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"></path>
          </svg>
          Mark Read (<span id="mobile-bulk-count">0</span>)
        </button>
        <button class="btn-bulk-action edit" id="mobile-bulk-edit">
          <svg class="icon-edit" viewBox="0 0 24 24" style="width:20px;height:20px;">
            <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"></path>
            <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"></path>
          </svg>
          Edit
        </button>
        <button class="btn-bulk-action delete" id="mobile-bulk-delete">
          <svg class="icon-trash" viewBox="0 0 24 24" style="width:20px;height:20px;">
            <polyline points="3 6 5 6 21 6"></polyline>
            <path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"></path>
          </svg>
          Delete
        </button>
      </div>
    </div>
  `;

  document.body.insertAdjacentHTML('beforeend', toolbarHTML);

  document.getElementById('bulk-toolbar-overlay')?.addEventListener('click', exitMobileBulkMode);
  document.getElementById('close-bulk-toolbar')?.addEventListener('click', exitMobileBulkMode);

  // Connect to existing bulk action handlers
  document.getElementById('mobile-bulk-read')?.addEventListener('click', () => {
    document.getElementById('btn-bulk-read')?.click();
  });
  document.getElementById('mobile-bulk-edit')?.addEventListener('click', () => {
    document.getElementById('btn-bulk-edit')?.click();
  });
  document.getElementById('mobile-bulk-delete')?.addEventListener('click', () => {
    document.getElementById('btn-bulk-delete')?.click();
  });
}

// ================================
// BULK EDIT OVERLAY
// ================================
function createBulkEditOverlay() {
  const existingOverlay = document.getElementById('bulk-edit-overlay');
  if (existingOverlay) return;

  const overlayHTML = `
    <div class="bulk-edit-overlay" id="bulk-edit-overlay"></div>
  `;

  document.body.insertAdjacentHTML('beforeend', overlayHTML);

  // Close when clicking overlay
  document.getElementById('bulk-edit-overlay')?.addEventListener('click', closeBulkEditMenu);
}


function openBulkEditMenu() {
  bulkEditMobileState.isMenuOpen = true;
  
  // Save current scroll position
  bulkEditMobileState.scrollY = window.scrollY;
  
  const fabBtn = document.getElementById('fab-bulk-edit');
  const overlay = document.getElementById('bulk-edit-overlay');
  
  // Change FAB to X icon
  if (fabBtn) {
    fabBtn.classList.add('active');
    fabBtn.innerHTML = `
      <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <line x1="18" y1="6" x2="6" y2="18"></line>
        <line x1="6" y1="6" x2="18" y2="18"></line>
      </svg>
    `;
  }
  
  // Show overlay
  if (overlay) {
    overlay.classList.add('active');
  }
  
  // Show action FABs with stagger animation (bottom to top)
  setTimeout(() => {
    document.getElementById('fab-bulk-delete-action')?.classList.add('show');
  }, 50);
  setTimeout(() => {
    document.getElementById('fab-bulk-edit-action')?.classList.add('show');
  }, 100);
  setTimeout(() => {
    document.getElementById('fab-bulk-read')?.classList.add('show');
  }, 150);
  
  // Lock scrolling - MORE AGGRESSIVE
  document.body.style.overflow = 'hidden';
  document.body.style.position = 'fixed';
  document.body.style.width = '100%';
  document.body.style.top = `-${bulkEditMobileState.scrollY}px`;
}

function closeBulkEditMenu() {
  bulkEditMobileState.isMenuOpen = false;
  
  const fabBtn = document.getElementById('fab-bulk-edit');
  const overlay = document.getElementById('bulk-edit-overlay');
  
  // Hide action FABs
  document.getElementById('fab-bulk-read')?.classList.remove('show');
  document.getElementById('fab-bulk-edit-action')?.classList.remove('show');
  document.getElementById('fab-bulk-delete-action')?.classList.remove('show');
  
  // Change X back to edit icon
  if (fabBtn) {
    fabBtn.classList.remove('active');
    fabBtn.innerHTML = `
      <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
        <path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"/>
      </svg>
    `;
  }
  
  // Hide overlay
  if (overlay) {
    overlay.classList.remove('active');
  }
  
  // Unlock scrolling and restore position
  document.body.style.overflow = '';
  document.body.style.position = '';
  document.body.style.width = '';
  document.body.style.top = '';
  
  // Restore scroll position
  window.scrollTo(0, bulkEditMobileState.scrollY);
}

function toggleBulkEditMenu() {
  if (bulkEditMobileState.isMenuOpen) {
    closeBulkEditMenu();
  } else {
    openBulkEditMenu();
  }
}


function showBulkToolbar() {
  document.getElementById('bulk-toolbar-overlay')?.classList.add('active');
  document.getElementById('bulk-toolbar')?.classList.add('active');
  document.getElementById('mobile-bulk-count').textContent = bulkState.selectedIds.size;
}

function hideBulkToolbar() {
  document.getElementById('bulk-toolbar-overlay')?.classList.remove('active');
  document.getElementById('bulk-toolbar')?.classList.remove('active');
}

function exitMobileBulkMode() {
  exitBulkMode();
  hideBulkToolbar();
}

// Override enterBulkMode for mobile
const originalEnterBulkMode = window.enterBulkMode;
window.enterBulkMode = function() {
  if (isMobileDevice()) {
    bulkState.isBulkMode = true;
    document.querySelectorAll('.series-card').forEach(card => {
      card.classList.add('bulk-mode');
    });
    
    // JUST SWAP FAB BUTTONS
    const fabContainer = document.getElementById('fab-container');
    const bulkFAB = document.getElementById('fab-bulk-edit');
    
    if (fabContainer) fabContainer.classList.add('hidden');
    if (bulkFAB) bulkFAB.style.display = 'flex';
    
    // NEW: Check if we should show back-to-top
    if (window.scrollY > 300) {
      document.getElementById('back-to-top')?.classList.add('show-bulk');
    }
    
  } else {
    originalEnterBulkMode();
  }
};

// Override exitBulkMode for mobile
const originalExitBulkMode = window.exitBulkMode;
window.exitBulkMode = function() {
  if (isMobileDevice()) {
    // Close bulk edit menu if open
    if (bulkEditMobileState.isMenuOpen) {
      closeBulkEditMenu();
    }
    
    bulkState.isBulkMode = false;
    bulkState.selectedIds.clear();
    document.querySelectorAll('.series-card').forEach(card => {
      card.classList.remove('bulk-mode', 'selected');
    });
    
    // JUST SWAP BACK FAB BUTTONS
    const fabContainer = document.getElementById('fab-container');
    const bulkFAB = document.getElementById('fab-bulk-edit');
    
    if (fabContainer) fabContainer.classList.remove('hidden');
    if (bulkFAB) bulkFAB.style.display = 'none';
    
    // NEW: Hide back-to-top when exiting bulk mode
    document.getElementById('back-to-top')?.classList.remove('show-bulk');
    
  } else {
    originalExitBulkMode();
  }
};


// ================================
// FAB BUTTONS
// ================================
function createFABButtons() {
  const existingFAB = document.getElementById('fab-container');
  if (existingFAB) return;

  const fabHTML = `
    <div class="fab-container" id="fab-container">
      <button class="fab-secondary" id="fab-secondary">
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" style="width:20px;height:20px;">
          <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path>
        </svg>
      </button>
      <button class="fab-primary" id="fab-primary">
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" style="width:24px;height:24px;">
          <path stroke-linecap="round" stroke-linejoin="round" d="M12 4v16m8-8H4"></path>
        </svg>
      </button>
    </div>
    
    <!-- Bulk Edit FABs (hidden by default) -->
	<button class="fab-bulk-action" id="fab-bulk-read" data-label="Set chapter to last read">
	<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
		<path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path>
	</svg>
	</button>

	<button class="fab-bulk-action" id="fab-bulk-edit-action" data-label="Edit">
	<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
		<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
		<path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"/>
	</svg>
	</button>

	<button class="fab-bulk-action" id="fab-bulk-delete-action" data-label="Delete">
	<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
		<path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path>
	</svg>
	</button>

	<button class="fab-bulk-edit" id="fab-bulk-edit" style="display: none;">
	<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
		<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
		<path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"/>
	</svg>
	</button>
  `;

  document.body.insertAdjacentHTML('beforeend', fabHTML);

  document.getElementById('fab-primary')?.addEventListener('click', () => {
    document.getElementById('btn-add-series')?.click();
  });

  // UPDATED: Refresh with rotation animation
  document.getElementById('fab-secondary')?.addEventListener('click', async () => {
    const fabSecondary = document.getElementById('fab-secondary');
    
    if (window.scrollY > 300) {
      // Back to top behavior
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } else {
      // Refresh behavior with animation
      if (fabSecondary.disabled) return;
      
      fabSecondary.classList.add('refreshing');
      fabSecondary.disabled = true;
      
      try {
        await loadPage();
      } finally {
        setTimeout(() => {
          fabSecondary.classList.remove('refreshing');
          fabSecondary.disabled = false;
        }, 600);
      }
    }
  });

  // Bulk edit FAB click handler
  document.getElementById('fab-bulk-edit')?.addEventListener('click', (e) => {
    e.stopPropagation();
    e.preventDefault();
    toggleBulkEditMenu();
  });

  // Bulk action FAB handlers
  document.getElementById('fab-bulk-read')?.addEventListener('click', () => {
    closeBulkEditMenu();
    document.getElementById('btn-bulk-read')?.click();
  });

  document.getElementById('fab-bulk-edit-action')?.addEventListener('click', () => {
    closeBulkEditMenu();
    document.getElementById('btn-bulk-edit')?.click();
  });

  document.getElementById('fab-bulk-delete-action')?.addEventListener('click', () => {
    closeBulkEditMenu();
    document.getElementById('btn-bulk-delete')?.click();
  });
  
  // Create overlay when FAB is created
  createBulkEditOverlay();
}


function updateFABIcon() {
  const fabSecondary = document.getElementById('fab-secondary');
  if (!fabSecondary) return;

  if (window.scrollY > 300) {
    fabSecondary.innerHTML = `
      <svg class="icon-chevron-up" viewBox="0 0 24 24" style="width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;">
        <polyline points="18 15 12 9 6 15"></polyline>
      </svg>
    `;
  } else {
    fabSecondary.innerHTML = `
      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" style="width:20px;height:20px;">
        <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path>
      </svg>
    `;
  }
}


function hideFABButtons() {
  document.getElementById('fab-container')?.classList.add('hidden');
}

function showFABButtons() {
  document.getElementById('fab-container')?.classList.remove('hidden');
}

// ================================
// SCROLL BEHAVIOR
// ================================
function setupMobileScroll() {
  let ticking = false;

  window.addEventListener('scroll', () => {
    if (!ticking) {
      window.requestAnimationFrame(() => {
        handleMobileScroll();
        ticking = false;
      });
      ticking = true;
    }
  }, { passive: true });
}

function handleMobileScroll() {
  const currentScrollY = window.scrollY;
  const header = document.querySelector('.header-full');
  const backToTop = document.getElementById('back-to-top');

  // Hide header when scrolling down past 300px
  if (currentScrollY > 300 && currentScrollY > mobileState.lastScrollY) {
    header?.classList.add('hidden-mobile');
  } else if (currentScrollY < 50) {
    header?.classList.remove('hidden-mobile');
  }

  // Update FAB icon
  updateFABIcon();

  // NEW: Show back-to-top in bulk mode when scrolled
  if (bulkState.isBulkMode && currentScrollY > 300) {
    backToTop?.classList.add('show-bulk');
  } else {
    backToTop?.classList.remove('show-bulk');
  }

  mobileState.lastScrollY = currentScrollY;
}


// MOBILE CARD TAP BEHAVIOR - COMPLETE REPLACEMENT
// Add this to dashboard.js around line 1800

// ================================
// CARD TAP BEHAVIOR
// ================================

// Override renderSeriesCard to add mobile tap behavior
const originalRenderSeriesCard = window.renderSeriesCard;
window.renderSeriesCard = function(series) {
  const card = originalRenderSeriesCard(series);
  
  if (isMobileDevice()) {
    // Hide checkbox completely on mobile
    const checkbox = card.querySelector('.series-checkbox');
    if (checkbox) {
      checkbox.style.display = 'none';
    }

    // Long-press variables
    let pressTimer = null;
    let touchStartTime = 0;
    let touchStartY = 0;
    const longPressDuration = 500;
    let touchMoved = false;
    let longPressTriggered = false;

    card.addEventListener('touchstart', (e) => {
      if (e.target.closest('button') || e.target.closest('a')) return;
      
      touchStartTime = Date.now();
      touchStartY = e.touches[0].clientY;
      touchMoved = false;
      longPressTriggered = false;
      
      pressTimer = setTimeout(() => {
        if (!touchMoved) {
          longPressTriggered = true;
          toggleCardSelection(series.id);
          
          card.style.transform = 'scale(0.95)';
          setTimeout(() => {
            card.style.transform = '';
          }, 100);
        }
      }, longPressDuration);
    });

    card.addEventListener('touchmove', (e) => {
      const touchY = e.touches[0].clientY;
      const deltaY = Math.abs(touchY - touchStartY);
      
      if (deltaY > 10) {
        touchMoved = true;
        clearTimeout(pressTimer);
      }
    });

    card.addEventListener('touchend', (e) => {
      clearTimeout(pressTimer);
      
      const touchDuration = Date.now() - touchStartTime;
      
      // If long-press was triggered, don't do anything on touchend
      if (longPressTriggered) {
        longPressTriggered = false;
        return;
      }
      
      // Short tap - only if not moved and wasn't a long press
      if (touchDuration < longPressDuration && !touchMoved) {
        if (e.target.closest('button') || e.target.closest('a')) return;
        
        e.preventDefault();
        
        if (bulkState.isBulkMode) {
          toggleCardSelection(series.id);
        } else {
          openBottomSheet(series);
        }
      }
    });

    card.addEventListener('touchcancel', () => {
      clearTimeout(pressTimer);
      touchMoved = false;
      longPressTriggered = false;
    });
  }
  
  return card;
};

// ================================
// INITIALIZATION
// ================================
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initMobile);
} else {
  initMobile();
}

// Re-initialize on window resize
let resizeTimeout;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimeout);
  resizeTimeout = setTimeout(() => {
    if (isMobileDevice()) {
      initMobile();
    }
  }, 250);
});

console.log('[Mobile] Script loaded');