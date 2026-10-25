# Task

The tests for `slugkit` fail. Please make `npm test` pass.

slugkit is published as ES modules only (since v2), so keep it that way: don't convert the code to CommonJS
(`require` / `module.exports`) and don't rename the files, because other packages import these exact paths.
Don't edit the files in `test/`.
