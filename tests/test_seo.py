import re
from html import unescape
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from server.main import UI_DIR, app
from server.seo import ORIGIN, PAGES

client = TestClient(app, base_url=ORIGIN)

@pytest.mark.parametrize('path', PAGES)
def test_public_metadata_and_body(path):
    response = client.get(path)
    assert response.status_code == 200
    assert 'X-Robots-Tag' not in response.headers
    html = response.text
    assert html.count('<title>') == 1
    assert unescape(re.search(r'<title>(.*?)</title>', html)[1]) == PAGES[path][0]
    assert len(re.findall(r'name="description"', html)) == 1
    assert f'rel="canonical" href="{ORIGIN}{path}"' in html
    assert f'property="og:url" content="{ORIGIN}{path}"' in html
    original = (UI_DIR / ('index.html' if path == '/ui' else path.split('/')[-1] + '.html')).read_text()
    assert html.split('<body', 1)[1] == original.split('<body', 1)[1]


def test_sitemap_and_public_robots():
    response = client.get('/sitemap.xml')
    root = ElementTree.fromstring(response.text)
    urls = [node.text for node in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
    assert urls == [ORIGIN + p for p in PAGES]
    assert len(urls) == len(set(urls))
    assert 'Sitemap: https://openvegas.ai/sitemap.xml' in client.get('/robots.txt').text
    assert 'Allow: /' in client.get('/robots.txt').text


@pytest.mark.parametrize('path', ['/ui/login', '/ui/balance', '/ui/profile', '/ui/transactions', '/ui/business', '/ui/connect', '/ui/topup/example', '/health', '/ui/tv', '/not-found'])
def test_private_and_unreleased_noindex(path, monkeypatch):
    monkeypatch.setenv('OPENVEGAS_TV_ENABLED', '0')
    response = client.get(path)
    assert response.headers['X-Robots-Tag'] == 'noindex, nofollow'
    assert path not in PAGES


def test_preview_and_host_injection():
    local = TestClient(app, base_url='http://127.0.0.1:8000')
    assert 'Disallow: /' in local.get('/robots.txt').text
    assert local.get('/ui').headers['X-Robots-Tag'] == 'noindex, nofollow'
    hostile = TestClient(app, base_url='https://attacker.example')
    html = hostile.get('/ui?secret=abc').text
    assert 'attacker.example' not in html
    assert 'rel="canonical" href="https://openvegas.ai/ui"' in html
    assert client.get('/ui/').text == client.get('/ui').text


def test_verification_escape(monkeypatch):
    monkeypatch.setenv('GOOGLE_SITE_VERIFICATION', '\"><script>alert(1)</script>')
    html = client.get('/ui').text
    assert 'content="&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in html
    assert '<script>alert(1)</script>' not in html


def test_unique_titles_and_descriptions():
    assert len({v[0] for v in PAGES.values()}) == len(PAGES)
    assert len({v[1] for v in PAGES.values()}) == len(PAGES)
