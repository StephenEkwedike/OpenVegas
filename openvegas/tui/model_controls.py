"""Compact mouse-driven selectors; selection still uses the validated command loop."""

import asyncio
import re

from prompt_toolkit.mouse_events import MouseButton, MouseEventType

from openvegas.capabilities import REASONING_EFFORTS
from .model_picker import model_options, selectable


def _label(value, limit=64):
    return "".join(c for c in str(value) if c.isascii() and c.isprintable())[:limit]


class ModelControls:
    def __init__(self, *, app, current, load_models, load_efforts, submit, size):
        self.app, self.current, self.submit, self.size = app, current, submit, size
        self.load_models, self.load_efforts = load_models, load_efforts
        self.menu = None
        self.rows, self.efforts = [], ()
        self.page = 0
        self.loading, self.error = False, ""
        self.task = None
        self.drag_effort = None

    def close(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.menu = None
        self.app.invalidate()

    def _click(self, action):
        def handler(event):
            if event.event_type == MouseEventType.MOUSE_UP and event.button == MouseButton.LEFT:
                action()
        return handler

    def toggle(self, menu):
        if self.menu == menu:
            self.close()
            return
        self.close()
        self.menu, self.loading, self.error, self.page = menu, True, "", 0

        async def load():
            try:
                if menu == "models":
                    payload = await asyncio.wait_for(self.load_models(), 10)
                    if self.task is not asyncio.current_task():
                        return
                    if payload.get("switching_enabled") is False:
                        raise ValueError("disabled")
                    provider, model, _ = self.current()
                    self.rows = sorted(model_options(payload), key=lambda r: (
                        not selectable(r), (r["provider"], r["model_id"]) != (provider, model),
                        r["provider"] != provider, r["provider"], r["model_id"],
                    ))
                else:
                    efforts = await asyncio.wait_for(self.load_efforts(), 10)
                    if self.task is not asyncio.current_task():
                        return
                    self.efforts = tuple(e for e in REASONING_EFFORTS if e in efforts)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error = "Unavailable. Close and reopen to retry."
            finally:
                # An older cancelled load must not clear the new menu's spinner.
                if self.task is asyncio.current_task():
                    self.loading = False
                    self.app.invalidate()

        self.task = self.app.create_background_task(load())
        self.app.invalidate()

    def _choose_model(self, row):
        if not selectable(row):
            self.error = _label(row.get("unavailable_reason") or "Model unavailable.")
            self.app.invalidate()
            return
        provider, model = row["provider"], row["model_id"]
        if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}", s) for s in (provider, model)):
            self.error = "Invalid model identifier; selection unchanged."
            self.app.invalidate()
            return
        self.submit(f"/provider {provider} {model}")

    def _effort_handler(self, effort):
        def handler(event):
            if event.button != MouseButton.LEFT:
                return
            if event.event_type == MouseEventType.MOUSE_DOWN:
                self.drag_effort = effort
            elif event.event_type == MouseEventType.MOUSE_MOVE and self.drag_effort is not None:
                self.drag_effort = effort
            elif event.event_type == MouseEventType.MOUSE_UP:
                self.drag_effort = None
                self.submit(f"/reasoning {effort}")
            self.app.invalidate()
        return handler

    def fragments(self):
        provider, model, effort = self.current()
        columns, height = self.size()
        width = max(12, columns - 2)
        short_model = _label(model.rsplit("/", 1)[-1], max(8, width - 26))
        row = [
            ("class:bottom-toolbar", f" [ {short_model} v ]", self._click(lambda: self.toggle("models"))),
            ("class:bottom-toolbar", f" [ Effort: {_label(effort or 'Auto', 9)} v ]", self._click(lambda: self.toggle("effort"))),
        ]
        if not self.menu:
            return row
        if self.loading or self.error:
            return row + [("class:bottom-toolbar", "\n " + (self.error or "Loading...")[:width])]
        if self.menu == "models":
            page_size = min(4, max(1, height - 5))
            pages = max(1, (len(self.rows) + page_size - 1) // page_size)
            self.page = min(self.page, pages - 1)
            items = self.rows[self.page * page_size:(self.page + 1) * page_size]
            for item in items:
                active = (item["provider"], item["model_id"]) == (provider, model)
                marker = "*" if active else " "
                label = f"{marker} {item['model_id']}"
                if item["provider"] != provider:
                    label += f" ({item['provider']})"
                if not selectable(item):
                    label += " (unavailable)"
                row.append(("class:bottom-toolbar" + (" reverse" if active else ""),
                            "\n " + _label(label, width), self._click(lambda item=item: self._choose_model(item))))

            # Keep Prev/Next under the pointer on the short final page too.
            for _ in range(min(page_size, len(self.rows)) - len(items)):
                row.append(("class:bottom-toolbar", "\n "))

            def move(delta):
                self.page = max(0, min(pages - 1, self.page + delta))
                self.app.invalidate()

            row.extend([
                ("class:bottom-toolbar", "\n < Prev ", self._click(lambda: move(-1))),
                ("class:bottom-toolbar", f"{self.page + 1}/{pages}"),
                ("class:bottom-toolbar", " Next > ", self._click(lambda: move(1))),
                ("class:bottom-toolbar", " Close ", self._click(self.close)),
            ])
            if not self.rows:
                row.append(("class:bottom-toolbar", "\n No available catalog models."))
        else:
            selected = self.drag_effort or effort or "default"
            used = 0
            row.append(("class:bottom-toolbar", "\n "))
            labels = {"default": "Auto", "minimal": "Min", "medium": "Med", "xhigh": "XHigh"}
            for index, value in enumerate(("default", *self.efforts)):
                text = labels.get(value, value.title())
                text = f"[{text}]" if value == selected else f" {text} "
                separator = " -- " if index else ""
                if used + len(separator + text) > width:
                    separator, used = "\n ", 0
                row.append(("class:bottom-toolbar", separator))
                row.append(("class:bottom-toolbar" + (" reverse" if value == selected else ""),
                            text, self._effort_handler(value)))
                used += len(separator + text)
            if not self.efforts:
                row.append(("class:bottom-toolbar", "\n " + "Provider default only; no reviewed effort levels."[:width]))
        return row
