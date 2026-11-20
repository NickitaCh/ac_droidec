"""Run the real Web Worker and compare exact server assessment on fixed inputs.

Requires the local Playwright/Chrome installation and prepared WASM assets.
No Discord, Telegram or game requests are made.
"""
from copy import deepcopy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import gzip
from pathlib import Path
import sys
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services import mod_optimizer_browser as browser, mod_optimizer_v2 as engine
from swgoh_comlink import StatCalc
from tests.test_mod_optimizer_v2 import mod, row


def example():
    unit = {'definitionId': 'VADER:SEVEN_STAR', 'currentRarity': 7, 'currentLevel': 85,
            'currentTier': 13, 'relic': {'currentTier': 3}, 'skill': [], 'equipment': []}
    mods = [mod(f'A{s}', s, 'A', speed=5) for s in range(1, 7)] + [mod(f'B{s}', s, 'B', speed=20) for s in range(1, 7)]
    return dict(members={'P': ['A', 'B']}, rows={'A': {'P': [row(value=190)]}, 'B': {'P': [row(value=170)]}},
                units={'A': unit, 'B': deepcopy(unit)}, tiers={}, mods=mods, names={'A': 'Первый', 'B': 'Второй'},
                locked=[], relic_mode='current', turn_order=[], deep_search=False)


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<!doctype html><html><body>Browser calculation verification</body></html>')
            return
        if not self.path.startswith('/static/'):
            self.send_error(404)
            return
        super().do_GET()

    def log_message(self, *args):
        pass


def main():
    from playwright.sync_api import sync_playwright
    calc_data = json.loads((ROOT / 'tests/fixtures/mod_optimizer_statcalc.json').read_text())
    calc = StatCalc(calc_data)
    prepared = example()
    # Use attainable real speed thresholds with a shared scarce pool.
    for base in prepared['rows']:
        prepared['rows'][base]['P'][0] = row(value=engine.final_stats(calc, prepared['units'][base], [])['Speed'] + 65)
    native = engine.calculate(calc, *(prepared[k] for k in ('members', 'rows', 'units', 'tiers', 'mods', 'names')))
    source = browser.python_source()
    if len(sys.argv) > 1:
        with gzip.open(sys.argv[1], 'rt', encoding='utf8') as stream:
            saved = json.load(stream)
        prepared = saved['payload']
        calc_data = prepared.pop('calculator')
        calc = StatCalc(calc_data)
        native = saved['reference']
        source = saved['source']
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(ROOT / 'web')))
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            chrome = playwright.chromium.launch(channel='chrome', headless=True)
            page = chrome.new_page()
            page.goto(f'http://127.0.0.1:{server.server_port}/')
            # Capture tracebacks only in this synthetic local test, never production.
            page.route('**/mod_optimizer_worker.js*', lambda route: route.fulfill(
                content_type='application/javascript', body=(ROOT / 'web/static/mod_optimizer_worker.js').read_text().replace(
                    "// Python tracebacks", "console.error(String(error));\n        // Python tracebacks")))
            errors = []
            page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
            outcome = page.evaluate("""data => new Promise(resolve => {
                const worker = new Worker('/static/mod_optimizer_worker.js');
                const timer = setTimeout(() => {worker.terminate(); resolve({type:'timeout'});}, 600000);
                worker.onmessage = ({data}) => {
                    if (data.type === 'complete' || data.type === 'error') {clearTimeout(timer); worker.terminate(); resolve(data);}
                };
                worker.onerror = event => {clearTimeout(timer); resolve({type:'error', message:event.message});};
                worker.postMessage(data);
            })""", {'source': source, 'payload': {**prepared, 'calculator': calc_data, '_verification': True}})
            chrome.close()
        if outcome['type'] != 'complete':
            raise AssertionError((outcome, errors))
        checked, passed, donors = browser.assess(calc, prepared, outcome['result']['allocation'])
        # Equivalent optimum can choose different IDs. Compare passes, actual
        # required values and resource exclusivity rather than arbitrary ties.
        assert passed == native[1], (passed, native[1])
        actual = {r['base_id']: (r['ok'], sorted((c['stat_name'], c['current']) for c in r['passed'] + r['failed'])) for r in checked}
        expected = {r['base_id']: (r['ok'], sorted((c['stat_name'], c['current']) for c in r['passed'] + r['failed']))
                    for r in outcome['result']['verification'][0] if r['owned']}
        actual = {base: values for base, values in actual.items() if base in expected}
        assert actual == expected, (actual, expected)
        print('Real Chrome worker matches TEST: exact StatCalc values, plate checks and exclusive allocation.')
    finally:
        server.shutdown()


if __name__ == '__main__':
    main()
