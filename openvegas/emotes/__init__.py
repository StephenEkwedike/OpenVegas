"""Authored emote runtime. No assets, entitlements, hooks, or CLI auto-registration."""

from .bridge import ChatEmoteBridge, TurnToken
from .controller import EmoteController, State
from .events import Event, Phase
from .manifest import (
    Animation,
    LoadedPack,
    Manifest,
    PackError,
    load_pack,
    validate_manifest,
)
from .render import fit_frame, prompt_toolkit_fragments, rich_frame
from .spool import EventSpool, publish_event

__all__ = [
    "Animation",
    "ChatEmoteBridge",
    "EmoteController",
    "Event",
    "EventSpool",
    "LoadedPack",
    "Manifest",
    "PackError",
    "Phase",
    "State",
    "TurnToken",
    "fit_frame",
    "load_pack",
    "prompt_toolkit_fragments",
    "publish_event",
    "rich_frame",
    "validate_manifest",
]
