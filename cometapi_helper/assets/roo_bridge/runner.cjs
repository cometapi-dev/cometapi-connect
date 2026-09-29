'use strict';
const fs = require('node:fs');
const path = require('node:path');
const store = require('./store.cjs');
const SAFE_ERRORS = new Set(['unsupported', 'active-task', 'uninitialized', 'unsupported-schema',
  'invalid-snapshot', 'invalid-profiles', 'conflict', 'rollback-failed', 'write-failed-restored']);
exports.run = async function () {
  const directory = process.env.COMETAPI_ROO_BRIDGE_DIRECTORY;
  if (!directory || !path.isAbsolute(directory)) throw new Error('Missing private bridge directory');
  const st = fs.lstatSync(directory);
  if (!st.isDirectory() || st.isSymbolicLink() || (process.platform !== 'win32' && (st.mode & 0o077))) throw new Error('Unsafe bridge directory');
  let result;
  try {
    const file = path.join(directory, 'request.json');
    const s = fs.lstatSync(file);
    if (!s.isFile() || s.isSymbolicLink() || s.nlink !== 1 || s.size > 4 * 1024 * 1024 || (process.platform !== 'win32' && (s.mode & 0o077)))
      throw new Error('Invalid private request');
    const request = JSON.parse(fs.readFileSync(file, 'utf8'));
    const vscode = require('vscode');
    const ext = vscode.extensions.getExtension('RooVeterinaryInc.roo-cline');
    if (!ext) throw new Error('unsupported');
    const api = await ext.activate();
    // Drain startup profile initialization before observing native state.
    if (typeof api?.sidebarProvider?.providerSettingsManager?.lock !== 'function') throw new Error('unsupported');
    await api.sidebarProvider.providerSettingsManager.lock(async () => {});
    if (request.operation === 'read') result = {ok: true, snapshot: await store.snapshot(api, ext.packageJSON.version)};
    else if (request.operation === 'write')
      result = {ok: true, snapshot: await store.write(api, ext.packageJSON.version, request.desired, request.expected)};
    else throw new Error('unsupported');
  } catch (error) {
    result = {ok: false, error: SAFE_ERRORS.has(error.message) ? error.message : 'bridge-failed'};
  }
  fs.writeFileSync(path.join(directory, 'response.tmp'), JSON.stringify(result), {mode: 0o600, flag: 'wx'});
  fs.renameSync(path.join(directory, 'response.tmp'), path.join(directory, 'response.json'));
};
