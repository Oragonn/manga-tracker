let currentBackupFilename = null;
let currentBackupTab = 'database';

function switchBackupTab(tab) {
  currentBackupTab = tab;
  document.querySelectorAll('.backup-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tab);
  });
  document.getElementById('tab-database')?.classList.toggle('hidden', tab !== 'database');
  document.getElementById('tab-series-csv')?.classList.toggle('hidden', tab !== 'series-csv');

  if (tab === 'series-csv') {
    loadSeriesBackups();
  } else {
    loadBackups();
  }
}

// Format date to DD/MM/YYYY HH:MM
function formatFullDate(dateObj) {
  const day = String(dateObj.getDate()).padStart(2, '0');
  const month = String(dateObj.getMonth() + 1).padStart(2, '0');
  const year = dateObj.getFullYear();
  const hours = String(dateObj.getHours()).padStart(2, '0');
  const minutes = String(dateObj.getMinutes()).padStart(2, '0');
  return `${day}/${month}/${year} ${hours}:${minutes}`;
}

async function loadBackups() {
  try {
    const res = await fetch('/api/backups');
    const data = await res.json();
    
    if (data.error) {
      document.getElementById('backup-list').innerHTML =
        `<p style="color: #ef4444;">Error: ${escapeHtml(data.error)}</p>`;
      return;
    }
    
    // Update status
    document.getElementById('total-backups').textContent = data.count;
    const dbTargetMB = 2048; // 2 GB gauge, not a hard limit
    document.getElementById('disk-usage').textContent =
      `${data.total_size_mb.toFixed(1)} MB / 2 GB`;

    // Calculate usage percentage against the gauge target
    const usagePercent = Math.min((data.total_size_mb / dbTargetMB) * 100, 100);
    document.getElementById('usage-fill').style.width = `${usagePercent}%`;
    
    // Calculate last backup time
    if (data.backups.length > 0) {
      const lastBackup = data.backups[0];
      const ageHours = lastBackup.age_hours;
      let lastBackupText;
      if (ageHours < 1) {
        lastBackupText = `${Math.floor(ageHours * 60)} min ago`;
      } else if (ageHours < 24) {
        lastBackupText = `${Math.floor(ageHours)} hours ago`;
      } else {
        lastBackupText = `${Math.floor(ageHours / 24)} days ago`;
      }
      document.getElementById('last-backup').textContent = lastBackupText;
      
      // Calculate next backup (hourly)
      const nextInMinutes = 60 - (ageHours * 60) % 60;
      document.getElementById('next-backup').textContent = 
        `${Math.floor(nextInMinutes)} min`;
    }
    
    // Group backups by date
    const grouped = groupByDate(data.backups);
    
    // Render backups
    let html = '';
    for (const [dateLabel, backups] of Object.entries(grouped)) {
      html += `
        <div class="date-group">
          <div class="date-group-header">📅 ${dateLabel}</div>
      `;
      
      // CHANGED: Wrap backup entries in a content div for mobile collapsing
      html += `<div class="date-group-content">`;
      
      for (const backup of backups) {
        const ageText = backup.age_hours < 1 
          ? `${Math.floor(backup.age_hours * 60)} min ago`
          : backup.age_hours < 24 
            ? `${Math.floor(backup.age_hours)} hours ago`
            : `${Math.floor(backup.age_hours / 24)} days ago`;
        
        // Format full date for tooltip
        const backupDate = new Date(backup.created);
        const fullDate = formatFullDate(backupDate);
        
        html += `
          <div class="backup-entry">
            <div class="backup-info">
              <div class="backup-filename">${escapeHtml(backup.filename)}</div>
              <div class="backup-meta">
                Size: ${backup.size_mb.toFixed(2)} MB  •  
                <span class="backup-time" data-full-date="${fullDate}">${ageText}</span>
              </div>
            </div>
            <div class="backup-entry-actions">
              <button class="btn-backup" onclick="downloadBackup('${backup.filename}')">
                ⬇️ Download
              </button>
              <button class="btn-backup danger" onclick="showRestoreModal('${backup.filename}', '${ageText}', ${backup.size_mb.toFixed(2)})">
                ↻ Restore
              </button>
            </div>
          </div>
        `;
      }
      
      html += `</div></div>`; // CHANGED: Close content wrapper and date-group
    }
    
    document.getElementById('backup-list').innerHTML = html || 
      '<p style="color: #94a3b8;">No backups found.</p>';
    
  } catch (err) {
    console.error('Failed to load backups:', err);
    document.getElementById('backup-list').innerHTML = 
      '<p style="color: #ef4444;">Error loading backups.</p>';
  }
}

function groupByDate(backups) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  
  const groups = {
    'Today': [],
    'Yesterday': [],
    'This Week': [],
    'Older': []
  };
  
  for (const backup of backups) {
    const backupDate = new Date(backup.created);
    const backupDay = new Date(backupDate.getFullYear(), backupDate.getMonth(), backupDate.getDate());
    
    if (backupDay.getTime() === today.getTime()) {
      groups['Today'].push(backup);
    } else if (backupDay.getTime() === yesterday.getTime()) {
      groups['Yesterday'].push(backup);
    } else if ((now - backupDay) / 86400000 < 7) {
      groups['This Week'].push(backup);
    } else {
      groups['Older'].push(backup);
    }
  }
  
  // Remove empty groups
  return Object.fromEntries(
    Object.entries(groups).filter(([_, backups]) => backups.length > 0)
  );
}

async function refreshStatus() {
  await loadBackups();
}

async function createBackupNow() {
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = '⏳ Creating...';
  
  try {
    const res = await fetch('/api/backups/create', { method: 'POST' });
    const data = await res.json();
    
    if (data.success) {
      // ADDED: Show notification
      showNotification('Backup created successfully', 'backup');
      btn.textContent = '✅ Created!';
      setTimeout(() => {
        btn.textContent = '➕ Create Backup Now';
        btn.disabled = false;
        loadBackups();
      }, 1500);
    } else {
      btn.textContent = '❌ Failed';
      setTimeout(() => {
        btn.textContent = '➕ Create Backup Now';
        btn.disabled = false;
      }, 1500);
    }
  } catch (err) {
    console.error('Backup creation failed:', err);
    btn.textContent = '❌ Error';
    setTimeout(() => {
      btn.textContent = '➕ Create Backup Now';
      btn.disabled = false;
    }, 1500);
  }
}

function downloadBackup(filename) {
  window.location.href = `/api/backups/download/${filename}`;
}

async function loadSeriesBackups() {
  try {
    const res = await fetch('/api/backups/series-csv');
    const data = await res.json();

    if (data.error) {
      document.getElementById('series-backup-list').innerHTML =
        `<p style="color: #ef4444;">Error: ${escapeHtml(data.error)}</p>`;
      return;
    }

    document.getElementById('csv-total-backups').textContent = data.count;
    document.getElementById('csv-disk-usage').textContent =
      `${(data.total_size_mb * 1024).toFixed(1)} KB / 10 MB`;

    const csvTargetMB = 10; // 10 MB gauge, not a hard limit
    const csvUsagePercent = Math.min((data.total_size_mb / csvTargetMB) * 100, 100);
    document.getElementById('csv-usage-fill').style.width = `${csvUsagePercent}%`;

    if (data.backups.length > 0) {
      const ageHours = data.backups[0].age_hours;
      let lastBackupText;
      if (ageHours < 1) {
        lastBackupText = `${Math.floor(ageHours * 60)} min ago`;
      } else if (ageHours < 24) {
        lastBackupText = `${Math.floor(ageHours)} hours ago`;
      } else {
        lastBackupText = `${Math.floor(ageHours / 24)} days ago`;
      }
      document.getElementById('csv-last-backup').textContent = lastBackupText;

      // Calculate next backup (daily)
      const csvIntervalHours = 24;
      const nextInHours = csvIntervalHours - (ageHours % csvIntervalHours);
      let nextBackupText;
      if (nextInHours < 1) {
        nextBackupText = `${Math.floor(nextInHours * 60)} min`;
      } else {
        nextBackupText = `${Math.floor(nextInHours)}h ${Math.floor((nextInHours % 1) * 60)}m`;
      }
      document.getElementById('csv-next-backup').textContent = nextBackupText;
    } else {
      document.getElementById('csv-last-backup').textContent = '—';
      document.getElementById('csv-next-backup').textContent = '—';
    }

    const grouped = groupByDate(data.backups);

    let html = '';
    for (const [dateLabel, backups] of Object.entries(grouped)) {
      html += `
        <div class="date-group">
          <div class="date-group-header">📅 ${dateLabel}</div>
      `;
      html += `<div class="date-group-content">`;

      for (const backup of backups) {
        const ageText = backup.age_hours < 1
          ? `${Math.floor(backup.age_hours * 60)} min ago`
          : backup.age_hours < 24
            ? `${Math.floor(backup.age_hours)} hours ago`
            : `${Math.floor(backup.age_hours / 24)} days ago`;

        const backupDate = new Date(backup.created);
        const fullDate = formatFullDate(backupDate);

        html += `
          <div class="backup-entry">
            <div class="backup-info">
              <div class="backup-filename">${escapeHtml(backup.filename)}</div>
              <div class="backup-meta">
                Size: ${(backup.size_mb * 1024).toFixed(1)} KB  •
                <span class="backup-time" data-full-date="${fullDate}">${ageText}</span>
              </div>
            </div>
            <div class="backup-entry-actions">
              <button class="btn-backup" onclick="downloadSeriesBackup('${backup.filename}')">
                ⬇️ Download
              </button>
            </div>
          </div>
        `;
      }

      html += `</div></div>`;
    }

    document.getElementById('series-backup-list').innerHTML = html ||
      '<p style="color: #94a3b8;">No backups found.</p>';

  } catch (err) {
    console.error('Failed to load series backups:', err);
    document.getElementById('series-backup-list').innerHTML =
      '<p style="color: #ef4444;">Error loading backups.</p>';
  }
}

async function createSeriesBackupNow() {
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = '⏳ Creating...';

  try {
    const res = await fetch('/api/backups/series-csv/create', { method: 'POST' });
    const data = await res.json();

    if (data.success) {
      showNotification('Series backup created successfully', 'backup');
      btn.textContent = '✅ Created!';
      setTimeout(() => {
        btn.textContent = '➕ Create Backup Now';
        btn.disabled = false;
        loadSeriesBackups();
      }, 1500);
    } else {
      btn.textContent = '❌ Failed';
      setTimeout(() => {
        btn.textContent = '➕ Create Backup Now';
        btn.disabled = false;
      }, 1500);
    }
  } catch (err) {
    console.error('Series backup creation failed:', err);
    btn.textContent = '❌ Error';
    setTimeout(() => {
      btn.textContent = '➕ Create Backup Now';
      btn.disabled = false;
    }, 1500);
  }
}

function downloadSeriesBackup(filename) {
  window.location.href = `/api/backups/series-csv/download/${filename}`;
}

function showRestoreModal(filename, ageText, sizeMB) {
  currentBackupFilename = filename;
  
  document.getElementById('restore-details').innerHTML = `
    <p>You are about to restore from:</p>
    <p style="color: white; font-weight: 600; margin: 8px 0;">📦 ${escapeHtml(filename)}</p>
    <p>Created: ${ageText}</p>
    <p>Size: ${sizeMB} MB</p>
  `;
  
  document.getElementById('restore-modal').classList.remove('hidden');
}

function closeRestoreModal() {
  currentBackupFilename = null;
  document.getElementById('restore-modal').classList.add('hidden');
}

document.getElementById('confirm-restore-btn')?.addEventListener('click', async () => {
  if (!currentBackupFilename) return;
  
  const btn = document.getElementById('confirm-restore-btn');
  btn.disabled = true;
  btn.textContent = 'Restoring...';
  
  try {
    const res = await fetch(`/api/backups/restore/${currentBackupFilename}`, {
      method: 'POST'
    });
    const data = await res.json();
    
    if (data.success) {
      // ADDED: Show notification
      showNotification('Database restored from backup', 'backup');
      closeRestoreModal();
    } else {
      showNotification('Restore failed: ' + (data.error || 'Unknown error'), 'error');
      btn.textContent = 'Yes, Restore Database';
      btn.disabled = false;
    }
  } catch (err) {
    showNotification('Network error: ' + err.message, 'error');
    btn.textContent = 'Yes, Restore Database';
    btn.disabled = false;
  }
});

// Load backups on page load
document.addEventListener('DOMContentLoaded', () => {
  initNotifications();
  loadBackups();
  
  // Auto-refresh every 60 seconds (whichever tab is active)
  setInterval(() => {
    if (currentBackupTab === 'series-csv') {
      loadSeriesBackups();
    } else {
      loadBackups();
    }
  }, 60000);
});

// Mobile collapsible date groups
(function() {
  function isMobile() {
    return window.innerWidth <= 768;
  }

  if (isMobile()) {
    console.log('[Mobile] Enabling collapsible date groups...');
    
    // Add click handlers to date group headers
    document.addEventListener('click', (e) => {
      const header = e.target.closest('.date-group-header');
      if (!header) return;
      
      const content = header.nextElementSibling;
      if (!content || !content.classList.contains('date-group-content')) return;
      
      // Toggle collapsed state
      const isCollapsed = content.classList.contains('collapsed');
      
      if (isCollapsed) {
        // Expand
        content.classList.remove('collapsed');
        header.classList.remove('collapsed');
        content.style.maxHeight = content.scrollHeight + 'px';
      } else {
        // Collapse
        content.classList.add('collapsed');
        header.classList.add('collapsed');
        content.style.maxHeight = '0';
      }
    });
    
    // Initialize collapsed state (keep Today expanded, collapse others)
    const observer = new MutationObserver(() => {
      const dateGroups = document.querySelectorAll('.date-group');
      if (dateGroups.length === 0) return;
      
      dateGroups.forEach((group, index) => {
        const header = group.querySelector('.date-group-header');
        const content = group.querySelector('.date-group-content');
        
        if (!header || !content) return;
        
        // Wrap content if not already wrapped
        if (!content.classList.contains('date-group-content')) {
          const entries = Array.from(group.children).filter(el => 
            el.classList.contains('backup-entry')
          );
          
          const wrapper = document.createElement('div');
          wrapper.className = 'date-group-content';
          entries.forEach(entry => wrapper.appendChild(entry));
          group.appendChild(wrapper);
        }
              });
      
      observer.disconnect();
    });
    
    observer.observe(document.getElementById('backup-list'), {
      childList: true,
      subtree: true
    });
  }
})();