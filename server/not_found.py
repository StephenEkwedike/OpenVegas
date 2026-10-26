"""Browser-friendly missing pages without changing API error contracts."""
from pathlib import Path

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException


def install(app, ui_dir: Path):
    @app.exception_handler(HTTPException)
    async def browser_not_found(request: Request, exc: HTTPException):
        path = request.url.path
        # /ui/assets is a machine resource even when opened in a browser.
        page_request = path == '/ui' or path.startswith('/ui/') or path == '/' or not path.strip('/').count('/')
        machine_resource = path.startswith('/ui/assets/') or '.' in path.rsplit('/', 1)[-1]
        accepts_html = 'text/html' in request.headers.get('accept', '').lower()
        if exc.status_code == 404 and request.method in {'GET', 'HEAD'} and accepts_html and page_request and not machine_resource:
            return FileResponse(ui_dir / '404.html', status_code=404, headers={
                'Cache-Control': 'no-store', 'Vary': 'Accept',
                'X-Robots-Tag': 'noindex, nofollow',
            })
        return await http_exception_handler(request, exc)
