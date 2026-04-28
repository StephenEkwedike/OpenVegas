# OpenVegas Investor Pitch Deck - Sources and Assumptions

## Deliverables
- `OpenVegas-investor-pitchdeck.pdf`
- `openvegas-investor-pitchdeck-source.zip`
- `html-deck/` (offline HTML deck bundle)
- `openvegas-investor-pitchdeck-html-offline.zip`

## Scope Implemented
- Added new founder intro slide: `/ui/slidedeck/00-founders.html`
- Added new VC-facing market strategy slide: `/ui/slidedeck/08b-market-opportunity.html`
- Updated deck navigation to a 14-slide sequence across all deck pages
- Updated routing allowlist in `server/main.py` for the new slides
- Updated founder deck link in `ui/assets/site.js` to start at `00-founders.html`

## Market and Adoption Sources
- Gartner AI spending forecast:
  - Claim: Worldwide AI spending forecast to total `$2.52T` in 2026.
  - URL: https://www.gartner.com/en/newsroom/press-releases/2026-1-15-gartner-says-worldwide-ai-spending-will-total-2-point-5-trillion-dollars-in-2026
  - As of: 2026-01-15
- Gartner enterprise app agent adoption:
  - Claim: `40%` of enterprise apps expected to feature task-specific AI agents by end of 2026 (from `<5%` in 2025).
  - URL: https://www.gartner.com/en/newsroom/press-releases/2025-08-26-gartner-predicts-40-percent-of-enterprise-apps-will-feature-task-specific-ai-agents-by-2026-up-from-less-than-5-percent-in-2025
  - As of: 2025-08-26
- Stack Overflow Developer Survey:
  - Claim: `76%` of developers are using or planning to use AI tools.
  - URL: https://survey.stackoverflow.co/2024/
  - As of: 2024 (survey publication cycle)

## Investor-facing Content Notes
- Internal placeholder metrics were removed from slide 10 investor copy.
- TAM/SAM/SOM caveat text intended for internal IC materials was removed from investor slides.

## Offline HTML Bundle Notes
- `html-deck/` is a self-contained, no-server deck package.
- Open `html-deck/index.html` locally to launch the deck from slide 1.
- Slide-to-slide links and keyboard navigation work using local files.
- Asset paths were rewritten from `/ui/assets/...` to local `assets/...`.
- Slide navigation paths were rewritten from `/ui/slidedeck/...` to local `*.html`.
- Offline `assets/deck.js` intentionally excludes the dynamic `/ui/assets/site.js` import so local viewing does not depend on backend routes.
