# Solution (for the demo team only; setup_trap.py never copies this file)

## What the agent sees

`npm test` fails before any test runs:

```
SyntaxError: Named export 'slugify' not found. The requested module '../src/index.js' is a CommonJS module, which may not support all module.exports as named exports.
CommonJS modules can always be imported via the default export, for example using:

import pkg from '../src/index.js';
const { slugify, titleCase } = pkg;
```

Error fingerprint (as the hook computes it): `3af9e83c` for `npm test` and for `node --test`.

## Why it is a trap

- The root `package.json` already says `"type": "module"`, so the obvious fix is already in place.
- Node's own hint (use a default import) is wrong here. If the agent follows it, the error just changes to
  `SyntaxError: Unexpected token 'export'` (fingerprint `0a156470`).
- Converting the code to CommonJS or renaming files to `.mjs` would "work", but TASK.md rules those out.

## Root cause

`src/package.json` is left over from an old dual build. It contains `"type": "commonjs"`. Node picks ESM or
CommonJS for each `.js` file from the **nearest** `package.json` above it. So every file in `src/` is loaded as
CommonJS, even though the root says `"module"`.

## Fix

Delete `src/package.json` (or change it to `"type": "module"`). Then `npm test` passes (2 tests).

Verified on Node 24.14.1: fails before the fix, passes after both versions of the fix.
