'use strict';

// Restore the page palette before the stylesheets paint the page.
(() => {
  let theme = 'dark';
  try {
    const saved = localStorage.getItem('web-terminal-theme');
    if (saved === 'light' || saved === 'dark') theme = saved;
  } catch (_) { /* Storage may be disabled; theme switching still works. */ }
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="color-scheme"]').content = theme;
})();
