// Run with Playwright installed; no real app configuration or model requests.
const {chromium} = require('playwright');
const {spawn} = require('node:child_process');
const path = require('node:path');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const locales = process.env.TEST_LOCALES ? process.env.TEST_LOCALES.split(',') : ['en','zh-TW','ja','ko','fr','de','es','it','pt','ru','ar','th','vi','id','tr','pl'];
const dictionaries = Object.fromEntries([...new Set([...locales, 'en', 'fr'])].map(code => [code, JSON.parse(fs.readFileSync(path.join(__dirname,'../cometapi_helper/static/locales',code+'.json'),'utf8'))]));
const firstApp = JSON.parse(fs.readFileSync(path.join(__dirname,'../cometapi_helper/coding_catalog.json'),'utf8'))[0];
const child = spawn(process.env.PYTHON_BIN || 'python', [path.join(__dirname,'browser_i18n_server.py')]);
child.stderr.pipe(process.stderr);
const ready = new Promise((resolve,reject) => {
  let text = '';
  child.stdout.on('data', data => { text += data; if (text.includes('\n')) resolve(text.trim().split('\n')[0]); });
  child.on('error',reject); child.on('exit',code => reject(new Error('Fixture server exited: '+code)));
});
(async () => {
  let browser;
  try {
    const url = await ready;
    browser = await chromium.launch({headless:true, ...(process.env.BROWSER_CHANNEL ? {channel:process.env.BROWSER_CHANNEL} : {})});
    for (const code of locales) {
      const context = await browser.newContext({locale:code, viewport:{width:1280,height:900}});
      const page = await context.newPage(); const errors=[];
      const externalRequests=[];
      page.on('pageerror',error => errors.push(error.message));
      page.on('request',request => { if (new URL(request.url()).origin !== new URL(url).origin) externalRequests.push(request.url()); });
      await page.goto(url); await page.waitForSelector('.app-card');
      assert.equal(await page.getAttribute('html','lang'),code);
      assert.equal(await page.getAttribute('html','dir'),code==='ar'?'rtl':'ltr');
      assert.equal(await page.locator('#key-title').textContent(),dictionaries[code]['Start with your CometAPI key']);
      assert.equal(await page.locator('.app-description').first().textContent(),dictionaries[code][firstApp.description]);
      assert.equal(await page.locator('.app-detail').first().textContent(),dictionaries[code]['Details ↗']);
      await page.locator('.app-detail').first().click();
      assert.ok((await page.locator('#detail-body').textContent()).includes(dictionaries[code]['Documentation']));
      assert.equal(await page.locator('#detail-body ol li').first().textContent(),dictionaries[code][firstApp.instructions[0]]);
      await page.locator('#detail-done').click();
      await page.locator('#api-key').fill('sk-browser-fixture-only');
      await page.locator('.app-check').first().check();
      let finishPreview;
      const previewGate = new Promise(resolve => { finishPreview = resolve; });
      await page.route('**/api/preview', async route => { await previewGate; await route.continue(); });
      await page.locator('#review-button').click();
      assert.equal(await page.locator('#review-button').textContent(),dictionaries[code]['Preparing…']);
      assert.equal(await page.locator('#language-select').isDisabled(),true);
      finishPreview();
      await page.waitForSelector('#review-dialog[open]');
      assert.equal(await page.locator('#review-title').textContent(),dictionaries[code]['Review your changes']);
      assert.equal(await page.locator('.path').textContent(),'C:/fixture/settings.json');
      if (code === 'en') {
        await page.evaluate(() => { Object.defineProperty(navigator,'languages',{configurable:true,value:['fr-FR']}); window.dispatchEvent(new Event('languagechange')); });
        assert.equal(await page.locator('.fields').textContent(),dictionaries.fr['Settings: {fields}'].replace('{fields}','api_key'));
        await page.evaluate(() => { delete navigator.languages; window.dispatchEvent(new Event('languagechange')); });
      }
      await page.locator('#review-cancel').click();
      await page.locator('#language-select').selectOption(code==='fr'?'en':'fr');
      assert.equal(await page.locator('#api-key').inputValue(),'sk-browser-fixture-only');
      assert.equal(await page.locator('.app-check').first().isChecked(),true);
      const selected = code==='fr'?'en':'fr';
      assert.equal(await page.locator('#key-title').textContent(),dictionaries[selected]['Start with your CometAPI key']);
      await page.goto(url); await page.waitForSelector('.app-card');
      assert.equal(await page.getAttribute('html','lang'),selected);
      await page.locator('#language-select').selectOption('');
      assert.equal(await page.getAttribute('html','lang'),code);
      await page.locator('#api-key').fill('sk-browser-fixture-only');
      await page.locator('.app-check').first().check();
      await page.locator('#review-button').click();
      await page.locator('#apply-button').click();
      await page.waitForSelector('#global-notice:not([hidden])');
      assert.ok((await page.locator('#global-notice').textContent()).includes(dictionaries[code]['Configuration saved. Restart the selected apps and follow any app-specific notes.']));
      assert.equal(await page.locator('#api-key').inputValue(),'');
      assert.equal(await page.locator('.app-check:checked').count(),0);
      await page.locator('#nav-history').click(); await page.waitForSelector('.history-card');
      assert.equal(await page.locator('.history-actions .badge').textContent(),dictionaries[code]['Applied']);
      await page.locator('.history-actions button').click();
      assert.equal(await page.locator('#restore-title').textContent(),dictionaries[code]['Restore previous settings?']);
      await page.locator('#restore-confirm').click();
      await page.waitForFunction(() => !document.getElementById('restore-dialog').open);
      await page.waitForFunction(expected => document.querySelector('.history-actions .badge')?.textContent === expected,dictionaries[code]['Restored']);
      assert.equal(await page.locator('.history-actions .badge').textContent(),dictionaries[code]['Restored']);
      assert.equal(await page.locator('#global-notice').textContent(),dictionaries[code]['Original configuration files restored. Restart the affected apps.']);
      await page.locator('#nav-connect').click();
      assert.equal(await page.locator('#breadcrumb-page').textContent(),dictionaries[code]['Connect apps']);
      if (code === 'ar') {
        for (const width of [1050, 880, 800]) {
          await page.setViewportSize({width,height:900});
          assert.equal(await page.evaluate(() => document.querySelector('.main').getBoundingClientRect().right <= document.querySelector('.sidebar').getBoundingClientRect().left + 1),true,'RTL sidebar overlap at '+width);
        }
      }
      await page.setViewportSize({width:390,height:844});
      if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth+1)) {
        console.error(await page.evaluate(() => [...document.querySelectorAll('body *')].filter(element => { const box = element.getBoundingClientRect(); return box.width && (box.right > innerWidth+1 || box.left < -1); }).map(element => ({tag:element.tagName, id:element.id, class:element.className, text:element.textContent.slice(0,90)})).slice(0,15)));
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth+1),true,'mobile overflow '+code);
      assert.deepEqual(errors,[]);
      assert.deepEqual(externalRequests,[],'localization must remain offline');
      if (process.env.SCREENSHOT_DIR && ['zh-TW','ar','de'].includes(code)) {
        await page.locator('#nav-connect').click();
        await page.setViewportSize({width:1280,height:900});
        await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'i18n-'+code+'.png'),fullPage:false});
      }
      await context.close(); console.log(code+': browser flow passed');
    }
  } finally { if (browser) await browser.close(); child.kill(); }
})().catch(error => {console.error(error);process.exitCode=1;});
