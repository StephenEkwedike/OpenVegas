"""Public-page search metadata; never derive canonical URLs from request headers."""
import os
import re
from html import escape
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, Response

ORIGIN = "https://openvegas.ai"
PAGES = {
    "/ui": ("OpenVegas | Terminal Games and AI Credits for Claude & Codex", "Play terminal games with $V, explore emotes, and fund separately billed AI requests in Claude Code and Codex. Games can win or lose credits."),
    "/ui/how-it-works": ("How OpenVegas Works | Claude Code and Codex", "Connect OpenVegas, fund your $V wallet, and choose terminal games, emotes or separately billed AI requests. Native subscription quotas stay unchanged."),
    "/ui/how-to-play": ("Terminal Game Guide | Poker, Blackjack & Roulette | OpenVegas", "Learn OpenVegas game rules, wagers and terminal controls for poker, blackjack and roulette. Understand payouts and risks before playing with $V."),
    "/ui/pricing": ("OpenVegas Pricing | AI Credits and Wallet Funding", "Explore OpenVegas wallet funding and AI usage pricing. Use $V for supported requests with separate billing, not a refill of host subscription limits."),
    "/ui/product": ("OpenVegas Product | A Wallet Layer for AI Usage", "Explore the OpenVegas wallet layer for AI usage: funding, credit balances and integrations for your existing coding workflow."),
    "/ui/faq": ("OpenVegas FAQ | Credits, Games and AI Billing", "Answers about OpenVegas credits, game outcomes, wallet funding and AI billing. Learn how $V differs from Claude and Codex subscription allowances."),
    "/ui/security": ("OpenVegas Security | Wallet and Account Controls", "Read about OpenVegas account and wallet controls, transaction visibility and security practices before connecting your coding tools."),
    "/ui/contact": ("Contact OpenVegas | Support and Partnerships", "Contact the OpenVegas team for support, product questions and partnerships around terminal entertainment and AI usage funding."),
    "/ui/call-to-action": ("Install OpenVegas | Get Started in Your Terminal", "Get started with OpenVegas in your terminal. Connect your account and explore games, emotes and optional $V-funded AI requests."),
}


def page_response(path: Path, url_path: str, headers: dict) -> HTMLResponse:
    text = path.read_text(encoding="utf-8")
    canonical_path = url_path.rstrip("/")
    if canonical_path in PAGES:
        title, description = PAGES[canonical_path]
        text = re.sub(r"<title>.*?</title>", "<title>" + escape(title) + "</title>", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<meta\s+name=[\"\x27]description[\"\x27][^>]*>', "", text, flags=re.IGNORECASE)
        url = ORIGIN + canonical_path
        tags = [f'<link rel="canonical" href="{url}" />']
        for name, value in (("description", description), ("twitter:card", "summary")):
            tags.append(f'<meta name="{name}" content="{escape(value, quote=True)}" />')
        for name, value in (("title", title), ("description", description), ("url", url), ("type", "website"), ("site_name", "OpenVegas"), ("image", ORIGIN + "/ui/assets/brand/openvegas-logo-circle.jpg")):
            tags.append(f'<meta property="og:{name}" content="{escape(value, quote=True)}" />')
        token = os.getenv("GOOGLE_SITE_VERIFICATION", "").strip()
        if token:
            tags.append(f'<meta name="google-site-verification" content="{escape(token, quote=True)}" />')
        text = text.replace("</head>", "\n".join(tags) + "\n</head>", 1)
    return HTMLResponse(text, headers=headers)


def install(app):
    @app.middleware("http")
    async def indexing_policy(request, call_next):
        response = await call_next(request)
        public = request.url.path.rstrip("/") in PAGES
        # Crawl private routes to see noindex; robots.txt is not access control.
        if (request.url.hostname != "openvegas.ai" or not public or response.status_code != 200) and not request.url.path.startswith("/ui/assets/"):
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @app.get("/sitemap.xml", include_in_schema=False)
    async def sitemap():
        entries = "".join(f"<url><loc>{ORIGIN}{path}</loc></url>" for path in PAGES)
        return Response('<?xml version="1.0" encoding="UTF-8"?>'
                        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + entries + '</urlset>',
                        media_type="application/xml")

    @app.get("/robots.txt", include_in_schema=False)
    async def robots(request: Request):
        policy = "Allow: /\nSitemap: " + ORIGIN + "/sitemap.xml" if request.url.hostname == "openvegas.ai" else "Disallow: /"
        return Response("User-agent: *\n" + policy + "\n", media_type="text/plain")
