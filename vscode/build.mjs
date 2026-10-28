import { build } from 'esbuild';

await build({
  entryPoints: ['src/ext/extension.ts'],
  outfile: 'dist/extension.cjs',
  bundle: true,
  platform: 'node',
  format: 'cjs',
  target: 'node20',
  external: ['vscode'],
  sourcemap: false,
  minify: false,
  logLevel: 'info',
});
await build({
  entryPoints: ['src/webview/main.ts'],
  outfile: 'media/main.js',
  bundle: true,
  platform: 'browser',
  format: 'iife',
  target: 'chrome120',
  sourcemap: false,
  minify: true,
  logLevel: 'info',
});
