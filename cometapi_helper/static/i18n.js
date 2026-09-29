/* Offline interface localization. Configuration values and diagnostics are never sent out. */
(() => {
  'use strict';
  const languages = {en:'English', 'zh-TW':'繁體中文', ja:'日本語', ko:'한국어', fr:'Français', de:'Deutsch', es:'Español', it:'Italiano', pt:'Português', ru:'Русский', ar:'العربية', th:'ไทย', vi:'Tiếng Việt', id:'Bahasa Indonesia', tr:'Türkçe', pl:'Polski'};
  function chooseLocale(preferences = [], saved = '') {
    if (Object.hasOwn(languages, saved)) return saved;
    for (const preference of preferences) {
      const tag = String(preference).toLowerCase().replaceAll('_', '-');
      const base = tag.split('-')[0];
      if (base === 'zh') return 'zh-TW';
      if (Object.hasOwn(languages, base)) return base;
    }
    return 'en';
  }
  function translate(source, dictionary, values = {}) {
    const translated = Object.hasOwn(dictionary, source) ? dictionary[source] : source;
    return translated.replace(/\{(\w+)\}/g, (match, key) => Object.hasOwn(values, key) ? String(values[key]) : match);
  }
  function start(catalogs) {
    const preferences = () => navigator.languages?.length ? navigator.languages : [navigator.language || 'en'];
    let saved = '';
    try { saved = document.cookie.split('; ').find(item => item.startsWith('cometapi_locale='))?.split('=')[1] || ''; } catch {}
    let locale = chooseLocale(preferences(), saved);
    const records = new WeakMap();
    const select = document.getElementById('language-select');
    const automatic = document.createElement('option');
    automatic.value = ''; automatic.textContent = 'Browser language'; select.append(automatic);
    for (const [code, name] of Object.entries(languages)) {
      const option = document.createElement('option'); option.value = code; option.textContent = name; option.lang = code; select.append(option);
    }
    select.value = Object.hasOwn(languages, saved) ? saved : '';
    function t(source, values) { return translate(source, catalogs[locale] || {}, values); }
    function update(owner, key, current, write) {
      const entries = records.get(owner) || {};
      const previous = entries[key];
      const source = previous && previous.rendered === current ? previous.source : current;
      const leading = source.match(/^\s*/)[0], trailing = source.match(/\s*$/)[0];
      const rendered = source.trim() ? leading + t(source.trim()) + trailing : source;
      entries[key] = {source, rendered}; records.set(owner, entries);
      if (rendered !== current) write(rendered);
    }
    function refresh() {
      document.documentElement.lang = locale;
      document.documentElement.dir = locale === 'ar' ? 'rtl' : 'ltr';
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const text = walker.currentNode;
        if (text.parentElement.closest('script,style,code,textarea,option,.path,.app-name,#detail-title,#restore-description,#app-version')) continue;
        update(text, 'text', text.nodeValue, value => { text.nodeValue = value; });
      }
      for (const element of document.querySelectorAll('[aria-label],[placeholder],[title]')) {
        for (const attribute of ['aria-label','placeholder','title']) {
          if (element.hasAttribute(attribute)) update(element, attribute, element.getAttribute(attribute), value => element.setAttribute(attribute, value));
        }
      }
      automatic.textContent = t('Browser language');
    }
    select.addEventListener('change', () => {
      saved = select.value;
      try { document.cookie = 'cometapi_locale=' + saved + '; Path=/; SameSite=Strict; Max-Age=' + (saved ? '31536000' : '0'); } catch {}
      locale = chooseLocale(preferences(), saved);
      refresh();
      document.dispatchEvent(new Event('comet-language-change'));
    });
    window.addEventListener('languagechange', () => {
      if (!select.value) { locale = chooseLocale(preferences()); refresh(); document.dispatchEvent(new Event('comet-language-change')); }
    });
    refresh();
    return {t, refresh, get locale() { return locale; }};
  }
  globalThis.CometI18n = {languages, chooseLocale, translate, start};
})();
