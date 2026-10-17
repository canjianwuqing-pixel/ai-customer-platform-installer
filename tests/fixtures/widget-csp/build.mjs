// Build only test dependencies in an ignored directory, not production assets.
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const dependencyRoot = process.env.WIDGET_CSP_NODE_MODULES || fileURLToPath(new URL('./node_modules', import.meta.url));
if (!path.isAbsolute(dependencyRoot)) throw new Error('The test dependency directory must be absolute.');
const require = createRequire(path.join(dependencyRoot, '..', 'package.json'));
const { build } = require('esbuild');
const root = fileURLToPath(new URL('../../../', import.meta.url));
await build({
  entryPoints: [fileURLToPath(new URL('./widget.js', import.meta.url))],
  outfile: path.join(root, 'runtime/tests/widget-csp.bundle.js'),
  bundle: true, format: 'esm', platform: 'browser', minify: true,
  nodePaths: [dependencyRoot], external: ['/contract/*'],
  // Match v4.18 vite.shared.ts's full Vue build; render functions below stand
  // in for compiled SFCs. Leave i18n build flags unchanged: the production
  // bootstrap must select the CSP-safe supported runtime mode before imports.
  alias: { vue: 'vue/dist/vue.esm-bundler.js' },
  define: { 'process.env.NODE_ENV': '"production"', __VUE_OPTIONS_API__: 'true',
    __VUE_PROD_DEVTOOLS__: 'false', __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false' },
});
