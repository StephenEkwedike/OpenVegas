from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.main import app

client = TestClient(app)


@pytest.mark.parametrize('path', ['/ui/missing', '/missing', '/ui/missing/nested', '/ui/tv'])
def test_browser_404(path, monkeypatch):
    monkeypatch.setenv('OPENVEGAS_TV_ENABLED', '0')
    r = client.get(path, headers={'Accept': 'text/html'})
    assert r.status_code == 404
    assert r.headers['content-type'].startswith('text/html')
    assert 'This page is' in r.text
    assert 'href="/ui"' in r.text and 'href="/ui/how-to-play"' in r.text
    assert r.headers['x-robots-tag'] == 'noindex, nofollow'
    assert r.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('path', ['/ui/missing', '/missing', '/ui/assets/missing.css', '/wallet/missing'])
def test_json_clients(path):
    r = client.get(path, headers={'Accept': 'application/json'})
    assert r.status_code == 404
    assert r.headers['content-type'].startswith('application/json')
    assert 'detail' in r.json()


@pytest.mark.parametrize('path', ['/ui/assets/missing.js', '/wallet/missing', '/sitemap-missing.xml'])
def test_machine_resources_stay_json(path):
    r = client.get(path, headers={'Accept': 'text/html'})
    assert r.status_code == 404
    assert r.headers['content-type'].startswith('application/json')


def test_no_url_reflection_or_soft_404():
    r = client.get('/ui/missing?secret=do-not-reflect', headers={'Accept': 'text/html'})
    assert 'do-not-reflect' not in r.text
    assert 'canonical' not in r.text
    assert client.get('/ui').status_code == 200
    assert '/ui/404' not in client.get('/sitemap.xml').text


def test_production_package_inputs():
    docker = Path('Dockerfile').read_text()
    ignore = Path('.dockerignore').read_text()
    assert 'COPY ui/ ./ui/' in docker
    assert 'COPY server/ ./server/' in docker
    assert '!ui/**/*.html' in ignore
    assert '!server/' in ignore
    assert Path('ui/404.html').is_file()
    assert Path('server/not_found.py').is_file()
