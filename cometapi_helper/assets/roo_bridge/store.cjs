'use strict';
// Check the native API shape rather than the extension release number.
// Use the extension's native SecretStorage, never Electron ciphertext editing.
const PROFILE_KEY = 'roo_cline_config_api_config';
const FIELDS = ['apiProvider', 'openAiBaseUrl', 'openAiModelId', 'openAiHeaders',
  'openAiUseAzure', 'openAiR1FormatEnabled', 'openAiStreamingEnabled',
  'openAiCustomModelInfo', 'includeMaxTokens', 'modelMaxTokens',
  'currentApiConfigName', 'listApiConfigMeta'];
const SECRET = 'openAiApiKey';
const clone = value => JSON.parse(JSON.stringify(value));
const canonical = value => JSON.stringify(value, function (_, v) {
  return v && typeof v === 'object' && !Array.isArray(v)
    ? Object.fromEntries(Object.keys(v).sort().map(k => [k, v[k]])) : v;
});
function checked(api, version) {
  const provider = api.sidebarProvider;
  const ctx = api.context;
  const proxy = provider?.contextProxy;
  const manager = provider?.providerSettingsManager;
  if (!ctx?.secrets || !ctx?.globalState ||
      typeof proxy?.setValue !== 'function' || typeof proxy?.storeSecret !== 'function' ||
      typeof manager?.lock !== 'function' || manager.secretsKey !== PROFILE_KEY ||
      typeof api.getCurrentTaskStack !== 'function') throw new Error('unsupported');
  if (api.getCurrentTaskStack().length) throw new Error('active-task');
  return {ctx, proxy, manager};
}
async function snapshot(api, version) {
  const {ctx} = checked(api, version);
  const profiles = await ctx.secrets.get(PROFILE_KEY);
  if (typeof profiles !== 'string') throw new Error('uninitialized');
  const parsed = JSON.parse(profiles);
  if (!parsed.apiConfigs || !parsed.currentApiConfigName || !parsed.modeApiConfigs)
    throw new Error('unsupported-schema');
  // v3.54.0 defaults to architect; native profile writes also reformat JSON.
  // Compare logical profile values rather than serializer whitespace.
  return {schema: 1, profiles: canonical(parsed), mode: ctx.globalState.get('mode') ?? 'architect',
    values: Object.fromEntries(FIELDS.map(k => {
      const value = clone(ctx.globalState.get(k) ?? null);
      // Roo rebuilds this derived list from profile keys when a task starts.
      if (k === 'listApiConfigMeta' && Array.isArray(value))
        value.sort((a,b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
      return [k, value];
    })),
    secret: await ctx.secrets.get(SECRET) ?? null};
}
function validate(value) {
  if (!value || value.schema !== 1 || typeof value.profiles !== 'string' ||
      typeof value.mode !== 'string' || !value.values ||
      canonical(Object.keys(value.values).sort()) !== canonical([...FIELDS].sort()) ||
      (value.secret !== null && typeof value.secret !== 'string')) throw new Error('invalid-snapshot');
  const profiles = JSON.parse(value.profiles);
  if (!profiles.apiConfigs || !profiles.modeApiConfigs || !profiles.currentApiConfigName)
    throw new Error('invalid-profiles');
}
async function write(api, version, desired, expected) {
  validate(desired); validate(expected);
  const {ctx, proxy, manager} = checked(api, version);
  // The caller owns the only Code process for this profile. Roo's own manager
  // lock serializes profile migration/configuration operations in this host.
  return manager.lock(async () => {
    const before = await snapshot(api, version);
    if (canonical(before) !== canonical(expected)) throw new Error('conflict');
    const save = async value => {
      await ctx.secrets.store(PROFILE_KEY, value.profiles);
      await proxy.storeSecret(SECRET, value.secret === null ? undefined : value.secret);
      for (const name of FIELDS)
        await proxy.setValue(name, value.values[name] === null ? undefined : value.values[name]);
    };
    try {
      await save(desired);
      if (canonical(await snapshot(api, version)) !== canonical(desired)) throw new Error('readback');
    } catch (_) {
      try { await save(before); } catch (_) { throw new Error('rollback-failed'); }
      throw new Error('write-failed-restored');
    }
    return desired;
  });
}
module.exports = {snapshot, write, validate, canonical, FIELDS};
