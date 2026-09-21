#!/usr/bin/env python3
"""Capture documentation PNGs from an isolated demo server (requires Playwright).

Start a separate server with KOTORI_DATA_DIR=/tmp/kotori-docs-data on port 8766.
Usage: python scripts/capture_docs.py http://127.0.0.1:8766/#DEMO_PROJECT_ID
Do not point this script at a personal project; only demo projects are accepted.
"""
import json
import struct
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
url = sys.argv[1]
parsed = urlsplit(url)
if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.fragment:
    raise SystemExit('Pass a localhost URL with a demo project ID.')
with urlopen(f'{parsed.scheme}://{parsed.netloc}/api/projects/{parsed.fragment}', timeout=10) as response:
    project = json.load(response)
if not project.get('demo'):
    raise SystemExit('Documentation captures require a demo project, not personal media.')

folder = ROOT / 'docs' / 'images'
folder.mkdir(parents=True, exist_ok=True)
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width': 1440, 'height': 1100}, device_scale_factor=2)
    page = context.new_page()
    page.goto(url, wait_until='networkidle')
    page.locator('#project-name').wait_for()
    page.evaluate('document.fonts.ready')
    page.locator('#video').evaluate('(v) => {v.currentTime = 1;}')
    page.wait_for_function("document.querySelector('#video').readyState >= 2 && !document.querySelector('#video').seeking")

    def shot(name, selector=None):
        options = dict(path=str(folder / f'{name}.png'), type='png', animations='disabled', scale='device')
        if selector:
            page.locator(selector).screenshot(**options)
        else:
            page.screenshot(**options)
        data = (folder / f'{name}.png').read_bytes()
        assert data[:8] == b'\x89PNG\r\n\x1a\n'
        print(name, struct.unpack('>II', data[16:24]), flush=True)

    def close():
        page.locator('dialog[open] .close-dialog').click()

    shot('overview')
    for name, button, dialog in [('asr','asr-button','asr-dialog'),
                                 ('translation','translate-button','translate-dialog'),
                                 ('speakers','speakers-button','speakers-dialog'),
                                 ('assembly','assembly-button','assembly-dialog'),
                                 ('clips','clips-button','clips-dialog'),
                                 ('models','system-button','system-dialog'),
                                 ('timing-offset','shift-subtitle','shift-dialog'),
                                 ('glossary','source-button','source-dialog')]:
        page.locator('#'+button).click()
        shot(name, 'dialog[open]')
        close()
    page.locator('#import-button').click()
    page.get_by_text('从 YouTube 链接下载并导入',exact=True).click()
    shot('import','dialog[open]')
    close()
    page.locator('#clips-button').click()
    page.locator('#clips-export').click()
    page.locator('#export-format').select_option('burn')
    shot('export','dialog[open]')
    close()
    page.locator('#zoom-select').select_option('15')
    page.get_by_role('button',name='01',exact=True).click()
    page.locator('.timeline-panel').scroll_into_view_if_needed()
    boxes = [page.locator(s).bounding_box() for s in ['.timeline-panel','.subtitles-panel']]
    x = min(b['x'] for b in boxes); y = min(b['y'] for b in boxes)
    width = max(b['x']+b['width'] for b in boxes)-x
    height = max(b['y']+b['height'] for b in boxes)-y
    page.screenshot(path=str(folder/'timing.png'), type='png', scale='device',
                    clip=dict(x=x,y=y,width=width,height=height), animations='disabled')
    print('timing', struct.unpack('>II',(folder/'timing.png').read_bytes()[16:24]), flush=True)
    browser.close()
