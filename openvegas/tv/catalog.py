"""Bundled, local-only programming. Empty slots are not live stations."""

from copy import deepcopy
from functools import lru_cache
from importlib import resources

from .schema import CHANNEL_IDS, MAX_FILE_BYTES, parse_episode

_SLOTS = (
    ("Odd Hours", "Original satirical dispatches from unlikely universes."),
    ("Small Wonders", "A reserved slot for small, original discoveries."),
    ("Night Desk", "A reserved slot for after-hours stories."),
    ("Field Notes", "A reserved slot for curious observations."),
    ("Paper Planet", "A reserved slot for handmade worlds."),
    ("Future Weather", "A reserved slot for speculative forecasts."),
    ("Simulated Sports", "A reserved slot for fictional, authored sports. No live games or odds."),
    ("Quiet Signals", "A reserved slot for gentle visual stories."),
    ("Workshop", "A reserved slot for creator experiments."),
    ("Elsewhere", "A reserved slot for imaginary travel."),
    ("Tiny Theatre", "A reserved slot for short original performances."),
    ("Open Frequency", "A reserved slot for future programming."),
)


@lru_cache(maxsize=1)
def _pilot() -> dict:
    resource = resources.files("openvegas.tv").joinpath("assets/bureau-pilot.json")
    with resource.open("rb") as source:
        return parse_episode(source.read(MAX_FILE_BYTES + 1))


def get_episode(episode_id: str) -> dict:
    """Return a detached normalized storyboard, or KeyError for an unknown id."""
    if episode_id != "bureau-pilot":
        raise KeyError(episode_id)
    return deepcopy(_pilot())


def catalog() -> dict:
    """JSON-ready public catalog. Reading it never bills, fetches, or installs."""
    return {
        "channels": [{
            "id": channel_id, "name": name, "description": description,
            "available": channel_id == "01",
            "availability": "playable" if channel_id == "01" else "no_shows",
            "episode_ids": ["bureau-pilot"] if channel_id == "01" else [],
            "message": "Original captioned animatic" if channel_id == "01" else "No shows yet",
        } for channel_id, (name, description) in zip(CHANNEL_IDS, _SLOTS)],
        "episodes": [get_episode("bureau-pilot")],
        "billing": {
            "enabled": False,
            "planned_models": ["pay_as_you_go", "monthly_all_channel_pass"],
            "pricing_status": "approved",
            "currency": "USD",
            "pay_as_you_go": {"price_usd": 0.75, "unit": "hour"},
            "monthly_all_channel_pass": {"price_usd": 20, "unit": "month", "unlimited": True},
        },
    }


def guidelines() -> dict:
    """JSON-ready authoring limits; advisory rights text is not certification."""
    return {
        "schema_version": 1,
        "format": "captioned_vector_animatic",
        "canvas": {"width": 320, "height": 180},
        "max_file_bytes": MAX_FILE_BYTES,
        "max_duration_seconds": 1800,
        "channel_ids": list(CHANNEL_IDS),
        "shape_kinds": ["rect", "ellipse", "text"],
        "required_episode_fields": [
            "schema_version", "id", "channel_id", "title", "description",
            "duration_seconds", "canvas", "scenes", "captions", "credits",
        ],
        "rules": [
            "Use original or properly authorized art and writing; verify rights before submission.",
            "Only JSON data: no URLs, media references, HTML, code, commands, or control characters.",
            "Scenes tile the timeline; captions are ordered and non-overlapping.",
            "Keyframes use scene-relative seconds and linearly interpolate x, y, opacity.",
            "Simulated Sports must be clearly fictional, not a real broadcast or betting service.",
            "Local apply saves a draft only; it never submits, publishes, installs, or bills.",
        ],
        "billing": catalog()["billing"],
    }
