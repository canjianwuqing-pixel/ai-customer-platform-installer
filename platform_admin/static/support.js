/* Native Chatwoot top-level mode: no SDK, frame, hidden buttons or chat code. */
(() => {
  // The native Messages route contains the first-message composer as well as
  // existing history; do not land on Widget's optional welcome/home screen.
  if (!window.location.hash) window.location.replace('#/messages');
  const token = window.chatwootWebChannel && window.chatwootWebChannel.websiteToken;
  const visitor = window.authToken;
  if (typeof token === 'string' && /^[A-Za-z0-9_-]{8,256}$/.test(token) &&
      typeof visitor === 'string' && /^[A-Za-z0-9_.-]{1,4096}$/.test(visitor)) {
    document.cookie = `platform_support_${token}=${visitor}; Path=/support/; Max-Age=604800; Secure; SameSite=Lax`;
  }
  const resize = () => document.documentElement.style.setProperty('--support-height',
    `${window.visualViewport ? window.visualViewport.height : window.innerHeight}px`);
  resize();
  window.addEventListener('resize', resize);
  if (window.visualViewport) window.visualViewport.addEventListener('resize', resize);
})();
