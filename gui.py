from __future__ import annotations

import os
import re
import sys
import shlex
import ctypes
import asyncio
import logging
import plistlib
import tkinter as tk
from pathlib import Path
from collections import abc
from textwrap import dedent
from math import log10, ceil
from dataclasses import dataclass
from tkinter.font import Font, nametofont
from functools import partial, cached_property
from datetime import datetime, timedelta, timezone
from tkinter import Tk, ttk, StringVar, DoubleVar, IntVar
from typing import Any, Union, Tuple, TypedDict, NoReturn, Generic, TYPE_CHECKING

import pystray
from yarl import URL
from PIL.ImageTk import PhotoImage
from PIL import Image as Image_module

if sys.platform == "win32":
    import win32api
    import win32con
    import win32gui

if sys.platform == "darwin":
    import AppKit

from translate import _
from cache import ImageCache
from exceptions import MinerException, ExitRequest
from settings import Settings, LIST_CONTRAST_MIN
from utils import resource_path, set_root_icon, webopen, task_wrapper, Game, _T
from constants import (
    MAX_INT,
    SELF_PATH,
    IS_PACKAGED,
    SCRIPTS_PATH,
    WINDOW_TITLE,
    LOGGING_LEVELS,
    MAX_WEBSOCKETS,
    WS_TOPICS_LIMIT,
    OUTPUT_FORMATTER,
    State,
    PriorityMode,
)
if sys.platform == "win32":
    from registry import RegistryKey, ValueType, ValueNotFound


if TYPE_CHECKING:
    from twitch import Twitch
    from channel import Channel
    from inventory import DropsCampaign, TimedDrop


logger = logging.getLogger("TwitchDrops")
TK_PADDING = Union[int, Tuple[int, int], Tuple[int, int, int], Tuple[int, int, int, int]]
DIGITS = ceil(log10(WS_TOPICS_LIMIT))


######################
# GUI ELEMENTS START #
######################


class _TKOutputHandler(logging.Handler):
    def __init__(self, output: GUIManager):
        super().__init__()
        self._output = output

    def emit(self, record):
        self._output.print(self.format(record))


class PlaceholderEntry(ttk.Entry):
    def __init__(
        self,
        master: ttk.Widget,
        *args: Any,
        placeholder: str,
        prefill: str = '',
        placeholdercolor: str = "grey60",
        **kwargs: Any,
    ):
        super().__init__(master, *args, **kwargs)
        self._prefill: str = prefill
        self._show: str = kwargs.get("show", '')
        self._text_color: str = kwargs.get("foreground", '')
        self._ph_color: str = placeholdercolor
        self._ph_text: str = placeholder
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        if isinstance(self, ttk.Combobox):
            # only bind this for comboboxes
            self.bind("<<ComboboxSelected>>", self._combobox_select)
        self._ph: bool = False
        self._insert_placeholder()

    def _insert_placeholder(self) -> None:
        """
        If we're empty, insert a placeholder, set placeholder text color and make sure it's shown.
        If we're not empty, leave the box as is.
        """
        if not super().get():
            self._ph = True
            super().config(foreground=self._ph_color, show='')
            super().insert("end", self._ph_text)

    def _remove_placeholder(self) -> None:
        """
        If we've had a placeholder, clear the box and set normal text colour and show.
        """
        if self._ph:
            self._ph = False
            super().delete(0, "end")
            super().config(foreground=self._text_color, show=self._show)
            if self._prefill:
                super().insert("end", self._prefill)

    def _focus_in(self, event: tk.Event[PlaceholderEntry]) -> None:
        self._remove_placeholder()

    def _focus_out(self, event: tk.Event[PlaceholderEntry]) -> None:
        self._insert_placeholder()

    def _combobox_select(self, event: tk.Event[PlaceholderEntry]):
        # combobox clears and inserts the selected value internally, bypassing the insert method.
        # disable the placeholder flag and set the color here, so _focus_in doesn't clear the entry
        self._ph = False
        super().config(foreground=self._text_color, show=self._show)

    def _store_option(
        self, options: dict[str, object], name: str, attr: str, *, remove: bool = False
    ) -> None:
        if name in options:
            if remove:
                value = options.pop(name)
            else:
                value = options[name]
            setattr(self, attr, value)

    def configure(self, *args: Any, **kwargs: Any) -> Any:
        options: dict[str, Any] = {}
        if args and args[0] is not None:
            options.update(args[0])
        if kwargs:
            options.update(kwargs)
        self._store_option(options, "show", "_show")
        self._store_option(options, "foreground", "_text_color")
        self._store_option(options, "placeholder", "_ph_text", remove=True)
        self._store_option(options, "prefill", "_prefill", remove=True)
        self._store_option(options, "placeholdercolor", "_ph_color", remove=True)
        return super().configure(**kwargs)

    def config(self, *args: Any, **kwargs: Any) -> Any:
        # because 'config = configure' makes mypy complain
        self.configure(*args, **kwargs)

    def get(self) -> str:
        if self._ph:
            return ''
        return super().get()

    def insert(self, index: str | int, content: str) -> None:
        # when inserting into the entry externally, disable the placeholder flag
        if not content:
            # if an empty string was passed in
            return
        self._remove_placeholder()
        super().insert(index, content)

    def delete(self, first: str | int, last: str | int | None = None) -> None:
        super().delete(first, last)
        self._insert_placeholder()

    def clear(self) -> None:
        self.delete(0, "end")

    def replace(self, content: str) -> None:
        super().delete(0, "end")
        self.insert("end", content)


class PlaceholderCombobox(PlaceholderEntry, ttk.Combobox):
    pass


class PaddedListbox(tk.Listbox):
    def __init__(self, master: ttk.Widget, *args, padding: TK_PADDING = (0, 0, 0, 0), **kwargs):
        # we place the listbox inside a frame with the same background
        # this means we need to forward the 'grid' method to the frame, not the listbox
        self._frame = tk.Frame(master)
        self._frame.rowconfigure(0, weight=1)
        self._frame.columnconfigure(0, weight=1)
        super().__init__(self._frame)
        # mimic default listbox style with sunken relief and borderwidth of 1
        if "relief" not in kwargs:
            kwargs["relief"] = "sunken"
        if "borderwidth" not in kwargs:
            kwargs["borderwidth"] = 1
        self.configure(*args, padding=padding, **kwargs)

    def grid(self, *args, **kwargs):
        return self._frame.grid(*args, **kwargs)

    def grid_remove(self) -> None:
        return self._frame.grid_remove()

    def grid_info(self) -> tk._GridInfo:
        return self._frame.grid_info()

    def grid_forget(self) -> None:
        return self._frame.grid_forget()

    def configure(self, *args: Any, **kwargs: Any) -> Any:
        options: dict[str, Any] = {}
        if args and args[0] is not None:
            options.update(args[0])
        if kwargs:
            options.update(kwargs)
        # NOTE on processed options:
        # • relief is applied to the frame only
        # • background is copied, so that both listbox and frame change color
        # • borderwidth is applied to the frame only
        # bg is folded into background for easier processing
        if "bg" in options:
            options["background"] = options.pop("bg")
        frame_options = {}
        if "relief" in options:
            frame_options["relief"] = options.pop("relief")
        if "background" in options:
            frame_options["background"] = options["background"]  # copy
        if "borderwidth" in options:
            frame_options["borderwidth"] = options.pop("borderwidth")
        self._frame.configure(frame_options)
        # update padding
        if "padding" in options:
            padding: TK_PADDING = options.pop("padding")
            padx1: tk._ScreenUnits
            padx2: tk._ScreenUnits
            pady1: tk._ScreenUnits
            pady2: tk._ScreenUnits
            if not isinstance(padding, tuple) or len(padding) == 1:
                if isinstance(padding, tuple):
                    padding = padding[0]
                padx1 = padx2 = pady1 = pady2 = padding
            elif len(padding) == 2:
                padx1 = padx2 = padding[0]
                pady1 = pady2 = padding[1]
            elif len(padding) == 3:
                padx1, padx2 = padding[0], padding[1]
                pady1 = pady2 = padding[2]
            else:
                padx1, padx2, pady1, pady2 = padding
            super().grid(column=0, row=0, padx=(padx1, padx2), pady=(pady1, pady2), sticky="nsew")
        else:
            super().grid(column=0, row=0, sticky="nsew")
        # listbox uses flat relief to blend in with the inside of the frame
        options["relief"] = "flat"
        return super().configure(options)

    def config(self, *args: Any, **kwargs: Any) -> Any:
        # because 'config = configure' makes mypy complain
        self.configure(*args, **kwargs)

    def configure_theme(self, *, bg: str, fg: str, sel_bg: str, sel_fg: str):
        # Apply basic colors for dark/light mode
        super().config(bg=bg, fg=fg, selectbackground=sel_bg, selectforeground=sel_fg)


class MouseOverLabel(ttk.Label):
    def __init__(self, *args, alt_text: str = '', reverse: bool = False, **kwargs) -> None:
        self._org_text: str = ''
        self._alt_text: str = ''
        self._alt_reverse: bool = reverse
        self._bind_enter: str | None = None
        self._bind_leave: str | None = None
        super().__init__(*args, **kwargs)
        self.configure(text=kwargs.get("text", ''), alt_text=alt_text, reverse=reverse)

    def _set_org(self, event: tk.Event[MouseOverLabel]):
        super().config(text=self._org_text)

    def _set_alt(self, event: tk.Event[MouseOverLabel]):
        super().config(text=self._alt_text)

    def configure(self, *args: Any, **kwargs: Any) -> Any:
        options: dict[str, Any] = {}
        if args and args[0] is not None:
            options.update(args[0])
        if kwargs:
            options.update(kwargs)
        applicable_options: set[str] = set((
            "text",
            "reverse",
            "alt_text",
        ))
        if applicable_options.intersection(options.keys()):
            # we need to pop some options, because they can't be passed down to the label,
            # as that will result in an error later down the line
            events_change: bool = False
            if "text" in options:
                if bool(self._org_text) != bool(options["text"]):
                    events_change = True
                self._org_text = options["text"]
            if "alt_text" in options:
                if bool(self._alt_text) != bool(options["alt_text"]):
                    events_change = True
                self._alt_text = options.pop("alt_text")
            if "reverse" in options:
                if bool(self._alt_reverse) != bool(options["reverse"]):
                    events_change = True
                self._alt_reverse = options.pop("reverse")
            if self._org_text and not self._alt_text:
                options["text"] = self._org_text
            elif (not self._org_text or self._alt_reverse) and self._alt_text:
                options["text"] = self._alt_text
            if events_change:
                if self._bind_enter is not None:
                    self.unbind(self._bind_enter)
                    self._bind_enter = None
                if self._bind_leave is not None:
                    self.unbind(self._bind_leave)
                    self._bind_leave = None
                if self._org_text and self._alt_text:
                    if self._alt_reverse:
                        self._bind_enter = self.bind("<Enter>", self._set_org)
                        self._bind_leave = self.bind("<Leave>", self._set_alt)
                    else:
                        self._bind_enter = self.bind("<Enter>", self._set_alt)
                        self._bind_leave = self.bind("<Leave>", self._set_org)
        return super().configure(options)

    def config(self, *args: Any, **kwargs: Any) -> Any:
        # because 'config = configure' makes mypy complain
        self.configure(*args, **kwargs)


class LinkLabel(ttk.Label):
    def __init__(self, *args, link: str, **kwargs) -> None:
        self._link: str = link
        # style provides font and foreground color
        if "style" not in kwargs:
            kwargs["style"] = "Link.TLabel"
        elif not kwargs["style"]:
            super().__init__(*args, **kwargs)
            return
        if "cursor" not in kwargs:
            kwargs["cursor"] = "hand2"
        if "padding" not in kwargs:
            # W, N, E, S
            kwargs["padding"] = (0, 2, 0, 2)
        super().__init__(*args, **kwargs)
        self.bind("<ButtonRelease-1>", lambda e: webopen(self._link))


class SelectMenu(tk.Menubutton, Generic[_T]):
    def __init__(
        self,
        master: tk.Misc,
        *args: Any,
        tearoff: bool = False,
        options: dict[str, _T],
        command: abc.Callable[[_T], Any] | None = None,
        default: str | None = None,
        relief: tk._Relief = "solid",
        background: str = "white",
        **kwargs: Any,
    ):
        width = max((len(k) for k in options.keys()), default=20)
        super().__init__(
            master, *args, background=background, relief=relief, width=width, **kwargs
        )
        self._menu_options: dict[str, _T] = options
        self._command = command
        self.menu = tk.Menu(self, tearoff=tearoff)
        self.config(menu=self.menu)
        for name in options.keys():
            self.menu.add_command(label=name, command=partial(self._select, name))
        if default is not None and default in self._menu_options:
            self.config(text=default)

    def _select(self, option: str) -> None:
        self.config(text=option)
        if self._command is not None:
            self._command(self._menu_options[option])

    def get(self) -> _T:
        return self._menu_options[self.cget("text")]


class SelectCombobox(ttk.Combobox):
    def __init__(
        self,
        master: tk.Misc,
        *args,
        width_offset: int = 0,
        width: int | None = None,
        textvariable: tk.StringVar,
        values: list[str] | tuple[str, ...],
        command: abc.Callable[[tk.Event[SelectCombobox]], None] | None = None,
        **kwargs,
    ) -> None:
        if width is None:
            font = Font(master, ttk.Style().lookup("TCombobox", "font"))
            # font.measure returns width in pixels, using '0' as the average character,
            # which is 6 pixels wide. We can convert it to width in characters by dividing.
            width = max(font.measure(v) // 6 + 1 for v in values)
        width += width_offset
        super().__init__(
            master,
            *args,
            width=width,
            values=values,
            state="readonly",
            exportselection=False,
            textvariable=textvariable,
            **kwargs,
        )
        if command is not None:
            self.bind("<<ComboboxSelected>>", command)


###########################################
# GUI ELEMENTS END / GUI DEFINITION START #
###########################################


class StatusBar:
    def __init__(self, manager: GUIManager, master: ttk.Widget):
        frame = ttk.LabelFrame(master, text=_("gui", "status", "name"), padding=(4, 0, 4, 4))
        frame.grid(column=0, row=0, columnspan=3, sticky="nsew", padx=2)
        self._label = ttk.Label(frame)
        self._label.grid(column=0, row=0, sticky="nsew")

    def update(self, text: str):
        self._label.config(text=text)

    def clear(self):
        self._label.config(text='')


class _WSEntry(TypedDict):
    status: str
    topics: int


class WebsocketStatus:
    def __init__(self, manager: GUIManager, master: ttk.Widget):
        frame = ttk.LabelFrame(master, text=_("gui", "websocket", "name"), padding=(4, 0, 4, 4))
        frame.grid(column=0, row=1, sticky="nsew", padx=2)
        self._status_var = StringVar(frame)
        self._topics_var = StringVar(frame)
        ttk.Label(
            frame,
            text='\n'.join(
                _("gui", "websocket", "websocket").format(id=i)
                for i in range(1, MAX_WEBSOCKETS + 1)
            ),
            style="MS.TLabel",
        ).grid(column=0, row=0)
        ttk.Label(
            frame,
            textvariable=self._status_var,
            width=16,
            justify="left",
            style="MS.TLabel",
        ).grid(column=1, row=0)
        ttk.Label(
            frame,
            textvariable=self._topics_var,
            width=(DIGITS * 2 + 1),
            justify="right",
            style="MS.TLabel",
        ).grid(column=2, row=0)
        self._items: dict[int, _WSEntry | None] = {i: None for i in range(MAX_WEBSOCKETS)}
        self._update()

    def update(self, idx: int, status: str | None = None, topics: int | None = None):
        if status is None and topics is None:
            raise TypeError("You need to provide at least one of: status, topics")
        entry = self._items.get(idx)
        if entry is None:
            entry = self._items[idx] = _WSEntry(
                status=_("gui", "websocket", "disconnected"), topics=0
            )
        if status is not None:
            entry["status"] = status
        if topics is not None:
            entry["topics"] = topics
        self._update()

    def remove(self, idx: int):
        if idx in self._items:
            del self._items[idx]
            self._update()

    def _update(self):
        status_lines: list[str] = []
        topic_lines: list[str] = []
        for idx in range(MAX_WEBSOCKETS):
            if (item := self._items.get(idx)) is None:
                status_lines.append('')
                topic_lines.append('')
            else:
                status_lines.append(item["status"])
                topic_lines.append(f"{item['topics']:>{DIGITS}}/{WS_TOPICS_LIMIT}")
        self._status_var.set('\n'.join(status_lines))
        self._topics_var.set('\n'.join(topic_lines))


@dataclass
class LoginData:
    username: str
    password: str
    token: str


class LoginForm:
    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._manager = manager
        self._var = StringVar(master)
        frame = ttk.LabelFrame(master, text=_("gui", "login", "name"), padding=(4, 0, 4, 4))
        frame.grid(column=1, row=1, sticky="nsew", padx=2)
        frame.columnconfigure(0, weight=2)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(4, weight=1)
        ttk.Label(frame, text=_("gui", "login", "labels")).grid(column=0, row=0)
        ttk.Label(frame, textvariable=self._var, justify="center").grid(column=1, row=0)
        self._login_entry = PlaceholderEntry(frame, placeholder=_("gui", "login", "username"))
        # self._login_entry.grid(column=0, row=1, columnspan=2)
        self._pass_entry = PlaceholderEntry(
            frame, placeholder=_("gui", "login", "password"), show='•'
        )
        # self._pass_entry.grid(column=0, row=2, columnspan=2)
        self._token_entry = PlaceholderEntry(frame, placeholder=_("gui", "login", "twofa_code"))
        # self._token_entry.grid(column=0, row=3, columnspan=2)

        self._confirm = asyncio.Event()
        self._button = ttk.Button(
            frame, text=_("gui", "login", "button"), command=self._confirm.set, state="disabled"
        )
        self._button.grid(column=0, row=4, columnspan=2)
        self.update(_("gui", "login", "logged_out"))

    def clear(self, login: bool = False, password: bool = False, token: bool = False):
        clear_all = not login and not password and not token
        if login or clear_all:
            self._login_entry.clear()
        if password or clear_all:
            self._pass_entry.clear()
        if token or clear_all:
            self._token_entry.clear()

    async def wait_for_login_press(self) -> None:
        self._confirm.clear()
        try:
            self._button.config(state="normal")
            await self._manager.coro_unless_closed(self._confirm.wait())
        finally:
            self._button.config(state="disabled")

    async def ask_login(self) -> LoginData:
        self.update(_("gui", "login", "required"))
        # ensure the window isn't hidden into tray when this runs
        self._manager.grab_attention(sound=False)
        while True:
            self._manager.print(_("gui", "login", "request"))
            await self.wait_for_login_press()
            login_data = LoginData(
                self._login_entry.get().strip(),
                self._pass_entry.get(),
                self._token_entry.get().strip(),
            )
            # basic input data validation: 3-25 characters in length, only ascii and underscores
            if (
                not 3 <= len(login_data.username) <= 25
                and re.match(r'^[a-zA-Z0-9_]+$', login_data.username)
            ):
                self.clear(login=True)
                continue
            if len(login_data.password) < 8:
                self.clear(password=True)
                continue
            if login_data.token and len(login_data.token) < 6:
                self.clear(token=True)
                continue
            return login_data

    async def ask_enter_code(self, page_url: URL, user_code: str) -> None:
        self.update(_("gui", "login", "required"))
        # ensure the window isn't hidden into tray when this runs
        self._manager.grab_attention(sound=False)
        self._manager.print(_("gui", "login", "request"))
        await self.wait_for_login_press()
        self._manager.print(f"Enter this code on the Twitch's device activation page: {user_code}")
        await asyncio.sleep(4)
        webopen(page_url)

    def update(self, status: str, user_id: int | None = None, user_name: str | None = None):
        user_id = user_id is None and "-" or str(user_id)
        user_name = user_name is None and "-" or user_name
        self._manager._root.title = f"{WINDOW_TITLE} | User: {user_name} ({user_id})"
        self._var.set(f"{status}\n{user_id}\n{user_name}")


class _BaseVars(TypedDict):
    progress: DoubleVar
    percentage: StringVar
    remaining: StringVar


class _CampaignVars(_BaseVars):
    name: StringVar
    game: StringVar


class _DropVars(_BaseVars):
    rewards: StringVar


class _ProgressVars(TypedDict):
    campaign: _CampaignVars
    drop: _DropVars


class CampaignProgress:
    BAR_LENGTH = 420
    ALMOST_DONE_SECONDS = 10

    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._manager = manager
        self._vars: _ProgressVars = {
            "campaign": {
                "name": StringVar(master),  # campaign name
                "game": StringVar(master),  # game name
                "progress": DoubleVar(master),  # controls the progress bar
                "percentage": StringVar(master),  # percentage display string
                "remaining": StringVar(master),  # time remaining string, filled via _update_time
            },
            "drop": {
                "rewards": StringVar(master),  # drop rewards
                "progress": DoubleVar(master),  # as above
                "percentage": StringVar(master),  # as above
                "remaining": StringVar(master),  # as above
            },
        }
        self._frame = frame = ttk.LabelFrame(
            master, text=_("gui", "progress", "name"), padding=(4, 0, 4, 4)
        )
        frame.grid(column=0, row=2, columnspan=2, sticky="nsew", padx=2)
        frame.columnconfigure(0, weight=2)
        frame.columnconfigure(1, weight=1)
        game_campaign = ttk.Frame(frame)
        game_campaign.grid(column=0, row=0, columnspan=2, sticky="nsew")
        game_campaign.columnconfigure(0, weight=1)
        game_campaign.columnconfigure(1, weight=1)
        ttk.Label(game_campaign, text=_("gui", "progress", "game")).grid(column=0, row=0)
        ttk.Label(game_campaign, textvariable=self._vars["campaign"]["game"]).grid(column=0, row=1)
        ttk.Label(game_campaign, text=_("gui", "progress", "campaign")).grid(column=1, row=0)
        ttk.Label(game_campaign, textvariable=self._vars["campaign"]["name"]).grid(column=1, row=1)
        ttk.Label(
            frame, text=_("gui", "progress", "campaign_progress")
        ).grid(column=0, row=2, rowspan=2)
        ttk.Label(frame, textvariable=self._vars["campaign"]["percentage"]).grid(column=1, row=2)
        ttk.Label(frame, textvariable=self._vars["campaign"]["remaining"]).grid(column=1, row=3)
        ttk.Progressbar(
            frame,
            mode="determinate",
            length=self.BAR_LENGTH,
            maximum=1,
            variable=self._vars["campaign"]["progress"],
        ).grid(column=0, row=4, columnspan=2)
        ttk.Separator(
            frame, orient="horizontal"
        ).grid(row=5, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(frame, text=_("gui", "progress", "drop")).grid(column=0, row=6, columnspan=2)
        ttk.Label(
            frame, textvariable=self._vars["drop"]["rewards"]
        ).grid(column=0, row=7, columnspan=2)
        ttk.Label(
            frame, text=_("gui", "progress", "drop_progress")
        ).grid(column=0, row=8, rowspan=2)
        ttk.Label(frame, textvariable=self._vars["drop"]["percentage"]).grid(column=1, row=8)
        ttk.Label(frame, textvariable=self._vars["drop"]["remaining"]).grid(column=1, row=9)
        ttk.Progressbar(
            frame,
            mode="determinate",
            length=self.BAR_LENGTH,
            maximum=1,
            variable=self._vars["drop"]["progress"],
        ).grid(column=0, row=10, columnspan=2)
        self._drop: TimedDrop | None = None
        self._seconds: int = 0
        self._timer_task: asyncio.Task[None] | None = None
        self.display(None)

    def _divmod(self, minutes: int) -> tuple[int, int]:
        if self._seconds < 60 and minutes > 0:
            minutes -= 1
        hours, minutes = divmod(minutes, 60)
        return (hours, minutes)

    def _update_time(self, seconds: int | None = None):
        if seconds is not None:
            self._seconds = seconds
        drop = self._drop
        if drop is not None:
            drop_minutes = drop.remaining_minutes
            campaign_minutes = drop.campaign.remaining_minutes
        else:
            drop_minutes = 0
            campaign_minutes = 0
        drop_vars: _DropVars = self._vars["drop"]
        campaign_vars: _CampaignVars = self._vars["campaign"]
        dseconds = self._seconds % 60
        hours, minutes = self._divmod(drop_minutes)
        drop_vars["remaining"].set(
            _("gui", "progress", "remaining").format(time=f"{hours:>2}:{minutes:02}:{dseconds:02}")
        )
        hours, minutes = self._divmod(campaign_minutes)
        campaign_vars["remaining"].set(
            _("gui", "progress", "remaining").format(time=f"{hours:>2}:{minutes:02}:{dseconds:02}")
        )

    async def _timer_loop(self):
        self._update_time(60)
        while self._seconds > 0:
            await asyncio.sleep(1)
            self._seconds -= 1
            self._update_time()
        self._timer_task = None

    def start_timer(self):
        if self._timer_task is None:
            if self._drop is None or self._drop.remaining_minutes <= 0:
                # if we're starting the timer at 0 drop minutes,
                # all we need is a single instant time update setting seconds to 60,
                # to avoid substracting a minute from campaign minutes
                self._update_time(60)
            else:
                self._timer_task = asyncio.create_task(self._timer_loop())

    def stop_timer(self):
        if self._timer_task is not None:
            self._timer_task.cancel()
            self._timer_task = None

    def minute_almost_done(self) -> bool:
        # already or almost done
        return self._timer_task is None or self._seconds <= self.ALMOST_DONE_SECONDS

    def display(self, drop: TimedDrop | None, *, countdown: bool = True, subone: bool = False):
        self._drop = drop
        vars_drop = self._vars["drop"]
        vars_campaign = self._vars["campaign"]
        self.stop_timer()
        if drop is None:
            # clear the drop display
            vars_drop["rewards"].set("...")
            vars_drop["progress"].set(0.0)
            vars_drop["percentage"].set("-%")
            vars_campaign["name"].set("...")
            vars_campaign["game"].set("...")
            vars_campaign["progress"].set(0.0)
            vars_campaign["percentage"].set("-%")
            self._update_time(0)
            return
        vars_drop["rewards"].set(drop.rewards_text())
        vars_drop["progress"].set(drop.progress)
        vars_drop["percentage"].set(f"{drop.progress:6.1%}")
        campaign = drop.campaign
        vars_campaign["name"].set(campaign.name)
        vars_campaign["game"].set(campaign.game.name)
        vars_campaign["progress"].set(campaign.progress)
        vars_campaign["percentage"].set(
            f"{campaign.progress:6.1%} ({campaign.claimed_drops}/{campaign.total_drops})"
        )
        if countdown:
            # restart our seconds update timer
            self.start_timer()
        elif subone:
            # display the current remaining time at 0 seconds (after substracting the minute)
            # this is because the watch loop will substract this minute
            # right after the first watch payload returns with a time update
            self._update_time(0)
        else:
            # display full time with no substracting
            self._update_time(60)


class ConsoleOutput:
    def __init__(self, manager: GUIManager, master: ttk.Widget):
        frame = ttk.LabelFrame(master, text=_("gui", "output"), padding=(4, 0, 4, 4))
        frame.grid(column=0, row=3, columnspan=3, sticky="nsew", padx=2)
        # tell master frame that the containing row can expand
        master.rowconfigure(3, weight=1)
        frame.rowconfigure(0, weight=1)  # let the frame expand
        frame.columnconfigure(0, weight=1)
        xscroll = ttk.Scrollbar(frame, orient="horizontal")
        yscroll = ttk.Scrollbar(frame, orient="vertical")
        self._text = tk.Text(
            frame,
            width=52,
            height=10,
            wrap="none",
            state="disabled",
            exportselection=False,
            xscrollcommand=xscroll.set,
            yscrollcommand=yscroll.set,
        )
        xscroll.config(command=self._text.xview)
        yscroll.config(command=self._text.yview)
        self._text.grid(column=0, row=0, sticky="nsew")
        xscroll.grid(column=0, row=1, sticky="ew")
        yscroll.grid(column=1, row=0, sticky="ns")

    def print(self, message: str):
        stamp = datetime.now().strftime("%X")
        if '\n' in message:
            message = message.replace('\n', f"\n{stamp}: ")
        self._text.config(state="normal")
        self._text.insert("end", f"{stamp}: {message}\n")
        self._text.see("end")  # scroll to the newly added line
        self._text.config(state="disabled")

    def configure_theme(self, *, bg: str, fg: str, sel_bg: str, sel_fg: str):
        # Apply colors to the Tk Text widget used for console output
        self._text.config(
            bg=bg,
            fg=fg,
            insertbackground=fg,
            selectbackground=sel_bg,
            selectforeground=sel_fg,
        )


class _Buttons(TypedDict):
    frame: ttk.Frame
    switch: ttk.Button


class ChannelList:
    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._manager = manager
        frame = ttk.LabelFrame(master, text=_("gui", "channels", "name"), padding=(4, 0, 4, 4))
        frame.grid(column=2, row=1, rowspan=2, sticky="nsew", padx=2)
        # tell master frame that the containing column can expand
        master.columnconfigure(2, weight=1)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        buttons_frame = ttk.Frame(frame)
        self._buttons: _Buttons = {
            "frame": buttons_frame,
            "switch": ttk.Button(
                buttons_frame,
                text=_("gui", "channels", "switch"),
                state="disabled",
                command=manager._twitch.state_change(State.CHANNEL_SWITCH),
            ),
        }
        buttons_frame.grid(column=0, row=0, columnspan=2)
        self._buttons["switch"].grid(column=0, row=0)
        scroll = ttk.Scrollbar(frame, orient="vertical")
        self._table = table = ttk.Treeview(
            frame,
            # columns definition is updated by _add_column
            yscrollcommand=scroll.set,
        )
        scroll.config(command=table.yview)
        table.grid(column=0, row=1, sticky="nsew")
        scroll.grid(column=1, row=1, sticky="ns")
        self._font = Font(frame, manager._style.lookup("Treeview", "font"))
        self._const_width: set[str] = set()
        table.tag_configure("watching", background="gray70")
        table.bind("<Button-1>", self._disable_column_resize)
        table.bind("<<TreeviewSelect>>", self._selected)
        self._add_column("#0", '', width=0)
        self._add_column(
            "channel", _("gui", "channels", "headings", "channel"), width=100, anchor='w'
        )
        self._add_column(
            "status",
            _("gui", "channels", "headings", "status"),
            width_template=[
                _("gui", "channels", "online"),
                _("gui", "channels", "pending"),
                _("gui", "channels", "offline"),
            ],
        )
        self._add_column("game", _("gui", "channels", "headings", "game"), width=50)
        self._add_column("drops", "🎁", width_template="✔")
        self._add_column(
            "viewers", _("gui", "channels", "headings", "viewers"), width_template="1234567"
        )
        self._add_column("acl_base", "📋", width_template="✔")
        self._channel_map: dict[str, Channel] = {}

    def _add_column(
        self,
        cid: str,
        name: str,
        *,
        anchor: tk._Anchor = "center",
        width: int | None = None,
        width_template: str | list[str] | None = None,
    ):
        table = self._table
        # NOTE: we don't do this for the icon column
        if cid != "#0":
            # we need to save the column settings and headings before modifying the columns...
            columns: tuple[str, ...] = table.cget("columns") or ()
            column_settings: dict[str, tuple[str, tk._Anchor, int, int]] = {}
            for s_cid in columns:
                s_column = table.column(s_cid)
                assert s_column is not None
                s_heading = table.heading(s_cid)
                assert s_heading is not None
                column_settings[s_cid] = (
                    s_heading["text"], s_heading["anchor"], s_column["width"], s_column["minwidth"]
                )
            # ..., then add the column
            table.config(columns=columns + (cid,))
            # ..., and then restore column settings and headings afterwards
            for s_cid, (s_name, s_anchor, s_width, s_minwidth) in column_settings.items():
                table.heading(s_cid, text=s_name, anchor=s_anchor)
                table.column(s_cid, minwidth=s_minwidth, width=s_width, stretch=False)
        # set heading and column settings for the new column
        if width_template is not None:
            if isinstance(width_template, str):
                width = self._measure(width_template)
            else:
                width = max((self._measure(template) for template in width_template), default=20)
            self._const_width.add(cid)
        assert width is not None
        table.heading(cid, text=name, anchor=anchor)
        table.column(cid, minwidth=width, width=width, stretch=False)

    def _disable_column_resize(self, event):
        if self._table.identify_region(event.x, event.y) == "separator":
            return "break"

    def _selected(self, event):
        selection = self._table.selection()
        if selection:
            self._buttons["switch"].config(state="normal")
        else:
            self._buttons["switch"].config(state="disabled")

    def _measure(self, text: str) -> int:
        # we need this because columns have 9-10 pixels of padding that cuts text off
        return self._font.measure(text) + 10

    def _redraw(self):
        # this forces a redraw that recalculates widget width
        self._table.event_generate("<<ThemeChanged>>")

    def _adjust_width(self, column: str, value: str):
        # causes the column to expand if the value's width is greater than the current width
        if column in self._const_width:
            return
        value_width = self._measure(value)
        curr_width = self._table.column(column, "width")
        if value_width > curr_width:
            self._table.column(column, width=value_width)
            self._redraw()

    def shrink(self):
        # causes the columns to shrink back after long values have been removed from it
        columns = self._table.cget("columns")
        iids = self._table.get_children()
        for column in columns:
            if column in self._const_width:
                continue
            if iids:
                # table has at least one item
                width = max(self._measure(self._table.set(i, column)) for i in iids)
                self._table.column(column, width=width)
            else:
                # no items - use minwidth
                minwidth = self._table.column(column, "minwidth")
                self._table.column(column, width=minwidth)
        self._redraw()

    def _set(self, iid: str, column: str, value: str):
        self._table.set(iid, column, value)
        self._adjust_width(column, value)

    def _insert(self, iid: str, values: dict[str, str]):
        to_insert: list[str] = []
        for cid in self._table.cget("columns"):
            value = values[cid]
            to_insert.append(value)
            self._adjust_width(cid, value)
        self._table.insert(parent='', index="end", iid=iid, values=to_insert)

    def clear_watching(self):
        for iid in self._table.tag_has("watching"):
            self._table.item(iid, tags='')

    def set_watching(self, channel: Channel):
        self.clear_watching()
        iid = channel.iid
        self._table.item(iid, tags="watching")
        self._table.see(iid)

    def get_selection(self) -> Channel | None:
        if not self._channel_map:
            return None
        selection = self._table.selection()
        if not selection:
            return None
        return self._channel_map[selection[0]]

    def clear_selection(self):
        self._table.selection_set('')

    def clear(self):
        iids = self._table.get_children()
        self._table.delete(*iids)
        self._channel_map.clear()
        self.shrink()

    def display(self, channel: Channel, *, add: bool = False):
        iid = channel.iid
        if not add and iid not in self._channel_map:
            # the channel isn't on the list and we're not supposed to add it
            return
        # ACL-based
        acl_based = "✔" if channel.acl_based else "❌"
        # status
        if channel.online:
            status = _("gui", "channels", "online")
        elif channel.pending_online:
            status = _("gui", "channels", "pending")
        else:
            status = _("gui", "channels", "offline")
        # game
        game = str(channel.game or '')
        # drops
        drops = "✔" if channel.drops_enabled else "❌"
        # viewers
        viewers = ''
        if channel.viewers is not None:
            viewers = str(channel.viewers)
        if iid in self._channel_map:
            self._set(iid, "game", game)
            self._set(iid, "drops", drops)
            self._set(iid, "status", status)
            self._set(iid, "viewers", viewers)
            self._set(iid, "acl_base", acl_based)
        elif add:
            self._channel_map[iid] = channel
            self._insert(
                iid,
                {
                    "game": game,
                    "drops": drops,
                    "status": status,
                    "viewers": viewers,
                    "acl_base": acl_based,
                    "channel": channel.name,
                },
            )

    def remove(self, channel: Channel):
        iid = channel.iid
        del self._channel_map[iid]
        self._table.delete(iid)


class TrayIcon:
    TITLE = "Twitch Drops Miner"

    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._manager = manager
        self.icon: pystray.Icon | None = None  # type: ignore[unused-ignore]
        self._icon_images: dict[str, Image_module.Image] = {
            "pickaxe": Image_module.open(resource_path("icons/pickaxe.ico")),
            "active": Image_module.open(resource_path("icons/active.ico")),
            "idle": Image_module.open(resource_path("icons/idle.ico")),
            "error": Image_module.open(resource_path("icons/error.ico")),
            "maint": Image_module.open(resource_path("icons/maint.ico")),
        }
        self._icon_state: str = "pickaxe"
        self._button = ttk.Button(master, command=self.minimize, text=_("gui", "tray", "minimize"))

        # Hides Tray button for macOS
        if sys.platform != "darwin":
            self._button.grid(column=0, row=0, sticky="ne")

    def __del__(self) -> None:
        self.stop()
        for icon_image in self._icon_images.values():
            icon_image.close()

    def _shorten(self, text: str, by_len: int, min_len: int) -> str:
        if (text_len := len(text)) <= min_len + 3 or by_len <= 0:
            # cannot shorten
            return text
        return text[:-min(by_len + 3, text_len - min_len)] + "..."

    def get_title(self, drop: TimedDrop | None) -> str:
        if drop is None:
            return self.TITLE
        campaign = drop.campaign
        title_parts: list[str] = [
            f"{self.TITLE}\n",
            f"{campaign.game.name}\n",
            drop.rewards_text(),
            f" {drop.progress:.1%} ({campaign.claimed_drops}/{campaign.total_drops})"
        ]
        min_len: int = 30
        max_len: int = 127
        missing_len = len(''.join(title_parts)) - max_len
        if missing_len > 0:
            # try shortening the reward text
            title_parts[2] = self._shorten(title_parts[2], missing_len, min_len)
            missing_len = len(''.join(title_parts)) - max_len
        if missing_len > 0:
            # try shortening the game name
            title_parts[1] = self._shorten(title_parts[1], missing_len, min_len)
            missing_len = len(''.join(title_parts)) - max_len
        if missing_len > 0:
            raise MinerException(f"Title couldn't be shortened: {''.join(title_parts)}")
        return ''.join(title_parts)

    def _start(self):
        loop = asyncio.get_running_loop()
        drop = self._manager.progress._drop

        # we need this because tray icon lives in a separate thread
        def bridge(func):
            return lambda: loop.call_soon_threadsafe(func)

        menu = pystray.Menu(
            pystray.MenuItem(_("gui", "tray", "show"), bridge(self.restore), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(_("gui", "tray", "quit"), bridge(self.quit)),
        )
        self.icon = pystray.Icon(
            "twitch_miner", self._icon_images[self._icon_state], self.get_title(drop), menu
        )
        # self.icon.run_detached()
        loop.run_in_executor(None, self.icon.run)

    def stop(self):
        if self.icon is not None:
            self.icon.stop()
            self.icon = None

    def quit(self):
        self._manager.close()

    def minimize(self):
        if sys.platform == "darwin":
            return
        if self.icon is None:
            self._start()
        else:
            self.icon.visible = True
        self._manager._root.withdraw()

    def restore(self):
        if self.icon is not None:
            # self.stop()
            self.icon.visible = False
        self._manager._root.deiconify()

    def notify(
        self, message: str, title: str | None = None, duration: float = 10
    ) -> asyncio.Task[None] | None:
        # do nothing if the user disabled notifications
        if not self._manager._twitch.settings.tray_notifications:
            return None
        if self.icon is not None:
            icon = self.icon  # nonlocal scope bind

            async def notifier():
                icon.notify(message, title)
                await asyncio.sleep(duration)
                icon.remove_notification()

            return asyncio.create_task(notifier())
        return None

    def update_title(self, drop: TimedDrop | None):
        if self.icon is not None:
            self.icon.title = self.get_title(drop)

    def change_icon(self, state: str):
        if state not in self._icon_images:
            raise ValueError("Invalid icon state")
        self._icon_state = state
        if self.icon is not None:
            self.icon.icon = self._icon_images[state]


class Notebook:
    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._nb = ttk.Notebook(master)
        self._nb.grid(column=0, row=0, sticky="nsew")
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)
        # prevent entries from being selected after switching tabs
        self._nb.bind("<<NotebookTabChanged>>", lambda event: manager._root.focus_set())

    def add_tab(self, widget: ttk.Widget, *, name: str, **kwargs):
        kwargs.pop("text", None)
        if "sticky" not in kwargs:
            kwargs["sticky"] = "nsew"
        self._nb.add(widget, text=name, **kwargs)

    def current_tab(self) -> int:
        return self._nb.index("current")

    def add_view_event(self, callback: abc.Callable[[tk.Event[ttk.Notebook]], Any]):
        self._nb.bind("<<NotebookTabChanged>>", callback, True)


class CampaignDisplay(TypedDict):
    frame: ttk.Frame
    status: ttk.Label


class InventoryOverview:
    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._manager = manager
        self._cache: ImageCache = manager._cache
        self._settings: Settings = manager._twitch.settings
        self._filters = {
            "not_linked": IntVar(
                master, self._settings.priority_mode is PriorityMode.PRIORITY_ONLY
            ),
            "upcoming": IntVar(master, 1),
            "expired": IntVar(master, 0),
            "excluded": IntVar(master, 0),
            "finished": IntVar(master, 0),
        }
        manager.tabs.add_view_event(self._on_tab_switched)
        # Filtering options
        filter_frame = ttk.LabelFrame(
            master, text=_("gui", "inventory", "filter", "name"), padding=(4, 0, 4, 4)
        )
        LABEL_SPACING = 20
        filter_frame.grid(column=0, row=0, columnspan=2, sticky="nsew")
        ttk.Label(
            filter_frame, text=_("gui", "inventory", "filter", "show"), padding=(0, 0, 10, 0)
        ).grid(column=0, row=0)
        icolumn = 0
        ttk.Checkbutton(
            filter_frame, variable=self._filters["not_linked"]
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Label(
            filter_frame,
            text=_("gui", "inventory", "filter", "not_linked"),
            padding=(0, 0, LABEL_SPACING, 0),
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Checkbutton(
            filter_frame, variable=self._filters["upcoming"]
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Label(
            filter_frame,
            text=_("gui", "inventory", "filter", "upcoming"),
            padding=(0, 0, LABEL_SPACING, 0),
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Checkbutton(
            filter_frame, variable=self._filters["expired"]
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Label(
            filter_frame,
            text=_("gui", "inventory", "filter", "expired"),
            padding=(0, 0, LABEL_SPACING, 0),
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Checkbutton(
            filter_frame, variable=self._filters["excluded"]
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Label(
            filter_frame,
            text=_("gui", "inventory", "filter", "excluded"),
            padding=(0, 0, LABEL_SPACING, 0),
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Checkbutton(
            filter_frame, variable=self._filters["finished"]
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Label(
            filter_frame,
            text=_("gui", "inventory", "filter", "finished"),
            padding=(0, 0, LABEL_SPACING, 0),
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Button(
            filter_frame, text=_("gui", "inventory", "filter", "refresh"), command=self.refresh
        ).grid(column=(icolumn := icolumn + 1), row=0)
        # Inventory view
        self._canvas = tk.Canvas(master, scrollregion=(0, 0, 0, 0))
        self._canvas.grid(column=0, row=1, sticky="nsew")
        master.rowconfigure(1, weight=1)
        master.columnconfigure(0, weight=1)
        xscroll = ttk.Scrollbar(master, orient="horizontal", command=self._canvas.xview)
        xscroll.grid(column=0, row=2, sticky="ew")
        yscroll = ttk.Scrollbar(master, orient="vertical", command=self._canvas.yview)
        yscroll.grid(column=1, row=1, sticky="ns")
        self._canvas.configure(xscrollcommand=xscroll.set, yscrollcommand=yscroll.set)
        self._canvas.bind("<Configure>", self._canvas_update)
        self._main_frame = ttk.Frame(self._canvas)
        self._canvas.bind(
            "<Enter>", lambda e: self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        )
        self._canvas.bind("<Leave>", lambda e: self._canvas.unbind_all("<MouseWheel>"))
        self._canvas.create_window(0, 0, anchor="nw", window=self._main_frame)
        self._campaigns: dict[DropsCampaign, CampaignDisplay] = {}
        self._drops: dict[str, ttk.Label] = {}

    def configure_theme(self, *, bg: str):
        # Canvas background needs manual control
        self._canvas.configure(bg=bg)

    def _update_visibility(self, campaign: DropsCampaign):
        # True if the campaign is supposed to show, False makes it hidden.
        frame = self._campaigns[campaign]["frame"]
        not_linked = bool(self._filters["not_linked"].get())
        expired = bool(self._filters["expired"].get())
        excluded = bool(self._filters["excluded"].get())
        upcoming = bool(self._filters["upcoming"].get())
        finished = bool(self._filters["finished"].get())
        priority_only = self._settings.priority_mode is PriorityMode.PRIORITY_ONLY
        if (
            campaign.required_minutes > 0  # don't show sub-only campaigns
            and (not_linked or campaign.eligible)
            and (campaign.active or upcoming and campaign.upcoming or expired and campaign.expired)
            and (
                excluded or (
                    not self._settings.is_excluded(campaign.game.name)
                    and not priority_only or self._settings.has_priority(campaign.game.name)
                )
            )
            and (finished or not campaign.finished)
        ):
            frame.grid()
        else:
            frame.grid_remove()

    def _on_tab_switched(self, event: tk.Event[ttk.Notebook]) -> None:
        if self._manager.tabs.current_tab() == 1:
            # refresh only if we're switching to the tab
            self.refresh()

    def get_status(self, campaign: DropsCampaign) -> tuple[str, str]:
        if campaign.active:
            status_text: str = _("gui", "inventory", "status", "active")
            status_color: str = "green"
        elif campaign.upcoming:
            status_text = _("gui", "inventory", "status", "upcoming")
            status_color = "goldenrod"
        else:
            status_text = _("gui", "inventory", "status", "expired")
            status_color = "red"
        return (status_text, status_color)

    def refresh(self):
        for campaign in self._campaigns:
            # status
            status_label = self._campaigns[campaign]["status"]
            status_text, status_color = self.get_status(campaign)
            status_label.config(text=status_text, foreground=status_color)
            # visibility
            self._update_visibility(campaign)
        self._canvas_update()

    def _canvas_update(self, event: tk.Event[tk.Canvas] | None = None):
        self._canvas.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_mousewheel(self, event: tk.Event[tk.Misc]):
        delta = -1 if event.delta > 0 else 1
        state: int = event.state if isinstance(event.state, int) else 0
        if state & 1:
            scroll = self._canvas.xview_scroll
        else:
            scroll = self._canvas.yview_scroll
        scroll(delta, "units")

    async def add_campaign(self, campaign: DropsCampaign) -> None:
        campaign_frame = ttk.Frame(self._main_frame, relief="ridge", borderwidth=1, padding=4)
        campaign_frame.grid(column=0, row=len(self._campaigns), sticky="nsew", pady=3)
        campaign_frame.rowconfigure(4, weight=1)
        campaign_frame.columnconfigure(1, weight=1)
        campaign_frame.columnconfigure(3, weight=10000)
        # Name
        ttk.Label(
            campaign_frame, text=campaign.name, takefocus=False, width=45
        ).grid(column=0, row=0, columnspan=2, sticky="w")
        # Status
        status_text, status_color = self.get_status(campaign)
        status_label = ttk.Label(
            campaign_frame, text=status_text, takefocus=False, foreground=status_color
        )
        status_label.grid(column=1, row=1, sticky="w", padx=4)
        # NOTE: We have to save the campaign's frame and status before any awaits happen,
        # otherwise the len(self._campaigns) call may overwrite an existing frame,
        # if the campaigns are added concurrently.
        self._campaigns[campaign] = {
            "frame": campaign_frame,
            "status": status_label,
        }
        # Starts / Ends
        MouseOverLabel(
            campaign_frame,
            text=_("gui", "inventory", "ends").format(
                time=campaign.ends_at.astimezone().replace(microsecond=0, tzinfo=None)
            ),
            alt_text=_("gui", "inventory", "starts").format(
                time=campaign.starts_at.astimezone().replace(microsecond=0, tzinfo=None)
            ),
            reverse=campaign.upcoming,
            takefocus=False,
        ).grid(column=1, row=2, sticky="w", padx=4)
        # Linking status
        if campaign.eligible:
            link_kwargs = {
                "style": '',
                "text": _("gui", "inventory", "status", "linked"),
                "foreground": "green",
            }
        else:
            link_kwargs = {
                "text": _("gui", "inventory", "status", "not_linked"),
                "foreground": "red",
            }
        LinkLabel(
            campaign_frame,
            link=campaign.link_url,
            takefocus=False,
            padding=0,
            **link_kwargs,
        ).grid(column=1, row=3, sticky="w", padx=4)
        # ACL channels
        acl = campaign.allowed_channels
        if acl:
            if len(acl) <= 5:
                allowed_text: str = '\n'.join(ch.name for ch in acl)
            else:
                allowed_text = '\n'.join(ch.name for ch in acl[:4])
                allowed_text += (
                    f"\n{_('gui', 'inventory', 'and_more').format(amount=len(acl) - 4)}"
                )
        else:
            allowed_text = _("gui", "inventory", "all_channels")
        ttk.Label(
            campaign_frame,
            text=f"{_('gui', 'inventory', 'allowed_channels')}\n{allowed_text}",
            takefocus=False,
        ).grid(column=1, row=4, sticky="nw", padx=4)
        # Image
        campaign_image = await self._cache.get(campaign.image_url, size=(108, 144))
        ttk.Label(campaign_frame, image=campaign_image).grid(column=0, row=1, rowspan=4)
        # Drops separator
        ttk.Separator(
            campaign_frame, orient="vertical", takefocus=False
        ).grid(column=2, row=0, rowspan=5, sticky="ns")
        # Drops display
        drops_row = ttk.Frame(campaign_frame)
        drops_row.grid(column=3, row=0, rowspan=5, sticky="nsew", padx=4)
        drops_row.rowconfigure(0, weight=1)
        for i, drop in enumerate(campaign.drops):
            drop_frame = ttk.Frame(drops_row, relief="ridge", borderwidth=1, padding=5)
            drop_frame.grid(column=i, row=0, padx=4)
            benefits_frame = ttk.Frame(drop_frame)
            benefits_frame.grid(column=0, row=0)
            benefit_images: list[PhotoImage] = await asyncio.gather(
                *(self._cache.get(benefit.image_url, (80, 80)) for benefit in drop.benefits)
            )
            for i, benefit, image in zip(range(len(drop.benefits)), drop.benefits, benefit_images):
                ttk.Label(
                    benefits_frame,
                    text=benefit.name,
                    image=image,
                    compound="bottom",
                ).grid(column=i, row=0, padx=5)
            self._drops[drop.id] = label = ttk.Label(drop_frame, justify=tk.CENTER)
            self.update_progress(drop, label)
            label.grid(column=0, row=1)
        if self._manager.tabs.current_tab() == 1:
            self._update_visibility(campaign)
            self._canvas_update()

    def clear(self) -> None:
        for child in self._main_frame.winfo_children():
            child.destroy()
        self._drops.clear()
        self._campaigns.clear()

    def update_progress(self, drop: TimedDrop, label: ttk.Label) -> None:
        progress_text: str
        progress_color: str = ''
        if drop.is_claimed:
            progress_color = "green"
            progress_text = _("gui", "inventory", "status", "claimed")
        elif drop.can_claim:
            progress_color = "goldenrod"
            progress_text = _("gui", "inventory", "status", "ready_to_claim")
        elif drop.current_minutes or drop.can_earn():
            progress_text = _("gui", "inventory", "percent_progress").format(
                percent=f"{drop.progress:3.1%}",
                minutes=drop.required_minutes,
            )
            if drop.ends_at < drop.campaign.ends_at:
                # this drop becomes unavailable earlier than the campaign ends
                progress_text += '\n' + _("gui", "inventory", "ends").format(
                    time=drop.ends_at.astimezone().replace(microsecond=0, tzinfo=None)
                )
        else:
            if drop.required_minutes > 0:
                progress_text = _("gui", "inventory", "minutes_progress").format(
                    minutes=drop.required_minutes
                )
            else:
                # required_minutes is zero for subscription-based drops
                progress_text = ''
            if datetime.now(timezone.utc) < drop.starts_at > drop.campaign.starts_at:
                # this drop can only be earned later than the campaign start
                progress_text += '\n' + _("gui", "inventory", "starts").format(
                    time=drop.starts_at.astimezone().replace(microsecond=0, tzinfo=None)
                )
            elif drop.ends_at < drop.campaign.ends_at:
                # this drop becomes unavailable earlier than the campaign ends
                progress_text += '\n' + _("gui", "inventory", "ends").format(
                    time=drop.ends_at.astimezone().replace(microsecond=0, tzinfo=None)
                )
        label.config(text=progress_text, foreground=progress_color)

    def update_drop(self, drop: TimedDrop) -> None:
        label = self._drops.get(drop.id)
        if label is None:
            return
        self.update_progress(drop, label)


def proxy_validate(entry: PlaceholderEntry, settings: Settings) -> bool:
    raw_url = entry.get().strip()
    entry.replace(raw_url)
    url = URL(raw_url)
    valid = url.host is not None and url.port is not None
    if not valid:
        entry.clear()
        url = URL()
    settings.proxy = url
    return valid


class _GameListsController:
    """
    Data layer for the Settings tab's three-column game list editor.

    Owns the bookkeeping for the user's classifications (priority order, the
    excluded set) and the transient catalog of currently-known games. The view
    asks for snapshots and issues operations; it never touches Settings
    directly. Single-classification is enforced — moving a name into one
    bucket auto-removes it from the other.

    "Stale" entries — names persisted in priority/exclude but absent from the
    most recent catalog update — are deliberately preserved so that user
    intent for past or upcoming campaigns survives across runs.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings: Settings = settings
        self._catalog: set[str] = set()
        self._linked: set[str] = set()
        self._catalog_initialized: bool = False

    def update_catalog(
        self, names: abc.Iterable[str], *, linked: abc.Iterable[str] | None = None
    ) -> None:
        # Accumulate to match legacy behavior: once a name has been observed
        # this run, it remains selectable in Available even if its campaign
        # later drops out of the inventory snapshot.
        self._catalog.update(names)
        if linked is not None:
            self._linked = set(linked)
        self._catalog_initialized = True

    def is_stale(self, name: str) -> bool:
        # Patterns aren't tied to a specific catalog entry — they may match
        # future games — so they're never marked stale.
        if Settings.is_pattern_entry(name):
            return False
        return self._catalog_initialized and name not in self._catalog

    def is_linked(self, name: str) -> bool:
        return name in self._linked

    @staticmethod
    def is_pattern(entry: str) -> bool:
        return Settings.is_pattern_entry(entry)

    @property
    def priority(self) -> list[str]:
        return list(self._settings.priority)

    @property
    def excluded(self) -> list[str]:
        return sorted(self._settings.exclude)

    def available(self) -> list[str]:
        # Filter out anything covered by an existing literal or pattern in
        # priority/exclude — handled centrally by Settings so the GUI and the
        # mining loop always agree on what counts as "covered".
        return sorted(
            name for name in self._catalog
            if not self._settings.has_priority(name)
            and not self._settings.is_excluded(name)
        )

    def add_to_priority(self, name: str) -> bool:
        if not name or name in self._settings.priority:
            return False
        self._settings.exclude.discard(name)
        self._settings.priority.append(name)
        self._settings.alter()
        return True

    def add_to_excluded(self, name: str) -> bool:
        if not name or name in self._settings.exclude:
            return False
        try:
            self._settings.priority.remove(name)
        except ValueError:
            pass
        self._settings.exclude.add(name)
        self._settings.alter()
        return True

    def remove_from_priority(self, name: str) -> bool:
        try:
            self._settings.priority.remove(name)
        except ValueError:
            return False
        self._settings.alter()
        return True

    def remove_from_excluded(self, name: str) -> bool:
        if name not in self._settings.exclude:
            return False
        self._settings.exclude.discard(name)
        self._settings.alter()
        return True

    def move_priority(self, name: str, amount: int) -> int | None:
        # amount > 0 moves up, amount < 0 moves down (legacy semantics).
        try:
            idx = self._settings.priority.index(name)
        except ValueError:
            return None
        max_idx = len(self._settings.priority) - 1
        new_idx = max(0, min(max_idx, idx - amount))
        if new_idx == idx:
            return idx
        self._settings.priority.pop(idx)
        self._settings.priority.insert(new_idx, name)
        self._settings.alter()
        return new_idx


class _GameListsView:
    """
    Three-column game-list editor (Priority | Available | Excluded).

    Pure UI: lays out the three listboxes, the inter-list transfer arrows,
    the priority reorder toolbar, the Available filter entry, and the
    right-click context menus. Delegates all data mutations to a
    :class:`_GameListsController`.
    """

    # Tint palettes indexed by the (config-only) ``list_contrast`` setting.
    # Index 0 = level 1 (current default, very subtle); higher indices step up
    # the saturation for displays where the lightest tints look almost white.
    _PRIORITY_TINTS_LIGHT = ("#e6f4ea", "#cdebd5", "#b5e1c0", "#9bd6aa", "#80cb95")
    _PRIORITY_TINTS_DARK = ("#1f3527", "#284631", "#31573b", "#3a6845", "#43794f")
    _EXCLUDED_TINTS_LIGHT = ("#fbeaea", "#f6d4d4", "#f1bdbd", "#eba6a6", "#e58f8f")
    _EXCLUDED_TINTS_DARK = ("#3a2222", "#4a2a2a", "#5a3232", "#6a3a3a", "#7a4242")
    _STALE_FG_LIGHT = "#888888"
    _STALE_FG_DARK = "#7a7a7a"
    # Glyph prepended to the displayed label for games whose Twitch account
    # link is already established — the underlying name list is unaffected.
    _LINKED_PREFIX = "🔗 "

    def __init__(self, master: ttk.Widget, controller: _GameListsController) -> None:
        self._controller = controller
        self._dark = False
        # Cache of the names currently displayed in each list, indexed by
        # listbox row — avoids parsing decorated strings to recover the
        # underlying game name.
        self._priority_names: list[str] = []
        self._available_names: list[str] = []
        self._excluded_names: list[str] = []
        # Last-applied palette so refreshes can re-tint without needing the
        # full theme dictionary on every call.
        self._palette = {
            "bg": "#ffffff", "fg": "#000000",
            "sel_bg": "#cce5ff", "sel_fg": "#000000",
        }
        self._build(master)
        self.refresh_all()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build(self, master: ttk.Widget) -> None:
        master.columnconfigure(0, weight=1, uniform="lst")
        master.columnconfigure(2, weight=1, uniform="lst")
        master.columnconfigure(4, weight=1, uniform="lst")
        master.rowconfigure(0, weight=1)

        self._priority_list = self._build_priority(master)
        # Between Priority and Available: ⮜ adds (item travels left into
        # Priority); ⮞ removes (item travels right back to Available).
        (
            self._available_to_priority_btn,
            self._priority_to_available_btn,
        ) = self._build_transfer_buttons(
            master, column=1,
            in_text="⮜", in_command=self._available_to_priority,
            out_text="⮞", out_command=self._priority_to_available,
        )
        self._available_list = self._build_available(master)
        # Between Available and Excluded: ⮞ adds; ⮜ removes.
        (
            self._available_to_excluded_btn,
            self._excluded_to_available_btn,
        ) = self._build_transfer_buttons(
            master, column=3,
            in_text="⮞", in_command=self._available_to_excluded,
            out_text="⮜", out_command=self._excluded_to_available,
        )
        self._excluded_list = self._build_excluded(master)
        self._build_context_menus(master)
        self._build_pattern_footer(master)
        self._build_legend(master)

    def _build_priority(self, master: ttk.Widget) -> PaddedListbox:
        frame = ttk.LabelFrame(
            master, padding=(4, 0, 4, 4), text=_("gui", "settings", "priority")
        )
        frame.grid(column=0, row=0, sticky="nsew", padx=(0, 2))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(frame)
        toolbar.grid(column=0, row=0, sticky="ew", pady=(0, 2))
        for i in range(4):
            toolbar.columnconfigure(i, weight=1, uniform="reord")
        # Order matters — used below to enable/disable on selection edges.
        self._priority_top_btn = ttk.Button(
            toolbar, text="⇈", style="Arrow.TButton", width=2,
            command=partial(self._move_priority, MAX_INT),
        )
        self._priority_top_btn.grid(column=0, row=0, sticky="ew")
        self._priority_up_btn = ttk.Button(
            toolbar, text="↑", style="Arrow.TButton", width=2,
            command=partial(self._move_priority, 1),
        )
        self._priority_up_btn.grid(column=1, row=0, sticky="ew")
        self._priority_down_btn = ttk.Button(
            toolbar, text="↓", style="Arrow.TButton", width=2,
            command=partial(self._move_priority, -1),
        )
        self._priority_down_btn.grid(column=2, row=0, sticky="ew")
        self._priority_bottom_btn = ttk.Button(
            toolbar, text="⇊", style="Arrow.TButton", width=2,
            command=partial(self._move_priority, -MAX_INT),
        )
        self._priority_bottom_btn.grid(column=3, row=0, sticky="ew")

        listbox = PaddedListbox(
            frame, height=14, padding=(1, 0),
            activestyle="none", selectmode="single",
            highlightthickness=0, exportselection=False,
        )
        listbox.grid(column=0, row=1, sticky="nsew")
        listbox.bind("<<ListboxSelect>>", lambda _e: self._update_button_state())
        listbox.bind("<Double-Button-1>", lambda _e: self._priority_to_available())
        listbox.bind("<Button-3>", self._on_priority_rightclick)
        return listbox

    def _build_available(self, master: ttk.Widget) -> PaddedListbox:
        frame = ttk.LabelFrame(
            master, padding=(4, 0, 4, 4), text=_("gui", "settings", "available")
        )
        frame.grid(column=2, row=0, sticky="nsew", padx=2)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        # NOTE: Don't bind a textvariable here. PlaceholderEntry implements its
        # placeholder by inserting the placeholder string as actual entry
        # content; a mirrored StringVar would therefore receive the placeholder
        # text on every focus-out and the filter would wipe the list. Reading
        # via ``entry.get()`` is safe — that override returns '' while the
        # placeholder is showing.
        self._search_entry = PlaceholderEntry(
            frame, placeholder=_("gui", "settings", "search_placeholder")
        )
        self._search_entry.grid(column=0, row=0, sticky="ew", pady=(0, 2))
        self._search_entry.bind(
            "<KeyRelease>", lambda _e: self._refresh_available()
        )
        # add="+": PlaceholderEntry already binds focus events to manage the
        # placeholder; we append rather than replace so its handler runs first
        # and the entry is in a consistent state by the time we read it.
        self._search_entry.bind(
            "<FocusOut>", lambda _e: self._refresh_available(), add="+"
        )
        self._search_entry.bind(
            "<FocusIn>", lambda _e: self._refresh_available(), add="+"
        )

        listbox = PaddedListbox(
            frame, height=14, padding=(1, 0),
            activestyle="none", selectmode="single",
            highlightthickness=0, exportselection=False,
        )
        listbox.grid(column=0, row=1, sticky="nsew")
        listbox.bind("<<ListboxSelect>>", lambda _e: self._update_button_state())
        listbox.bind("<Double-Button-1>", lambda _e: self._available_to_priority())
        listbox.bind("<Button-3>", self._on_available_rightclick)
        return listbox

    def _build_excluded(self, master: ttk.Widget) -> PaddedListbox:
        frame = ttk.LabelFrame(
            master, padding=(4, 0, 4, 4), text=_("gui", "settings", "exclude")
        )
        frame.grid(column=4, row=0, sticky="nsew", padx=(2, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        listbox = PaddedListbox(
            frame, height=14, padding=(1, 0),
            activestyle="none", selectmode="single",
            highlightthickness=0, exportselection=False,
        )
        listbox.grid(column=0, row=0, sticky="nsew")
        listbox.bind("<<ListboxSelect>>", lambda _e: self._update_button_state())
        listbox.bind("<Double-Button-1>", lambda _e: self._excluded_to_available())
        listbox.bind("<Button-3>", self._on_excluded_rightclick)
        return listbox

    def _build_transfer_buttons(
        self,
        master: ttk.Widget,
        *,
        column: int,
        in_text: str,
        in_command: abc.Callable[[], Any],
        out_text: str,
        out_command: abc.Callable[[], Any],
    ) -> tuple[ttk.Button, ttk.Button]:
        frame = ttk.Frame(master)
        frame.grid(column=column, row=0, padx=2, sticky="ns")
        # Pad rows above/below so the two buttons sit roughly mid-list.
        frame.rowconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)

        in_btn = ttk.Button(
            frame, text=in_text, style="Arrow.TButton", width=3, command=in_command
        )
        in_btn.grid(column=0, row=1, pady=2)
        out_btn = ttk.Button(
            frame, text=out_text, style="Arrow.TButton", width=3, command=out_command
        )
        out_btn.grid(column=0, row=2, pady=2)
        return in_btn, out_btn

    def _build_context_menus(self, master: ttk.Widget) -> None:
        ctx = lambda key: _("gui", "settings", "context_menu", key)  # noqa: E731

        self._available_menu = tk.Menu(master, tearoff=0)
        self._available_menu.add_command(
            label=ctx("add_to_priority"), command=self._available_to_priority
        )
        self._available_menu.add_command(
            label=ctx("add_to_excluded"), command=self._available_to_excluded
        )

        self._priority_menu = tk.Menu(master, tearoff=0)
        self._priority_menu.add_command(
            label=ctx("move_to_top"), command=partial(self._move_priority, MAX_INT)
        )
        self._priority_menu.add_command(
            label=ctx("move_up"), command=partial(self._move_priority, 1)
        )
        self._priority_menu.add_command(
            label=ctx("move_down"), command=partial(self._move_priority, -1)
        )
        self._priority_menu.add_command(
            label=ctx("move_to_bottom"), command=partial(self._move_priority, -MAX_INT)
        )
        self._priority_menu.add_separator()
        self._priority_menu.add_command(
            label=ctx("move_to_excluded"), command=self._priority_to_excluded
        )
        self._priority_menu.add_command(
            label=ctx("remove"), command=self._priority_to_available
        )

        self._excluded_menu = tk.Menu(master, tearoff=0)
        self._excluded_menu.add_command(
            label=ctx("move_to_priority"), command=self._excluded_to_priority
        )
        self._excluded_menu.add_command(
            label=ctx("remove"), command=self._excluded_to_available
        )

    def _build_legend(self, master: ttk.Widget) -> None:
        # Sample chips that mirror the actual list-row styling — each one is
        # rendered with the same bg/fg combination it represents in the lists,
        # so the legend itself is the legend.
        legend = ttk.Frame(master)
        legend.grid(column=0, row=2, columnspan=5, sticky="w", pady=(6, 0))
        ttk.Label(
            legend, text=_("gui", "settings", "legend", "name")
        ).grid(column=0, row=0, padx=(0, 8))

        chip_kw: dict[str, Any] = {
            "relief": "solid", "borderwidth": 1, "padx": 8, "pady": 1,
        }
        self._legend_priority = tk.Label(
            legend, text=_("gui", "settings", "legend", "priority"), **chip_kw
        )
        self._legend_priority.grid(column=1, row=0, padx=(0, 12))
        self._legend_excluded = tk.Label(
            legend, text=_("gui", "settings", "legend", "excluded"), **chip_kw
        )
        self._legend_excluded.grid(column=2, row=0, padx=(0, 12))
        # Stale chip pairs the priority tint with the muted foreground so the
        # user can see exactly how a stale entry renders inside a list.
        self._legend_stale = tk.Label(
            legend, text=_("gui", "settings", "legend", "stale"), **chip_kw
        )
        self._legend_stale.grid(column=3, row=0, padx=(0, 12))
        # Linked chip mirrors the prefix glyph used on the actual rows.
        self._legend_linked = tk.Label(
            legend,
            text=self._LINKED_PREFIX + _("gui", "settings", "legend", "linked"),
            **chip_kw,
        )
        self._legend_linked.grid(column=4, row=0)
        # Apply default (light) colors immediately; update_dark_mode will
        # repaint when the theme is finalised.
        self._apply_legend_colors()

    def _build_pattern_footer(self, master: ttk.Widget) -> None:
        # Patterns can only be added by typing them — the listboxes alone
        # can't introduce wildcard entries. This row sits between the lists
        # (row 0) and the legend (row 2) so it's discoverable but doesn't
        # crowd the more common point-and-click flow.
        footer = ttk.Frame(master)
        footer.grid(column=0, row=1, columnspan=5, sticky="ew", pady=(6, 0))
        footer.columnconfigure(1, weight=1)
        ttk.Label(
            footer, text=_("gui", "settings", "add_pattern")
        ).grid(column=0, row=0, padx=(0, 6))
        self._pattern_entry = PlaceholderEntry(
            footer,
            placeholder=_("gui", "settings", "add_pattern_placeholder"),
        )
        self._pattern_entry.grid(column=1, row=0, sticky="ew", padx=(0, 6))
        self._pattern_entry.bind("<Return>", lambda _e: self._pattern_to_priority())
        ttk.Button(
            footer,
            text=_("gui", "settings", "to_priority"),
            command=self._pattern_to_priority,
        ).grid(column=2, row=0, padx=(0, 4))
        ttk.Button(
            footer,
            text=_("gui", "settings", "to_excluded"),
            command=self._pattern_to_excluded,
        ).grid(column=3, row=0)

    def _apply_legend_colors(self) -> None:
        priority_tint = self._priority_tint()
        excluded_tint = self._excluded_tint()
        stale_fg = self._STALE_FG_DARK if self._dark else self._STALE_FG_LIGHT
        base_fg = self._palette["fg"]
        bg = self._palette["bg"]
        self._legend_priority.config(bg=priority_tint, fg=base_fg)
        self._legend_excluded.config(bg=excluded_tint, fg=base_fg)
        self._legend_stale.config(bg=priority_tint, fg=stale_fg)
        # Linked chip uses the base background so it visually matches an
        # untinted Available row that's been decorated with the link glyph.
        self._legend_linked.config(bg=bg, fg=base_fg)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def refresh_all(self) -> None:
        self._refresh_priority()
        self._refresh_available()
        self._refresh_excluded()
        self._update_button_state()

    def clear_selection(self) -> None:
        for lb in (self._priority_list, self._available_list, self._excluded_list):
            lb.selection_clear(0, "end")
        self._update_button_state()

    def update_dark_mode(
        self, *, bg: str, fg: str, sel_bg: str, sel_fg: str, dark: bool
    ) -> None:
        self._dark = dark
        self._palette = {"bg": bg, "fg": fg, "sel_bg": sel_bg, "sel_fg": sel_fg}
        for lb in (self._priority_list, self._available_list, self._excluded_list):
            lb.configure_theme(bg=bg, fg=fg, sel_bg=sel_bg, sel_fg=sel_fg)
        self._apply_legend_colors()
        # Re-apply per-row tints with the new palette.
        self._refresh_priority()
        self._refresh_excluded()
        self._refresh_available()

    # ------------------------------------------------------------------
    # List rendering
    # ------------------------------------------------------------------
    def _selected(self, listbox: PaddedListbox, names: list[str]) -> str | None:
        sel = listbox.curselection()
        if not sel:
            return None
        idx = sel[0]
        if idx >= len(names):
            return None
        return names[idx]

    def _contrast_index(self) -> int:
        # Resolve the contrast level lazily on each refresh so that a manual
        # edit to settings.json takes effect on Reload without reopening
        # the application.
        try:
            value = int(self._controller._settings.list_contrast)
        except (TypeError, ValueError):
            value = LIST_CONTRAST_MIN
        return max(0, min(len(self._PRIORITY_TINTS_LIGHT) - 1, value - 1))

    def _priority_tint(self) -> str:
        palette = self._PRIORITY_TINTS_DARK if self._dark else self._PRIORITY_TINTS_LIGHT
        return palette[self._contrast_index()]

    def _excluded_tint(self) -> str:
        palette = self._EXCLUDED_TINTS_DARK if self._dark else self._EXCLUDED_TINTS_LIGHT
        return palette[self._contrast_index()]

    def _decorate(self, name: str) -> str:
        # Display-only transformation: linked games get a small lock-link
        # glyph so the user can spot which campaigns they're already eligible
        # to mine. Patterns are never linked (they aren't catalog entries),
        # so this naturally leaves them un-prefixed.
        if self._controller.is_linked(name):
            return self._LINKED_PREFIX + name
        return name

    def _apply_tints(self, listbox: PaddedListbox, names: list[str], tint: str) -> None:
        stale_fg = self._STALE_FG_DARK if self._dark else self._STALE_FG_LIGHT
        base_fg = self._palette["fg"]
        for idx, name in enumerate(names):
            fg = stale_fg if self._controller.is_stale(name) else base_fg
            listbox.itemconfig(idx, background=tint, foreground=fg)

    def _refresh_priority(self) -> None:
        prev = self._selected(self._priority_list, self._priority_names)
        self._priority_names = self._controller.priority
        self._priority_list.delete(0, "end")
        for name in self._priority_names:
            self._priority_list.insert("end", self._decorate(name))
        self._apply_tints(self._priority_list, self._priority_names, self._priority_tint())
        if prev is not None and prev in self._priority_names:
            new_idx = self._priority_names.index(prev)
            self._priority_list.selection_set(new_idx)
            self._priority_list.see(new_idx)

    def _refresh_excluded(self) -> None:
        prev = self._selected(self._excluded_list, self._excluded_names)
        self._excluded_names = self._controller.excluded
        self._excluded_list.delete(0, "end")
        for name in self._excluded_names:
            self._excluded_list.insert("end", self._decorate(name))
        self._apply_tints(self._excluded_list, self._excluded_names, self._excluded_tint())
        if prev is not None and prev in self._excluded_names:
            new_idx = self._excluded_names.index(prev)
            self._excluded_list.selection_set(new_idx)
            self._excluded_list.see(new_idx)

    def _refresh_available(self) -> None:
        prev = self._selected(self._available_list, self._available_names)
        # PlaceholderEntry.get() returns '' while the placeholder is showing,
        # so an idle / unfocused field naturally yields an unfiltered list.
        flt = self._search_entry.get().strip().casefold()
        items = self._controller.available()
        if flt:
            items = [n for n in items if flt in n.casefold()]
        self._available_names = items
        self._available_list.delete(0, "end")
        for name in items:
            self._available_list.insert("end", self._decorate(name))
        # Available rows have no tint, but reset to base palette in case a
        # prior render colored them.
        for idx in range(len(items)):
            self._available_list.itemconfig(
                idx, background=self._palette["bg"], foreground=self._palette["fg"]
            )
        if prev is not None and prev in items:
            new_idx = items.index(prev)
            self._available_list.selection_set(new_idx)
            self._available_list.see(new_idx)
        self._update_button_state()

    # ------------------------------------------------------------------
    # Operations (selection-driven; safe no-ops when nothing is selected)
    # ------------------------------------------------------------------
    def _consume_pattern(self) -> str | None:
        text = self._pattern_entry.get().strip()
        if not text:
            return None
        self._pattern_entry.clear()
        return text

    def _pattern_to_priority(self) -> None:
        text = self._consume_pattern()
        if text and self._controller.add_to_priority(text):
            self._refresh_priority()
            self._refresh_available()
            self._select_in(self._priority_list, self._priority_names, text)

    def _pattern_to_excluded(self) -> None:
        text = self._consume_pattern()
        if text and self._controller.add_to_excluded(text):
            self._refresh_excluded()
            self._refresh_available()
            self._select_in(self._excluded_list, self._excluded_names, text)

    def _available_to_priority(self) -> None:
        name = self._selected(self._available_list, self._available_names)
        if name and self._controller.add_to_priority(name):
            self._refresh_priority()
            self._refresh_available()
            self._select_in(self._priority_list, self._priority_names, name)

    def _available_to_excluded(self) -> None:
        name = self._selected(self._available_list, self._available_names)
        if name and self._controller.add_to_excluded(name):
            self._refresh_excluded()
            self._refresh_available()
            self._select_in(self._excluded_list, self._excluded_names, name)

    def _priority_to_available(self) -> None:
        name = self._selected(self._priority_list, self._priority_names)
        if name and self._controller.remove_from_priority(name):
            self._refresh_priority()
            self._refresh_available()

    def _excluded_to_available(self) -> None:
        name = self._selected(self._excluded_list, self._excluded_names)
        if name and self._controller.remove_from_excluded(name):
            self._refresh_excluded()
            self._refresh_available()

    def _priority_to_excluded(self) -> None:
        name = self._selected(self._priority_list, self._priority_names)
        if name and self._controller.add_to_excluded(name):
            self._refresh_priority()
            self._refresh_excluded()
            self._select_in(self._excluded_list, self._excluded_names, name)

    def _excluded_to_priority(self) -> None:
        name = self._selected(self._excluded_list, self._excluded_names)
        if name and self._controller.add_to_priority(name):
            self._refresh_priority()
            self._refresh_excluded()
            self._select_in(self._priority_list, self._priority_names, name)

    def _move_priority(self, amount: int) -> None:
        name = self._selected(self._priority_list, self._priority_names)
        if name is None:
            return
        new_idx = self._controller.move_priority(name, amount)
        if new_idx is None:
            return
        self._refresh_priority()
        self._priority_list.selection_set(new_idx)
        self._priority_list.see(new_idx)
        self._update_button_state()

    def _select_in(
        self, listbox: PaddedListbox, names: list[str], name: str
    ) -> None:
        if name not in names:
            return
        idx = names.index(name)
        listbox.selection_clear(0, "end")
        listbox.selection_set(idx)
        listbox.see(idx)
        self._update_button_state()

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------
    def _update_button_state(self) -> None:
        avail_sel = bool(self._available_list.curselection())
        prio_sel = self._priority_list.curselection()
        excl_sel = bool(self._excluded_list.curselection())

        self._available_to_priority_btn.config(state="normal" if avail_sel else "disabled")
        self._available_to_excluded_btn.config(state="normal" if avail_sel else "disabled")
        self._priority_to_available_btn.config(state="normal" if prio_sel else "disabled")
        self._excluded_to_available_btn.config(state="normal" if excl_sel else "disabled")

        if prio_sel:
            idx = prio_sel[0]
            max_idx = self._priority_list.size() - 1
            up_state = "normal" if idx > 0 else "disabled"
            down_state = "normal" if idx < max_idx else "disabled"
            self._priority_top_btn.config(state=up_state)
            self._priority_up_btn.config(state=up_state)
            self._priority_down_btn.config(state=down_state)
            self._priority_bottom_btn.config(state=down_state)
        else:
            for btn in (
                self._priority_top_btn, self._priority_up_btn,
                self._priority_down_btn, self._priority_bottom_btn,
            ):
                btn.config(state="disabled")

    # ------------------------------------------------------------------
    # Right-click handling
    # ------------------------------------------------------------------
    def _select_at_pointer(
        self, listbox: PaddedListbox, event: tk.Event[PaddedListbox]
    ) -> bool:
        if listbox.size() == 0:
            return False
        idx = listbox.nearest(event.y)
        if idx < 0:
            return False
        listbox.selection_clear(0, "end")
        listbox.selection_set(idx)
        listbox.activate(idx)
        self._update_button_state()
        return True

    def _on_available_rightclick(self, event: tk.Event[PaddedListbox]) -> None:
        if not self._select_at_pointer(self._available_list, event):
            return
        self._available_menu.tk_popup(event.x_root, event.y_root)

    def _on_priority_rightclick(self, event: tk.Event[PaddedListbox]) -> None:
        if not self._select_at_pointer(self._priority_list, event):
            return
        idx = self._priority_list.curselection()[0]
        max_idx = self._priority_list.size() - 1
        up_state = "normal" if idx > 0 else "disabled"
        down_state = "normal" if idx < max_idx else "disabled"
        self._priority_menu.entryconfig(0, state=up_state)    # move to top
        self._priority_menu.entryconfig(1, state=up_state)    # move up
        self._priority_menu.entryconfig(2, state=down_state)  # move down
        self._priority_menu.entryconfig(3, state=down_state)  # move to bottom
        self._priority_menu.tk_popup(event.x_root, event.y_root)

    def _on_excluded_rightclick(self, event: tk.Event[PaddedListbox]) -> None:
        if not self._select_at_pointer(self._excluded_list, event):
            return
        self._excluded_menu.tk_popup(event.x_root, event.y_root)


class _SettingsVars(TypedDict):
    tray: IntVar
    proxy: StringVar
    autostart: IntVar
    dark_mode: IntVar
    language: StringVar
    priority_mode: StringVar
    tray_notifications: IntVar
    enable_badges_emotes: IntVar
    available_drops_check: IntVar


class SettingsPanel:
    AUTOSTART_NAME: str = "TwitchDropsMiner"
    AUTOSTART_KEY: str = "HKCU/Software/Microsoft/Windows/CurrentVersion/Run"

    @cached_property
    def PRIORITY_MODES(self) -> dict[PriorityMode, str]:
        # NOTE: Translation calls have to be deferred here,
        # to allow changing the language before the settings panel is initialized.
        return {
            PriorityMode.PRIORITY_ONLY: _("gui", "settings", "priority_modes", "priority_only"),
            PriorityMode.ENDING_SOONEST: _("gui", "settings", "priority_modes", "ending_soonest"),
            PriorityMode.LOW_AVBL_FIRST: _(
                "gui", "settings", "priority_modes", "low_availability"
            ),
        }

    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._manager = manager
        self._settings: Settings = manager._twitch.settings
        priority_mode = self._settings.priority_mode
        if priority_mode not in self.PRIORITY_MODES:
            priority_mode = PriorityMode.PRIORITY_ONLY
            self._settings.priority_mode = priority_mode
        self._vars: _SettingsVars = {
            "autostart": IntVar(master, 0),
            "language": StringVar(master, _.current),
            "proxy": StringVar(master, str(self._settings.proxy)),
            "tray": IntVar(master, self._settings.autostart_tray),
            "dark_mode": IntVar(master, int(self._settings.dark_mode)),
            "priority_mode": StringVar(master, self.PRIORITY_MODES[priority_mode]),
            "tray_notifications": IntVar(master, self._settings.tray_notifications),
            "enable_badges_emotes": IntVar(
                master, int(self._settings.enable_badges_emotes)
            ),
            "available_drops_check": IntVar(
                master, int(self._settings.available_drops_check)
            ),
        }
        self._game_lists_controller = _GameListsController(self._settings)
        # Top region: General + Advanced side by side; bottom region: the
        # full-width 3-column game-list editor; footer: Reload.
        master.rowconfigure(1, weight=1)
        master.columnconfigure(0, weight=1)
        center_frame = ttk.Frame(master)
        center_frame.grid(column=0, row=0, sticky="n")
        center_frame.columnconfigure(0, weight=0)
        center_frame.columnconfigure(1, weight=0)

        # General section
        general_frame = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "settings", "general", "name")
        )
        general_frame.grid(column=0, row=0, sticky="nsew")
        # use another frame to center the options within the section
        # NOTE: this can be adjusted or removed later on if more options were to be added
        general_frame.rowconfigure(0, weight=1)
        general_frame.columnconfigure(0, weight=1)
        general_center = ttk.Frame(general_frame)
        general_center.grid(column=0, row=0)

        # language frame
        language_frame = ttk.Frame(general_center)
        language_frame.grid(column=0, row=0)
        ttk.Label(language_frame, text="Language 🌐 (requires restart): ").grid(column=0, row=0)
        SelectCombobox(
            language_frame,
            values=list(_.languages),
            textvariable=self._vars["language"],
            command=lambda e: setattr(self._settings, "language", self._vars["language"].get()),
        ).grid(column=1, row=0)

        # checkboxes frame
        checkboxes_frame = ttk.Frame(general_center)
        checkboxes_frame.grid(column=0, row=1)
        ttk.Label(
            checkboxes_frame, text=_("gui", "settings", "general", "autostart")
        ).grid(column=0, row=(irow := 0), sticky="e")
        ttk.Checkbutton(
            checkboxes_frame, variable=self._vars["autostart"], command=self.update_autostart
        ).grid(column=1, row=irow, sticky="w")
        if sys.platform != "darwin":
            ttk.Label(
                checkboxes_frame, text=_("gui", "settings", "general", "tray")
            ).grid(column=0, row=(irow := irow + 1), sticky="e")
            ttk.Checkbutton(
                checkboxes_frame, variable=self._vars["tray"], command=self.update_autostart
            ).grid(column=1, row=irow, sticky="w")
            ttk.Label(
                checkboxes_frame, text=_("gui", "settings", "general", "tray_notifications")
            ).grid(column=0, row=(irow := irow + 1), sticky="e")
            ttk.Checkbutton(
                checkboxes_frame,
                variable=self._vars["tray_notifications"],
                command=lambda: setattr(
                    self._settings,
                    "tray_notifications",
                    bool(self._vars["tray_notifications"].get()),
                ),
            ).grid(column=1, row=irow, sticky="w")
        ttk.Label(
            checkboxes_frame, text=_("gui", "settings", "general", "dark_mode")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Checkbutton(
            checkboxes_frame,
            variable=self._vars["dark_mode"],
            command=self.update_dark_mode,
        ).grid(column=1, row=irow, sticky="w")
        ttk.Label(
            checkboxes_frame, text=_("gui", "settings", "general", "priority_mode")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        SelectCombobox(
            checkboxes_frame,
            command=self.priority_mode,
            textvariable=self._vars["priority_mode"],
            values=list(self.PRIORITY_MODES.values()),
        ).grid(column=1, row=irow, sticky="w")

        # proxy frame
        proxy_frame = ttk.Frame(general_center)
        proxy_frame.grid(column=0, row=2)
        ttk.Label(proxy_frame, text=_("gui", "settings", "general", "proxy")).grid(column=0, row=0)
        self._proxy = PlaceholderEntry(
            proxy_frame,
            width=37,
            validate="focusout",
            prefill="http://",
            textvariable=self._vars["proxy"],
            placeholder="http://username:password@address:port",
        )
        self._proxy.config(validatecommand=partial(proxy_validate, self._proxy, self._settings))
        self._proxy.grid(column=0, row=1)

        # Advanced section — sits to the right of General so the game-list
        # editor below has the full tab width to itself.
        advanced_frame = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "settings", "advanced", "name")
        )
        advanced_frame.grid(column=1, row=0, sticky="nsew", padx=(8, 0))
        advanced_frame.columnconfigure(0, weight=1)
        advanced_frame.rowconfigure(0, weight=1)
        advanced_center = ttk.Frame(advanced_frame)
        advanced_center.grid(column=0, row=0)

        # Warning message
        ttk.Label(
            advanced_center, text=_("gui", "settings", "advanced", "warning"), foreground="red"
        ).grid(column=0, row=(irow := 0), columnspan=2)
        ttk.Label(
            advanced_center,
            text=_("gui", "settings", "advanced", "warning_text"),
            foreground="goldenrod",
        ).grid(column=0, row=(irow := irow + 1), columnspan=2)
        # Toggles for badges and emotes, and available drops check
        ttk.Label(
            advanced_center, text=_("gui", "settings", "advanced", "enable_badges_emotes")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Checkbutton(
            advanced_center,
            variable=self._vars["enable_badges_emotes"],
            command=lambda: setattr(
                self._settings,
                "enable_badges_emotes",
                bool(self._vars["enable_badges_emotes"].get()),
            ),
        ).grid(column=1, row=irow, sticky="w")
        ttk.Label(
            advanced_center, text=_("gui", "settings", "advanced", "available_drops_check")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Checkbutton(
            advanced_center,
            variable=self._vars["available_drops_check"],
            command=lambda: setattr(
                self._settings,
                "available_drops_check",
                bool(self._vars["available_drops_check"].get()),
            ),
        ).grid(column=1, row=irow, sticky="w")

        # Game-lists region — full-width 3-column editor (Priority |
        # Available | Excluded). The view owns its own widgets/menus and
        # delegates state to ``self._game_lists_controller``.
        game_lists_frame = ttk.Frame(master, padding=(0, 8, 0, 0))
        game_lists_frame.grid(column=0, row=1, sticky="nsew")
        self._game_lists = _GameListsView(game_lists_frame, self._game_lists_controller)

        # Reload row
        reload_frame = ttk.Frame(master)
        reload_frame.grid(column=0, row=2, pady=4)
        ttk.Label(reload_frame, text=_("gui", "settings", "reload_text")).grid(column=0, row=0)
        ttk.Button(
            reload_frame,
            text=_("gui", "settings", "reload"),
            command=self._manager._twitch.state_change(State.INVENTORY_FETCH),
        ).grid(column=1, row=0)

        self._vars["autostart"].set(self._query_autostart())

    def clear_selection(self) -> None:
        self._game_lists.clear_selection()

    def update_dark_mode(self) -> None:
        self._settings.dark_mode = bool(self._vars["dark_mode"].get())
        self._manager.apply_theme(self._settings.dark_mode)

    def _get_self_path(self) -> str:
        # NOTE: we need double quotes in case the path contains spaces
        return f'"{SELF_PATH.resolve()!s}"'

    def _get_autostart_path(self) -> str:
        flags: list[str] = []
        # if applicable, include the current logging level as well
        for lvl_idx, lvl_value in LOGGING_LEVELS.items():
            if lvl_value == self._settings.logging_level:
                if lvl_idx > 0:
                    flags.append(f"-{'v' * lvl_idx}")
                break
        if self._vars["tray"].get():
            flags.append("--tray")
        if not IS_PACKAGED:
            # non-packaged autostart has to be done through the venv path pythonw
            return f"\"{SCRIPTS_PATH / 'pythonw'!s}\" {self._get_self_path()} {' '.join(flags)}"
        return f"{self._get_self_path()} {' '.join(flags)}"

    def _get_linux_autostart_filepath(self) -> Path:
        autostart_folder: Path = Path("~/.config/autostart").expanduser()
        if (config_home := os.environ.get("XDG_CONFIG_HOME")) is not None:
            config_autostart: Path = Path(config_home, "autostart").expanduser()
            if config_autostart.exists():
                autostart_folder = config_autostart
        return autostart_folder / f"{self.AUTOSTART_NAME}.desktop"

    def _get_mac_autostart_filepath(self) -> Path:
        return Path(
            Path.home(), f"Library/LaunchAgents/com.devilxd.{self.AUTOSTART_NAME.lower()}.plist"
        )

    def _query_autostart(self) -> bool:
        if sys.platform == "win32":
            with RegistryKey(self.AUTOSTART_KEY, read_only=True) as key:
                try:
                    value_type, value = key.get(self.AUTOSTART_NAME)
                except ValueNotFound:
                    return False
                # TODO: Consider deleting the old value to avoid autostart errors
                return (
                    value_type is ValueType.REG_SZ
                    and self._get_self_path() in value
                )
        elif sys.platform == "linux":
            autostart_file: Path = self._get_linux_autostart_filepath()
            if not autostart_file.exists():
                return False
            with autostart_file.open('r', encoding="utf8") as file:
                # TODO: Consider deleting the old file to avoid autostart errors
                return self._get_self_path() in file.read()
        elif sys.platform == "darwin":
            plist_file = self._get_mac_autostart_filepath()
            if not plist_file.exists():
                return False
            with plist_file.open('r', encoding="utf8") as file:
                return str(SELF_PATH.resolve()) in file.read()

    def update_autostart(self) -> None:
        enabled = bool(self._vars["autostart"].get())
        self._settings.autostart_tray = bool(self._vars["tray"].get())
        if sys.platform == "win32":
            if enabled:
                with RegistryKey(self.AUTOSTART_KEY) as key:
                    key.set(
                        self.AUTOSTART_NAME,
                        ValueType.REG_SZ,
                        self._get_autostart_path(),
                    )
            else:
                with RegistryKey(self.AUTOSTART_KEY) as key:
                    key.delete(self.AUTOSTART_NAME, silent=True)
        elif sys.platform == "linux":
            autostart_file: Path = self._get_linux_autostart_filepath()
            if enabled:
                file_contents: str = dedent(
                    f"""
                    [Desktop Entry]
                    Type=Application
                    Name=Twitch Drops Miner
                    Description=Mine timed drops on Twitch
                    Exec=sh -c '{self._get_autostart_path()}'
                    """
                )
                with autostart_file.open('w', encoding="utf8") as file:
                    file.write(file_contents)
            else:
                autostart_file.unlink(missing_ok=True)
        elif sys.platform == "darwin":
            plist_file = self._get_mac_autostart_filepath()

            if enabled:
                command_parts = shlex.split(self._get_autostart_path())
                plist_data = {
                    "Label": f"com.devilxd.{self.AUTOSTART_NAME.lower()}",
                    "ProgramArguments": command_parts,
                    "RunAtLoad": True,
                }
                plist_file.parent.mkdir(parents=True, exist_ok=True)
                with plist_file.open("wb") as file:
                    plistlib.dump(plist_data, file)
            else:
                plist_file.unlink(missing_ok=True)

    def set_games(self, games: set[Game], *, linked: set[str] | None = None) -> None:
        self._game_lists_controller.update_catalog(
            (game.name for game in games), linked=linked
        )
        self._game_lists.refresh_all()

    def priority_mode(self, event: tk.Event[ttk.Combobox]) -> None:
        mode_name: str = self._vars["priority_mode"].get()
        for value, name in self.PRIORITY_MODES.items():
            if mode_name == name:
                self._settings.priority_mode = value
                break


class HelpTab:
    WIDTH = 800

    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._twitch = manager._twitch
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)
        # use a frame to center the content within the tab
        center_frame = ttk.Frame(master)
        center_frame.grid(column=0, row=0)
        # use a frame for the bottom row specifically
        bottom_frame = ttk.Frame(master)
        bottom_frame.grid(column=0, row=1, sticky="nsew")
        irow = 0
        # About
        about = ttk.LabelFrame(center_frame, padding=(4, 0, 4, 4), text="About")
        about.grid(column=0, row=(irow := irow + 1), sticky="nsew", padx=2)
        about.columnconfigure(2, weight=1)
        # About - created by
        ttk.Label(
            about, text="Application created by: ", anchor="e"
        ).grid(column=0, row=0, sticky="nsew")
        LinkLabel(
            about, link="https://github.com/DevilXD", text="DevilXD"
        ).grid(column=1, row=0, sticky="nsew")
        # About - repo link
        ttk.Label(about, text="Repository: ", anchor="e").grid(column=0, row=1, sticky="nsew")
        LinkLabel(
            about,
            link="https://github.com/DevilXD/TwitchDropsMiner",
            text="https://github.com/DevilXD/TwitchDropsMiner",
        ).grid(column=1, row=1, sticky="nsew")
        # About - donate
        ttk.Separator(
            about, orient="horizontal"
        ).grid(column=0, row=2, columnspan=3, sticky="nsew")
        ttk.Label(about, text="Donate: ", anchor="e").grid(column=0, row=3, sticky="nsew")
        LinkLabel(
            about,
            link="https://www.buymeacoffee.com/DevilXD",
            text=(
                "If you like the application and found it useful, "
                "please consider donating a small amount of money to support me. Thank you!"
            ),
            wraplength=self.WIDTH,
        ).grid(column=1, row=3, sticky="nsew")
        # Useful links
        links = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "help", "links", "name")
        )
        links.grid(column=0, row=(irow := irow + 1), sticky="nsew", padx=2)
        LinkLabel(
            links,
            link="https://www.twitch.tv/drops/inventory",
            text=_("gui", "help", "links", "inventory"),
        ).grid(column=0, row=0, sticky="nsew")
        LinkLabel(
            links,
            link="https://www.twitch.tv/drops/campaigns",
            text=_("gui", "help", "links", "campaigns"),
        ).grid(column=0, row=1, sticky="nsew")
        # How It Works
        howitworks = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "help", "how_it_works")
        )
        howitworks.grid(column=0, row=(irow := irow + 1), sticky="nsew", padx=2)
        ttk.Label(
            howitworks, text=_("gui", "help", "how_it_works_text"), wraplength=self.WIDTH
        ).grid(sticky="nsew")
        getstarted = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "help", "getting_started")
        )
        getstarted.grid(column=0, row=(irow := irow + 1), sticky="nsew", padx=2)
        ttk.Label(
            getstarted, text=_("gui", "help", "getting_started_text"), wraplength=self.WIDTH
        ).grid(sticky="nsew")
        # Invalidate button
        invalidate_frame = ttk.Frame(bottom_frame)
        bottom_frame.columnconfigure(0, weight=1)  # center within the column
        invalidate_frame.grid(column=0, row=0, sticky="nse")
        ttk.Label(
            invalidate_frame, text=_("gui", "help", "invalidate", "text")
        ).grid(column=0, row=0)
        self._invalidate_button: ttk.Button = ttk.Button(
            invalidate_frame,
            text=_("gui", "help", "invalidate", "button"),
            command=self.invalidate_token,
            state="disabled",
        )
        self._invalidate_button.grid(column=1, row=0)

    def invalidate_token(self) -> None:
        # sync to async bridge
        asyncio.create_task(task_wrapper(self._invalidate_token)())

    async def _invalidate_token(self) -> None:
        auth_state = await self._twitch.get_auth()
        async with self._twitch.request(
            "POST",
            "https://id.twitch.tv/oauth2/revoke",
            data={
                "client_id": self._twitch._client_type.CLIENT_ID,
                "token": auth_state.access_token,
            }
        ) as response:
            if response.status == 200:
                auth_state.invalidate(delete_cookies=True)
            else:
                logger.error(f"Failed to invalidate the auth token: {response.status}")
        self._twitch.change_state(State.RESTART)


##########################################
# GUI DEFINITION END / GUI MANAGER START #
##########################################


class GUIManager:
    def __init__(self, twitch: Twitch):
        self._twitch: Twitch = twitch
        self._poll_task: asyncio.Task[NoReturn] | None = None
        self._close_requested = asyncio.Event()
        self._root = root = Tk(className=WINDOW_TITLE)
        # withdraw immediately to prevent the window from flashing
        self._root.withdraw()
        # root.resizable(False, True)
        set_root_icon(root, resource_path("icons/pickaxe.ico"))
        root.title(WINDOW_TITLE)  # window title
        root.bind_all("<KeyPress-Escape>", self.unfocus)  # pressing ESC unfocuses selection
        # Image cache for displaying images
        self._cache = ImageCache(self)

        # style adjustements
        self._style = style = ttk.Style(root)
        # theme
        theme = ''
        # theme = style.theme_names()[6]
        # style.theme_use(theme)
        # fix treeview's background color from tags not working (also see '_fixed_map')
        style.map(
            "Treeview",
            foreground=self._fixed_map("foreground"),
            background=self._fixed_map("background"),
        )
        # add padding to the tab names
        style.configure("TNotebook.Tab", padding=[8, 4])
        # Skip these for classic theme or macOS
        if theme != "classic" and sys.platform != "darwin":
            # remove Notebook.focus from the Notebook.Tab layout tree to avoid an ugly dotted line
            # on tab selection. We fold the Notebook.focus children into Notebook.padding children.
            original = style.layout("TNotebook.Tab")
            sublayout = original[0][1]["children"][0][1]
            sublayout["children"] = sublayout["children"][0][1]["children"]
            style.layout("TNotebook.Tab", original)
            # remove Checkbutton.focus dotted line from checkbuttons
            style.configure("TCheckbutton", padding=0)
            original = style.layout("TCheckbutton")
            sublayout = original[0][1]["children"]
            sublayout[1] = sublayout[1][1]["children"][0]
            del original[0][1]["children"][1]
            style.layout("TCheckbutton", original)
        # label style - green, yellow and red text
        style.configure("green.TLabel", foreground="green")
        style.configure("yellow.TLabel", foreground="goldenrod")
        style.configure("red.TLabel", foreground="red")
        # fonts storage
        self._fonts: dict[str, Font] = {}
        # end of style changes

        root_frame = ttk.Frame(root, padding=8)
        root_frame.grid(column=0, row=0, sticky="nsew")
        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)
        # Notebook
        self.tabs = Notebook(self, root_frame)
        # Tray icon - place after notebook so it draws on top of the tabs space
        self.tray = TrayIcon(self, root_frame)
        # Main tab
        main_frame = ttk.Frame(root_frame, padding=8)
        self.tabs.add_tab(main_frame, name=_("gui", "tabs", "main"))
        self.status = StatusBar(self, main_frame)
        self.websockets = WebsocketStatus(self, main_frame)
        self.login = LoginForm(self, main_frame)
        self.progress = CampaignProgress(self, main_frame)
        self.output = ConsoleOutput(self, main_frame)
        self.channels = ChannelList(self, main_frame)
        # Inventory tab
        inv_frame = ttk.Frame(root_frame, padding=8)
        self.inv = InventoryOverview(self, inv_frame)
        self.tabs.add_tab(inv_frame, name=_("gui", "tabs", "inventory"))
        # Settings tab
        settings_frame = ttk.Frame(root_frame, padding=8)
        self.settings = SettingsPanel(self, settings_frame)
        self.tabs.add_tab(settings_frame, name=_("gui", "tabs", "settings"))
        # Help tab
        help_frame = ttk.Frame(root_frame, padding=8)
        self.help = HelpTab(self, help_frame)
        self.tabs.add_tab(help_frame, name=_("gui", "tabs", "help"))
        # clamp minimum window size (update geometry first)
        root.update_idletasks()
        root.minsize(width=root.winfo_reqwidth(), height=root.winfo_reqheight())
        # register logging handler
        self._handler = _TKOutputHandler(self)
        self._handler.setFormatter(OUTPUT_FORMATTER)
        logger = logging.getLogger("TwitchDrops")
        logger.addHandler(self._handler)
        if (logging_level := logger.getEffectiveLevel()) < logging.ERROR:
            self.print(f"Logging level: {logging.getLevelName(logging_level)}")
        # gracefully handle Windows shutdown closing the application
        if sys.platform == "win32":
            # NOTE: this root.update() is required for the below to work - don't remove
            root.update()
            self._message_map = {
                # window close request
                win32con.WM_CLOSE: self.close,
                # shutdown request
                win32con.WM_QUERYENDSESSION: self.close,
            }
            # This hooks up the wnd_proc function as the message processor for the root window.
            self.old_wnd_proc = win32gui.SetWindowLong(
                self._handle, win32con.GWL_WNDPROC, self.wnd_proc
            )
            # This ensures all of this works when the application is withdrawn or iconified
            ctypes.windll.user32.ShutdownBlockReasonCreate(
                self._handle, ctypes.c_wchar_p(_("gui", "status", "exiting"))
            )
            # DEV NOTE: use this to remove the reason in the future
            # ctypes.windll.user32.ShutdownBlockReasonDestroy(self._handle)
        else:
            # use old-style window closing protocol for non-windows platforms
            root.protocol("WM_DELETE_WINDOW", self.close)
            root.protocol("WM_DESTROY_WINDOW", self.close)
        # Save current theme and apply palette after widgets are created
        try:
            self._orig_theme_name = self._style.theme_use()
        except Exception:
            self._orig_theme_name = ''
        self.apply_theme(self._twitch.settings.dark_mode)
        # stay hidden in tray if needed, otherwise show the window when everything's ready
        if self._twitch.settings.tray and sys.platform != "darwin":
            # NOTE: this starts the tray icon thread
            self._root.after_idle(self.tray.minimize)
        else:
            self._root.after_idle(self._root.deiconify)

    # https://stackoverflow.com/questions/56329342/tkinter-treeview-background-tag-not-working
    def _fixed_map(self, option):
        # Fix for setting text colour for Tkinter 8.6.9
        # From: https://core.tcl.tk/tk/info/509cafafae
        #
        # Returns the style map for 'option' with any styles starting with
        # ('!disabled', '!selected', ...) filtered out.

        # style.map() returns an empty list for missing options, so this
        # should be future-safe.
        return [
            elm for elm in self._style.map("Treeview", query_opt=option)
            if elm[:2] != ("!disabled", "!selected")
        ]

    def wnd_proc(self, hwnd, msg, w_param, l_param):
        """
        This function serves as a message processor for all messages sent
        to the application by Windows.
        """
        if msg == win32con.WM_DESTROY:
            win32api.SetWindowLong(self._handle, win32con.GWL_WNDPROC, self.old_wnd_proc)
        if msg in self._message_map:
            return self._message_map[msg](w_param, l_param)
        return win32gui.CallWindowProc(self.old_wnd_proc, hwnd, msg, w_param, l_param)

    @cached_property
    def _handle(self) -> int:
        return int(self._root.wm_frame(), 16)

    @property
    def running(self) -> bool:
        return self._poll_task is not None

    @property
    def close_requested(self) -> bool:
        return self._close_requested.is_set()

    async def wait_until_closed(self):
        # wait until the user closes the window
        await self._close_requested.wait()

    async def coro_unless_closed(self, coro: abc.Awaitable[_T]) -> _T:
        # In Python 3.11, we need to explicitly wrap awaitables
        tasks = [asyncio.ensure_future(coro), asyncio.ensure_future(self._close_requested.wait())]
        done: set[asyncio.Task[Any]]
        pending: set[asyncio.Task[Any]]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if self._close_requested.is_set():
            raise ExitRequest()
        return await next(iter(done))

    def prevent_close(self):
        self._close_requested.clear()

    def start(self):
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll())
        # self.progress.start_timer()

    def stop(self):
        self.progress.stop_timer()
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll(self):
        """
        This runs the Tkinter event loop via asyncio instead of calling mainloop.
        0.05s gives similar performance and CPU usage.
        Not ideal, but the simplest way to avoid threads, thread safety,
        loop.call_soon_threadsafe, futures and all of that.
        """
        update = self._root.update
        while True:
            try:
                update()
            except tk.TclError:
                # root has been destroyed
                break
            await asyncio.sleep(0.05)
        self._poll_task = None

    def close(self, *args) -> int:
        """
        Requests the GUI application to close.
        The window itself will be closed in the closing sequence later.
        """
        self._close_requested.set()
        # notify client we're supposed to close
        self._twitch.close()
        return 0

    def close_window(self):
        """
        Closes the window. Invalidates the logger.
        """
        self.tray.stop()
        logging.getLogger("TwitchDrops").removeHandler(self._handler)
        self._root.destroy()

    def unfocus(self, event):
        # support pressing ESC to unfocus
        self._root.focus_set()
        self.channels.clear_selection()
        self.settings.clear_selection()

    # these are here to interface with underlaying GUI components
    def save(self, *, force: bool = False) -> None:
        self._cache.save(force=force)

    def grab_attention(self, *, sound: bool = True):
        self.tray.restore()
        self._root.focus_set()
        if sound:
            self._root.bell()

    def set_games(self, games: set[Game], *, linked: set[str] | None = None) -> None:
        self.settings.set_games(games, linked=linked)

    def display_drop(
        self, drop: TimedDrop, *, countdown: bool = True, subone: bool = False
    ) -> None:
        self.progress.display(drop, countdown=countdown, subone=subone)  # main tab
        # inventory overview is updated from within drops themselves via change events
        self.tray.update_title(drop)  # tray

    def clear_drop(self):
        self.progress.display(None)
        self.tray.update_title(None)

    def print(self, message: str):
        # print to our custom output
        self.output.print(message)

    def _set_title_bar_color(self, color: int) -> None:
        """
        Set the Windows title bar color to match the theme.
        Only works on Windows with DWM enabled.

        Args:
            color: ARGB color value (e.g., 0x001E1E1E for dark gray).
        """
        if sys.platform != "win32":
            return
        # DWMWA_CAPTION_COLOR = 35
        DWMWA_CAPTION_COLOR = 35
        hwnd = self._root.winfo_id()
        frame_hwnd = ctypes.windll.user32.GetParent(hwnd)
        color_value = ctypes.c_int(color)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            frame_hwnd,
            DWMWA_CAPTION_COLOR,
            ctypes.byref(color_value),
            ctypes.sizeof(ctypes.c_int),
        )

    def apply_theme(self, dark: bool) -> None:
        """
        Apply dark/light palette to ttk styles and Tk widgets in a minimal, non-invasive way.
        """
        # Palette
        if dark:
            # Switch to a configurable ttk theme for better color control
            if sys.platform != "darwin" and self._style.theme_use() != "clam":
                self._style.theme_use("clam")
            bg = "#1e1e1e"
            fg = "#e6e6e6"
            sel_bg = "#094771"
            sel_fg = "#ffffff"
            link = "#4ea3ff"
            surface = "#252525"
            header = "#2a2a2a"
            fieldbg = "#2b2b2b"
            border = "#3c3c3c"
            muted = "#b3b3b3"
            accent = "#0d99ff"
        else:
            # Restore original theme if we changed it
            if getattr(self, "_orig_theme_name", '') and self._style.theme_use() == "clam":
                self._style.theme_use(self._orig_theme_name)
            # Use platform defaults but ensure toggling back is readable
            bg = "#f0f0f0"
            fg = "#000000"
            sel_bg = "#cce5ff"
            sel_fg = "#000000"
            link = "blue"
            surface = "#ffffff"
            header = "#eeeeee"
            fieldbg = "#ffffff"
            border = "#cccccc"
            muted = "#404040"
            accent = "#0a84ff"

        # Setting theme for macOS
        if sys.platform == "darwin":
            app = AppKit.NSApplication.sharedApplication()
            if dark:
                appearance = AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua)
            else:
                appearance = AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameAqua)
            app.setAppearance_(appearance)

        s = self._style
        # Fonts
        default_font = nametofont("TkDefaultFont")
        self._fonts["default"] = default_font
        # Font - button style with a larger font
        self._fonts["large"] = default_font.copy()
        self._fonts["large"].config(size=10)
        s.configure("Large.TButton", font=self._fonts["large"])
        # Font - button style for sorting arrows
        self._fonts["arrow"] = default_font.copy()
        self._fonts["arrow"].config(size=16)
        s.configure("Arrow.TButton", font=self._fonts["arrow"])
        s.configure("Arrow.TButton", padding=-4)  # reduce padding on arrow buttons
        # Font - label style that mimics links
        self._fonts["underlined"] = default_font.copy()
        self._fonts["underlined"].config(underline=True)
        s.configure("Link.TLabel", font=self._fonts["underlined"], foreground=link)
        # Font - label style with a monospace font
        self._fonts["monospaced"] = default_font.copy()
        self._fonts["monospaced"].config(family="Courier New", size=10)
        s.configure("MS.TLabel", font=self._fonts["monospaced"])

        # Base containers and labels
        s.configure("TFrame", background=bg, foreground=fg)
        s.configure("TLabel", background=bg, foreground=fg)
        s.configure("TLabelframe", background=bg, foreground=fg)
        s.configure("TLabelframe.Label", background=bg, foreground=fg)
        # Buttons and checks
        s.configure("TButton", background=surface, foreground=fg, bordercolor=border)
        s.map(
            "TButton",
            background=[("active", header), ("pressed", border)],
            foreground=[("disabled", muted)],
        )
        s.configure(
            "TCheckbutton",
            background=bg,
            foreground=fg,
            focuscolor=bg,
            bordercolor=border,
        )
        s.map(
            "TCheckbutton",
            # Remove hover visuals by mapping active/pressed to the base background
            background=[
                ("active", bg),
                ("pressed", bg),
            ],
            foreground=[("disabled", muted)],
            indicatorcolor=[
                ("selected", accent if dark else fg),
                ("!selected", border),
            ],
        )
        # Notebook
        s.configure("TNotebook", background=bg, bordercolor=border)
        s.configure("TNotebook.Tab", background=surface, foreground=fg, bordercolor=border)
        s.map(
            "TNotebook.Tab",
            background=[("selected", header), ("active", header)],
            foreground=[("disabled", muted)],
        )
        # Entries/Combos
        s.configure(
            "TEntry", fieldbackground=fieldbg, background=fieldbg, foreground=fg, insertcolor=fg
        )
        s.configure(
            "TCombobox", fieldbackground=fieldbg, background=fieldbg, foreground=fg, arrowcolor=fg
        )
        # Ensure readability for readonly comboboxes (Language, Priority mode)
        s.map(
            "TCombobox",
            foreground=[("readonly", fg), ("disabled", muted)],
            fieldbackground=[("readonly", fieldbg)],
            background=[("readonly", fieldbg)],
            arrowcolor=[("readonly", fg)],
        )
        s.map("TEntry", foreground=[("disabled", muted)])
        # Treeview
        s.configure(
            "Treeview",
            background=surface,
            fieldbackground=surface,
            foreground=fg,
            bordercolor=border,
        )
        s.map(
            "Treeview",
            background=[("selected", sel_bg)],
            foreground=[("selected", sel_fg)],
        )
        s.configure("Treeview.Heading", background=header, foreground=fg, bordercolor=border)
        # Progressbar
        s.configure("TProgressbar", background=accent, troughcolor=surface)
        # Scrollbars
        s.configure(
            "Vertical.TScrollbar",
            background=surface,
            troughcolor=bg,
            arrowcolor=fg,
            bordercolor=border,
        )
        s.configure(
            "Horizontal.TScrollbar",
            background=surface,
            troughcolor=bg,
            arrowcolor=fg,
            bordercolor=border,
        )

        # Pure Tk widgets
        # Console text
        self.output.configure_theme(bg=surface, fg=fg, sel_bg=sel_bg, sel_fg=sel_fg)
        # Game-list editor (Priority / Available / Excluded listboxes)
        self.settings._game_lists.update_dark_mode(
            bg=surface, fg=fg, sel_bg=sel_bg, sel_fg=sel_fg, dark=dark
        )
        # Inventory canvas
        self.inv.configure_theme(bg=bg)

        # Tk option database for selection/popup list readability (affects Tk-backed widgets)
        # Global selection colors and listbox defaults (covers Combobox dropdown)
        self._root.option_add("*selectBackground", sel_bg)
        self._root.option_add("*selectForeground", sel_fg)
        # Combobox dropdown list (Tk Listbox)
        for key in (
            "*TCombobox*Listbox.background",
            "*TCombobox*Listbox.Background",
            "*Listbox.background",
        ):
            self._root.option_add(key, surface)
        for key in (
            "*TCombobox*Listbox.foreground",
            "*TCombobox*Listbox.Foreground",
            "*Listbox.foreground",
        ):
            self._root.option_add(key, fg)
        for key in (
            "*TCombobox*Listbox.selectBackground",
            "*Listbox.selectBackground",
        ):
            self._root.option_add(key, sel_bg)
        for key in (
            "*TCombobox*Listbox.selectForeground",
            "*Listbox.selectForeground",
        ):
            self._root.option_add(key, sel_fg)

        # Set Windows title bar color to match dark theme
        if dark:
            # Use dark gray color 0x001E1E1E (ARGB format, matches bg color #1e1e1e)
            self._set_title_bar_color(0x001E1E1E)
        else:
            # Reset to system default title bar color
            self._set_title_bar_color(0xFFFFFFFF)


###################
# GUI MANAGER END #
###################


if __name__ == "__main__":
    # Everything below is for debug purposes only
    import aiohttp
    from types import SimpleNamespace

    class StrNamespace(SimpleNamespace):
        __hash__ = object.__hash__  # type: ignore

        def __str__(self):
            if hasattr(self, "_str__"):
                return self._str__(self)
            return super().__str__()

    class HashNamespace(SimpleNamespace):
        __hash__ = object.__hash__  # type: ignore

    def create_game(id: int, name: str):
        return StrNamespace(name=name, id=id, _str__=lambda s: s.name)

    iid = 0

    def create_channel(
        name: str,
        status: int,
        game: str | None,
        drops: bool,
        viewers: int,
        acl_based: bool,
    ):
        # status: 0 -> OFFLINE, 1 -> PENDING_ONLINE, 2 -> ONLINE
        if status == 1:
            status = False
            pending = True
        else:
            pending = False
        if game is not None:
            game_obj: StrNamespace | None = create_game(0, game)
        else:
            game_obj = None
        global iid
        return SimpleNamespace(
            name=name,
            iid=(iid := iid + 1),
            online=bool(status),
            pending_online=pending,
            game=game_obj,
            drops_enabled=drops,
            viewers=viewers,
            acl_based=acl_based,
        )

    def create_drop(
        campaign_name: str,
        game_name: str,
        rewards: list[str],
        claimed_drops: int,
        total_drops: int,
        current_minutes: int,
        total_minutes: int,
    ):
        cd = claimed_drops
        td = total_drops
        cm = current_minutes
        tm = total_minutes
        ref_stamp = datetime.now(timezone.utc)
        drop_image_url = (
            "https://static-cdn.jtvnw.net/twitch-quests-assets/"
            "REWARD/e0ede26e-b071-47f0-af5f-b80b26fa9fb4.png"
        )
        campaign_image_url = "https://static-cdn.jtvnw.net/ttv-boxart/515025-120x160.jpg"
        benefits = [SimpleNamespace(name=name, image_url=drop_image_url) for name in rewards]
        mock = SimpleNamespace(
            id="0",
            campaign=HashNamespace(
                name=campaign_name,
                id="campaign",
                game=create_game(0, game_name),
                expired=False,
                active=False,
                upcoming=True,
                eligible=False,
                finished=False,
                link_url="https://google.com",
                image_url=campaign_image_url,
                allowed_channels=[],
                starts_at=ref_stamp,
                ends_at=ref_stamp + timedelta(days=7),
                timed_drops={},
                claimed_drops=cd,
                total_drops=td,
                required_minutes=tm,
                remaining_drops=td - cd,
                progress=(cd * tm + cm) / (td * tm),
                remaining_minutes=(td - cd) * tm - cm,
            ),
            image_url=drop_image_url,
            can_claim=False,
            can_earn=lambda: False,
            is_claimed=False,
            preconditions=True,
            benefits=benefits,
            rewards_text=lambda: ', '.join(b.name for b in benefits),
            starts_at=ref_stamp + timedelta(seconds=2),
            ends_at=ref_stamp + timedelta(days=7) - timedelta(seconds=2),
            progress=cm/tm,
            current_minutes=cm,
            required_minutes=tm,
            remaining_minutes=tm-cm,
        )
        mock.campaign.timed_drops["0"] = mock
        mock.campaign.drops = mock.campaign.timed_drops.values()
        return mock

    async def main(exit_event: asyncio.Event):
        # Initialize GUI debug
        mock = SimpleNamespace(
            settings=SimpleNamespace(
                tray=False,
                priority=[],
                proxy=URL(),
                dark_mode=False,
                alter=lambda: None,
                language="English",
                autostart_tray=False,
                exclude={"Lit Game"},
                tray_notifications=True,
                enable_badges_emotes=False,
                available_drops_check=False,
                logging_level=LOGGING_LEVELS[0],
                priority_mode=PriorityMode.PRIORITY_ONLY,
            )
        )
        mock.change_state = lambda state: mock.gui.print(f"State change: {state.value}")
        mock.state_change = lambda state: partial(mock.change_state, state)
        mock.request = aiohttp.request
        # _.set_language("Русский")
        gui = GUIManager(mock)  # type: ignore
        mock.gui = gui
        mock.close = gui.stop
        gui.start()
        assert gui._poll_task is not None
        gui._poll_task.add_done_callback(lambda t: exit_event.set())
        # Login form
        gui.login.update("Login required", None)
        # Game selector and settings panel games
        gui.set_games(set([
            create_game(420690, "Lit Game"),
            create_game(123456, "Best Game"),
            create_game(654321, "My Game Very Long Name"),
        ]))
        # Channel list
        gui.channels.display(
            create_channel(
                name="Thomus",
                status=0,
                game=None,
                drops=False,
                viewers=0,
                acl_based=True,
            ),
            add=True,
        )
        channel = create_channel(
            name="Traitus", status=1, game=None, drops=False, viewers=0, acl_based=True
        )
        gui.channels.display(channel, add=True)
        gui.channels.set_watching(channel)
        gui.channels.display(
            create_channel(
                name="Testus",
                status=2,
                game="Best Game",
                drops=True,
                viewers=42,
                acl_based=False,
            ),
            add=True,
        )
        gui.channels.display(
            create_channel(
                name="Livus",
                status=2,
                game="Best Game",
                drops=True,
                viewers=69,
                acl_based=False,
            ),
            add=True,
        )
        gui._root.update()
        gui.channels.get_selection()
        # Inventory overview
        drop = create_drop(
            "Wardrobe Cleaning", "Cleaning Masters", ["Fancy Pants"], 2, 7, 0, 240
        )
        campaign = drop.campaign
        await gui.inv.add_campaign(campaign)

        gui.print("Single-line test message")
        await asyncio.sleep(1)
        gui.print("Multi-line\ntest\nmessage")

        # Tray
        # gui.tray.minimize()
        await asyncio.sleep(2)
        claim_text = (
            f"{campaign.game.name}\n"
            f"{drop.rewards_text()} ({campaign.claimed_drops}/{campaign.total_drops})"
        )
        gui.tray.notify(claim_text, "Mined Drop")

        # Drop progress
        gui.display_drop(drop, countdown=False)
        await asyncio.sleep(3)

        gui.progress.start_timer()
        await asyncio.sleep(5)

        gui.clear_drop()
        await asyncio.sleep(5)

        campaign.can_earn = lambda: True
        gui.inv.update_drop(drop)
        gui.display_drop(drop)
        await asyncio.sleep(10)

        drop.current_minutes = 239
        drop.remaining_minutes = 1
        drop.progress = 239/240
        campaign.remaining_minutes -= 1
        gui.inv.update_drop(drop)
        gui.display_drop(drop)
        await asyncio.sleep(63)

        drop.current_minutes = 240
        drop.remaining_minutes = 0
        drop.progress = 1.0
        campaign.remaining_minutes -= 1
        campaign.progress = 3/7
        campaign.claimed_drops = 3
        campaign.remaining_drops = 4
        gui.inv.update_drop(drop)
        gui.display_drop(drop)

    def main_exit(task: asyncio.Task[None]) -> None:
        if task.exception() is not None:
            exit_event.set()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    exit_event = asyncio.Event()
    main_task = loop.create_task(main(exit_event))
    main_task.add_done_callback(main_exit)
    loop.run_until_complete(exit_event.wait())
    if main_task.done():
        loop.run_until_complete(main_task)
