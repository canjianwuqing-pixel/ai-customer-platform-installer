/* Native Chatwoot top-level mode: no SDK, frame, hidden buttons or chat code. */
(() => {
  const token = window.chatwootWebChannel && window.chatwootWebChannel.websiteToken;
  const visitor = window.authToken;
  // Chatwoot v4.18 constructs Widget API URLs from location.search. Supplying
  // website_token only in the server-side /widget fetch renders HTML but makes
  // message/history calls fail. Run synchronously BEFORE any module evaluates.
  // Only the public inbox token belongs in the URL; never the visitor JWT.
  if (typeof token === 'string' && /^[A-Za-z0-9_-]{8,256}$/.test(token)) {
    const params = new URLSearchParams({ website_token: token });
    const locale = new URLSearchParams(location.search).get('locale');
    if (locale && /^[A-Za-z_\-]{2,16}$/.test(locale)) params.set('locale', locale);
    history.replaceState(null, '', location.pathname + '?' + params +
      (location.hash || '#/messages'));
  }
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
