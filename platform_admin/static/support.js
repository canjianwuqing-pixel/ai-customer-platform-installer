/* Native Chatwoot top-level mode: no SDK, frame, hidden buttons or chat code. */
(() => {
  // @chatwoot/utils 0.0.57 (v4.18.0's locked dependency) assigns this global
  // from an ES module. If the binding is absent, its strict-mode assignment
  // throws and utils.esm.js:881 falls back to Function(...), violating CSP.
  // Create only the writable binding before modules run; the upstream bundle
  // installs the ACTUAL runtime. Preserve existing runtimes and keep eval off.
  if (!('regeneratorRuntime' in window)) window.regeneratorRuntime = undefined;
  // v4.18's vue-i18n 9.14.5 also defaults to Function-based message compilation.
  // Its bundled, supported AST/JIT interpreter avoids dynamic JavaScript.
  // Set the runtime feature flag before dependency initialization, without
  // changing locale strings, Chatwoot assets or any server-side configuration.
  if (typeof window.__INTLIFY_JIT_COMPILATION__ !== 'boolean') {
    window.__INTLIFY_JIT_COMPILATION__ = true;
  }
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
