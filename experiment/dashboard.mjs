// Mechanical browser verification. Credentials are supplied through environment.
import {createRequire} from 'node:module';
import {resolve} from 'node:path';
const require = createRequire(resolve(import.meta.dirname, '../ui/package.json'));
const puppeteer = require('puppeteer');
const [url, payload, screenshot] = process.argv.slice(2);
let browser;
try {
  browser = await puppeteer.launch({headless: true, args: ['--no-sandbox']});
  const page = await browser.newPage();
  await page.setViewport({width: 1280, height: 900});
  page.setDefaultTimeout(20000);
  await page.goto(url, {waitUntil: 'networkidle2'});
  await page.waitForSelector('#login-form .name input');
  await page.type('#login-form .name input', process.env.GOTIFY_PROBE_USER);
  await page.type('#login-form .password input', process.env.GOTIFY_PROBE_PASSWORD);
  await page.click('#login-form button.login');
  await page.waitForFunction(value => document.body.innerText.includes(value), {}, payload);
  await page.reload({waitUntil: 'networkidle2'});
  await page.waitForFunction(value => document.body.innerText.includes(value), {}, payload);
  await page.screenshot({path: screenshot, fullPage: true});
  console.log(JSON.stringify({dashboard_ok: true, reload_ok: true}));
} catch (e) {
  console.error(JSON.stringify({dashboard_ok: false, error_type: e.name}));
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
}
