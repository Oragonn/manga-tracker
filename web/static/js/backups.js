let currentBackupFilename = null;

async function loadBackups() {
  try {
    const res = await fetch('/api/backups');
    const data = await res.json();
    
    if (data.error) {
      document.getElementById('backup-list').innerHTML = 
        `<p style="color: #ef4444;">Error: ${data.error}</p>`;
      return;
    }
    
    // Update status
    document.getElementById('total-backups').textContent = data.count;
    document.getElementById('disk-usage').textContent = 
      `${data.total_size_mb.toFixed(1)} MB`;
    
    // Calculate usage percentage (assuming 35 MB target for 7 days)
    const usagePercent = Math.min((data.total_size_mb / 35) * 100, 100);
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
      
      for (const backup of backups) {
        const ageText = backup.age_hours < 1 
          ? `${Math.floor(backup.age_hours * 60)} min ago`
          : backup.age_hours < 24 
            ? `${Math.floor(backup.age_hours)} hours ago`
            : `${Math.floor(backup.age_hours / 24)} days ago`;
        
        html += `
          <div class="backup-entry">
            <div class="backup-info">
              <div class="backup-filename">${backup.filename}</div>
              <div class="backup-meta">
                Size: ${backup.size_mb.toFixed(2)} MB  •  ${ageText}
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
      
      html += `</div>`;
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

function showRestoreModal(filename, ageText, sizeMB) {
  currentBackupFilename = filename;
  
  document.getElementById('restore-details').innerHTML = `
    <p>You are about to restore from:</p>
    <p style="color: white; font-weight: 600; margin: 8px 0;">📦 ${filename}</p>
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
      alert('✅ Database restored successfully! Please restart the application.');
      closeRestoreModal();
    } else {
      alert('❌ Restore failed: ' + (data.error || 'Unknown error'));
      btn.textContent = 'Yes, Restore Database';
      btn.disabled = false;
    }
  } catch (err) {
    alert('❌ Network error: ' + err.message);
    btn.textContent = 'Yes, Restore Database';
    btn.disabled = false;
  }
});

// Load backups on page load
document.addEventListener('DOMContentLoaded', () => {
  loadBackups();
  
  // Auto-refresh every 60 seconds
  setInterval(loadBackups, 60000);
});