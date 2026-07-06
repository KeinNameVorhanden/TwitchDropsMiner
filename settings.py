from __future__ import annotations

import fnmatch
from typing import Any, TypedDict, TYPE_CHECKING

from yarl import URL

from utils import json_load, json_save
from constants import MAX_INT, SETTINGS_PATH, DEFAULT_LANG, PriorityMode

if TYPE_CHECKING:
    from main import ParsedArgs


# Glob metacharacters — presence of any of these in a priority/exclude entry
# turns it into a pattern instead of a literal game name.
_PATTERN_CHARS = ("*", "?", "[")

# Bounds for the (config-only) ``list_contrast`` setting. 1 = the lightest tint
# pair (current default — looks correct on most displays); 5 = the most
# pronounced (helps when the lighter tints are washed out by display gamma /
# overlay filters / monochrome modes).
LIST_CONTRAST_MIN = 1
LIST_CONTRAST_MAX = 5


class SettingsFile(TypedDict):
    proxy: URL
    language: str
    dark_mode: bool
    exclude: set[str]
    priority: list[str]
    autostart_tray: bool
    connection_quality: int
    tray_notifications: bool
    enable_badges_emotes: bool
    available_drops_check: bool
    priority_mode: PriorityMode
    close_on_error: bool
    list_contrast: int


default_settings: SettingsFile = {
    "proxy": URL(),
    "priority": [],
    "exclude": set(),
    "dark_mode": False,
    "autostart_tray": False,
    "connection_quality": 1,
    "language": DEFAULT_LANG,
    "tray_notifications": True,
    "enable_badges_emotes": False,
    "available_drops_check": False,
    "priority_mode": PriorityMode.PRIORITY_ONLY,
    "close_on_error": True,
    "list_contrast": LIST_CONTRAST_MIN,
}


class Settings:
    # from args
    log: bool
    tray: bool
    dump: bool
    # args properties
    debug_ws: int
    debug_gql: int
    logging_level: int
    # from settings file
    proxy: URL
    language: str
    dark_mode: bool
    exclude: set[str]
    priority: list[str]
    autostart_tray: bool
    connection_quality: int
    tray_notifications: bool
    enable_badges_emotes: bool
    available_drops_check: bool
    priority_mode: PriorityMode

    PASSTHROUGH = ("_settings", "_args", "_altered")

    def __init__(self, args: ParsedArgs):
        self._settings: SettingsFile = json_load(SETTINGS_PATH, default_settings)
        self._args: ParsedArgs = args
        self._altered: bool = False

    # default logic of reading settings is to check args first, then the settings file
    def __getattr__(self, name: str, /) -> Any:
        if name in self.PASSTHROUGH:
            # passthrough
            return getattr(super(), name)
        elif hasattr(self._args, name):
            return getattr(self._args, name)
        elif name in self._settings:
            return self._settings[name]  # type: ignore[literal-required]
        return getattr(super(), name)

    def __setattr__(self, name: str, value: Any, /) -> None:
        if name in self.PASSTHROUGH:
            # passthrough
            return super().__setattr__(name, value)
        elif name in self._settings:
            self._settings[name] = value  # type: ignore[literal-required]
            self._altered = True
            return
        raise TypeError(f"{name} is missing a custom setter")

    def __delattr__(self, name: str, /) -> None:
        raise RuntimeError("settings can't be deleted")

    def alter(self) -> None:
        self._altered = True

    def save(self, *, force: bool = False) -> None:
        if self._altered or force:
            json_save(SETTINGS_PATH, self._settings, sort=True)

    # ------------------------------------------------------------------
    # Priority / exclude matching helpers
    #
    # Entries in ``priority`` and ``exclude`` are normally exact game names,
    # but any entry containing a glob metacharacter (``*``, ``?``, ``[``)
    # is treated as an :mod:`fnmatch` pattern (case-sensitive). A single
    # pattern can therefore stand in for a whole family of titles, e.g.
    # ``"EA Sports FC *"`` covers FC 24, FC 25, FC 26 without re-editing
    # the list every season.
    # ------------------------------------------------------------------
    @staticmethod
    def is_pattern_entry(entry: str) -> bool:
        return any(c in entry for c in _PATTERN_CHARS)

    @staticmethod
    def match_entry(entry: str, name: str) -> bool:
        if Settings.is_pattern_entry(entry):
            return fnmatch.fnmatchcase(name, entry)
        return entry == name

    def priority_index(self, name: str) -> int:
        """Index of the first priority entry matching ``name``, or MAX_INT."""
        for idx, entry in enumerate(self._settings["priority"]):
            if self.match_entry(entry, name):
                return idx
        return MAX_INT

    def has_priority(self, name: str) -> bool:
        return self.priority_index(name) < MAX_INT

    def is_excluded(self, name: str) -> bool:
        return any(self.match_entry(entry, name) for entry in self._settings["exclude"])
