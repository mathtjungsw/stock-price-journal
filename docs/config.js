// Public connection address only. No API keys, passwords, or portfolio data.
window.JOURNAL_CONFIG = {
  apiBase: ['localhost', '127.0.0.1'].includes(location.hostname)
    ? location.origin
    : 'https://stock-journal.52-79-211-161.sslip.io'
};
