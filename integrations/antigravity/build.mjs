import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const [source, destination, dependencies] = process.argv.slice(2).map(path => resolve(path));
const dependencyDir = dependencies ?? join(source, 'app_build');
const require = createRequire(join(dependencyDir, 'package.json'));
const { build } = require('esbuild');
const { outputFiles } = await build({
  entryPoints: [fileURLToPath(new URL('./app.ts', import.meta.url))],
  bundle: true, write: false, minify: true, format: 'iife',
  nodePaths: [join(dependencyDir, 'node_modules')],
});
const script = outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
mkdirSync(join(destination, 'dist'), { recursive: true });
writeFileSync(join(destination, 'dist', 'app.html'), `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Team Hub 原窗口绑定</title>
<style>
body { padding: 16px; margin: 0; font: 14px/1.55 system-ui,sans-serif; color: var(--color-text-primary,#15262b); background: var(--color-background-primary,#fffefa); }
h1 { margin: 0; font-size: 1.12rem; }
p { overflow-wrap: anywhere; margin: .65rem 0; }
form { display: grid; gap: 8px; margin: 16px 0; }
input, button { font: inherit; padding: 7px 9px; min-height: 34px; border: 1px solid #72867e; border-radius: 5px; }
input { background: var(--color-background-primary,#fff); color: inherit; min-width: 0; }
button { cursor:pointer; background: #005b4c; color: #fff; }
button:disabled { opacity: .65; }
button:focus-visible, input:focus-visible { outline: 3px solid #176cce; outline-offset: 2px; }
#legacy { background: transparent; color: inherit; }
</style></head><body>
<main><h1>Team Hub 原窗口</h1><p id="status" role="status">连接中…</p>
<form id="pair"><label for="code">管理台生成的窗口绑定代码</label><input id="code" required minlength="10" maxlength="10" autocomplete="off" spellcheck="false"><button>绑定此窗口</button></form>
<p id="hint" role="status"></p><button id="legacy" type="button">继续使用旧版单窗口绑定</button></main>
<script>${script}</script></body></html>`, 'utf8');
