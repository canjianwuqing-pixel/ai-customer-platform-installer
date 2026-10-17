# Widget CSP regression dependencies (test only)

These are not production dependencies. Versions of Vue, vue-i18n, date-fns and
`@chatwoot/utils` match Chatwoot **v4.18.0**'s `pnpm-lock.yaml`. The fixture uses
the real Vue renderer, i18n compiler/interpreter and published utils bundle,
including the exact `utils.esm.js:881` regenerator fallback. It is not the
complete compiled Chatwoot frontend. No deployment/inbox/database is needed.

From the project root, with Node.js/npm available:

```bash
npm ci --ignore-scripts --no-audit --no-fund --prefix tests/fixtures/widget-csp
node tests/fixtures/widget-csp/build.mjs
REQUIRE_WIDGET_CSP_TEST=true python3 -m unittest discover -s tests -p widget_csp_test.py -v
```

Install the existing Python browser test dependencies first; use
`TEST_CHROMIUM_BINARY`, `TEST_NGINX_BINARY`, `TEST_OPENSSL_BINARY` as needed.
An alternative isolated dependency directory is supported through the absolute
`WIDGET_CSP_NODE_MODULES` path. The generated bundle is ignored under `runtime/`;
no Node package, asset or test fixture is copied into production Docker images.
`REQUIRE_WIDGET_CSP_TEST=true` forbids silently skipping the browser regression
when the bundle is missing. Existing Python/fixture discovery includes the test.

The old bootstrap is replayed under the same unchanged CSP to prove a real
Chromium EvalError and failure to mount. The corrected bootstrap then mounts
Vue, renders translated text, loads two different Website Inbox tokens, sends
messages and receives simulated AI replies over real Nginx/TLS. A same-origin
page script separately proves `eval` and `Function` remain blocked, including
on the Admin's script-enabled page. This deliberately avoids CDP/DevTools
evaluation as evidence of CSP enforcement.

Why no `unsafe-eval`:

- The utils module attempts an undeclared strict-mode global assignment, then
  falls back to `Function`. Predeclaring the writable global lets the actual
  upstream regenerator runtime install itself without that fallback; it is not
  a replacement/stub runtime and existing bindings are not overwritten.
- vue-i18n 9.14.5 defaults to Function-based message compilation. Its supported
  `__INTLIFY_JIT_COMPILATION__` flag selects AST interpretation. Initializing it
  before dependency imports avoids the second EvalError without modifying
  translations, rebuilding Chatwoot, intercepting Function or weakening CSP.
- Both initializations are in the existing synchronous `/support/session.js`,
  not Admin HTML. All Admin and Support CSP directives remain byte-for-byte
  unchanged. Future upstream build/version changes require rerunning this test.

Primary sources:

- [Locked versions](https://github.com/chatwoot/chatwoot/blob/v4.18.0/pnpm-lock.yaml)
- [Widget imports and i18n initialization](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/javascript/entrypoints/widget.js)
- [Vite flags/aliases](https://github.com/chatwoot/chatwoot/blob/v4.18.0/vite.shared.ts)
- [Published utils bundle](https://unpkg.com/@chatwoot/utils@0.0.57/dist/utils.esm.js)
- [i18n AST and Function compiler implementations](https://github.com/intlify/vue-i18n/blob/v9.14.5/packages/core-base/src/compilation.ts)
