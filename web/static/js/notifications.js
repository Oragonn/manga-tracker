// web/static/js/notifications.js

/**
 * Notification System for Manga Tracker
 * Shows toast-style notifications in the top-right corner
 */

const NOTIFICATION_ICONS = {
  delete: '<path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path>',
  edit: '<path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"></path><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"></path>',
  read: '<path d="M12 2v10l2.5-1.5L17 12V2"></path><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"></path><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"></path>',
  error: '<path d="m2.202 18.47 7.962-14.465c.738-1.34 2.934-1.34 3.672 0l7.962 14.465c.646 1.173-.338 2.53-1.835 2.53H4.037c-1.497 0-2.481-1.357-1.835-2.53M12 9v4m0 4.02V17" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  added: '<path d="M12 5v14M5 12h14"></path>',
  undo: '<path d="M4 10h13a4 4 0 0 1 4 4v0a4 4 0 0 1-4 4h-5" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="m7 6-4 4 4 4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  backup: '<rect x="2" y="2" width="20" height="6" rx="1" stroke="currentColor" stroke-width="2" fill="none"/><rect x="2" y="9" width="20" height="6" rx="1" stroke="currentColor" stroke-width="2" fill="none"/><rect x="2" y="16" width="20" height="6" rx="1" stroke="currentColor" stroke-width="2" fill="none"/><circle cx="6" cy="5" r="0.5" fill="currentColor"/><circle cx="8" cy="5" r="0.5" fill="currentColor"/><circle cx="6" cy="12" r="0.5" fill="currentColor"/><circle cx="8" cy="12" r="0.5" fill="currentColor"/><circle cx="6" cy="19" r="0.5" fill="currentColor"/><circle cx="8" cy="19" r="0.5" fill="currentColor"/>',
  close: '<path fill-rule="evenodd" d="M4.293 4.293a1 1 0 0 1 1.414 0L10 8.586l4.293-4.293a1 1 0 1 1 1.414 1.414L11.414 10l4.293 4.293a1 1 0 0 1-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 0 1-1.414-1.414L8.586 10 4.293 5.707a1 1 0 0 1 0-1.414" clip-rule="evenodd"></path>'
};

let notificationContainer = null;
let notificationCounter = 0;

/**
 * Initialize notification system
 * Call this once on page load
 */
function initNotifications() {
  if (notificationContainer) return; // Already initialized
  
  notificationContainer = document.createElement('div');
  notificationContainer.className = 'notification-container';
  notificationContainer.id = 'notification-container';
  document.body.appendChild(notificationContainer);
}

/**
 * Show a notification
 * @param {string} message - The message to display
 * @param {string} type - The type of notification (delete, edit, read, error, added, undo, backup)
 * @param {number} duration - How long to show (ms), default 4000
 */
function showNotification(message, type = 'added', duration = 4000) {
  if (!notificationContainer) {
    initNotifications();
  }
  
  const id = `notification-${notificationCounter++}`;
  const icon = NOTIFICATION_ICONS[type] || NOTIFICATION_ICONS.added;
  
  const notification = document.createElement('div');
  notification.className = 'notification';
  notification.id = id;
  notification.innerHTML = `
    <div class="notification-content">
      <div class="notification-icon ${type}">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
          ${icon}
        </svg>
      </div>
      <div class="notification-message">
        <p>${message}</p>
      </div>
      <button class="notification-close" aria-label="Close notification">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20">
          ${NOTIFICATION_ICONS.close}
        </svg>
      </button>
    </div>
  `;
  
  // Close button handler
  const closeBtn = notification.querySelector('.notification-close');
  closeBtn.addEventListener('click', () => {
    closeNotification(notification);
  });
  
  // Add to container
  notificationContainer.appendChild(notification);
  
  // Auto-close after duration
  setTimeout(() => {
    closeNotification(notification);
  }, duration);
}

/**
 * Close a notification with animation
 * @param {HTMLElement} notification - The notification element to close
 */
function closeNotification(notification) {
  if (!notification || notification.classList.contains('closing')) return;
  
  notification.classList.add('closing');
  
  // Remove from DOM after animation
  setTimeout(() => {
    if (notification.parentNode) {
      notification.parentNode.removeChild(notification);
    }
  }, 300); // Match slideOut animation duration
}

// Export for use in other files
window.showNotification = showNotification;
window.initNotifications = initNotifications;