"""Chrome DevTools for browser actions, the DOM and settle hints (DESIGN.md section 6.3).

Observing the DOM gives every element a stable data-rlworld-id attribute, the
BrowserGym convention; browser actions name elements by those ids. A small
page script counts fetch and XHR requests in flight; until the page is complete
and nothing is in flight the screen never counts as settled. Images without a
browser simply report no DOM and never busy.
"""

import itertools
import json
from collections.abc import Callable
from typing import Any, Protocol

import httpx
import websocket

from rlworld.actions.models import (
    ActionModel,
    BackAction,
    ElementClickAction,
    FillAction,
    GotoAction,
    SelectOptionAction,
    TabAction,
)
from rlworld.guest.protocol import GuestFailure

TIMEOUT_SECONDS = 10.0
ELEMENT_ATTRIBUTE = "data-rlworld-id"
MAX_DOM_CHARACTERS = 200_000
# Counts requests in flight on every new document; the wrappers must be functions, so this page script has them.
PENDING_REQUESTS_SCRIPT = (
    "if (window.__rlworldPending === undefined) {"
    " window.__rlworldPending = 0;"
    " const rlworldFetch = window.fetch;"
    " window.fetch = function rlworldCountedFetch(...args) {"
    "  window.__rlworldPending += 1;"
    "  return rlworldFetch.apply(this, args).finally(function rlworldDone() { window.__rlworldPending -= 1; });"
    " };"
    " const rlworldSend = XMLHttpRequest.prototype.send;"
    " XMLHttpRequest.prototype.send = function rlworldCountedSend(...args) {"
    "  window.__rlworldPending += 1;"
    "  this.addEventListener('loadend', function rlworldLoaded() { window.__rlworldPending -= 1; });"
    "  return rlworldSend.apply(this, args);"
    " };"
    "}"
)
ANNOTATED_DOM_SCRIPT = (
    "var rlworldNext = window.__rlworldNextId || 0;"
    f" for (const rlworldElement of document.querySelectorAll('*')) {{"
    f"  if (!rlworldElement.hasAttribute('{ELEMENT_ATTRIBUTE}')) {{"
    f"   rlworldElement.setAttribute('{ELEMENT_ATTRIBUTE}', String(rlworldNext)); rlworldNext += 1; }} }}"
    " window.__rlworldNextId = rlworldNext;"
    " document.documentElement.outerHTML;"
)
BUSY_SCRIPT = "document.readyState !== 'complete' || (window.__rlworldPending || 0) > 0;"


class Connection(Protocol):
    def send(self, payload: str) -> Any: ...

    def recv(self) -> str: ...

    def close(self) -> None: ...


def open_socket(url: str) -> Connection:
    return websocket.create_connection(url, timeout=TIMEOUT_SECONDS)


def element_script(element_id: str, body: str) -> str:
    """Finds the element by its observed id, then runs body against rlworldElement."""
    selector = json.dumps(f'[{ELEMENT_ATTRIBUTE}="{element_id}"]')
    return (f"var rlworldElement = document.querySelector({selector});"
            f" if (rlworldElement === null) {{ throw new Error('no element ' + {json.dumps(element_id)}); }}"
            f" rlworldElement.scrollIntoView({{block: 'center'}}); {body} true;")


def fill_script(element_id: str, text: str) -> str:
    return element_script(element_id, (
        f"rlworldElement.focus(); rlworldElement.value = {json.dumps(text)};"
        " rlworldElement.dispatchEvent(new Event('input', {bubbles: true}));"
        " rlworldElement.dispatchEvent(new Event('change', {bubbles: true}));"))


def select_script(element_id: str, option: str) -> str:
    wanted = json.dumps(option)
    return element_script(element_id, (
        f"for (const rlworldOption of rlworldElement.options) {{"
        f" if (rlworldOption.value === {wanted} || rlworldOption.text === {wanted}) {{"
        f" rlworldElement.value = rlworldOption.value; }} }}"
        " rlworldElement.dispatchEvent(new Event('change', {bubbles: true}));"))


class CdpBrowser:
    def __init__(self, endpoint: str = "http://127.0.0.1:9222", http_client: httpx.Client | None = None,
                 connect: Callable[[str], Connection] = open_socket) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.http_client = http_client or httpx.Client(timeout=TIMEOUT_SECONDS)
        self.connect = connect
        self.connection: Connection | None = None
        self.target_id: str | None = None
        self.message_ids = itertools.count(1)

    def page_targets(self) -> list[dict[str, Any]]:
        targets = self.http_client.get(f"{self.endpoint}/json").json()
        return [target for target in targets if target.get("type") == "page"]

    def attach(self, target: dict[str, Any]) -> None:
        if self.connection is not None:
            self.connection.close()
        self.connection = self.connect(target["webSocketDebuggerUrl"])
        self.target_id = target["id"]
        self.command("Page.addScriptToEvaluateOnNewDocument", {"source": PENDING_REQUESTS_SCRIPT})
        self.evaluate(PENDING_REQUESTS_SCRIPT)

    def attached(self) -> bool:
        """False when the image runs no browser; browser actions then fail and settle hints stay quiet."""
        if self.connection is not None:
            return True
        try:
            targets = self.page_targets()
        except httpx.HTTPError:
            return False
        if not targets:
            return False
        self.attach(targets[0])
        return True

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.connection is None:
            raise GuestFailure("browser_error", "no browser page is attached")
        message_id = next(self.message_ids)
        self.connection.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            reply = json.loads(self.connection.recv())
            if reply.get("id") != message_id:
                continue
            if "error" in reply:
                raise GuestFailure("browser_error", f"{method}: {reply['error'].get('message', 'failed')}")
            return reply.get("result", {})

    def evaluate(self, expression: str) -> Any:
        result = self.command("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                   "awaitPromise": True})
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            message = details.get("exception", {}).get("description") or details.get("text", "script failed")
            raise GuestFailure("browser_error", message)
        return result.get("result", {}).get("value")

    def dom(self) -> str | None:
        if not self.attached():
            return None
        dom = self.evaluate(ANNOTATED_DOM_SCRIPT) or ""
        return dom if len(dom) <= MAX_DOM_CHARACTERS else f"{dom[:MAX_DOM_CHARACTERS]}<!-- truncated -->"

    def busy(self) -> bool:
        return self.attached() and bool(self.evaluate(BUSY_SCRIPT))

    def switch_to(self, target_id: str) -> None:
        for target in self.page_targets():
            if target["id"] == target_id:
                self.command("Target.activateTarget", {"targetId": target_id})
                self.attach(target)
                return
        raise GuestFailure("browser_error", f"tab {target_id} is gone")

    def perform_tab(self, action: TabAction) -> None:
        if action.operation == "new":
            created = self.command("Target.createTarget", {"url": action.url or "about:blank"})
            self.switch_to(created["targetId"])
        elif action.operation == "close":
            self.command("Target.closeTarget", {"targetId": self.target_id})
            self.connection = None
            if not self.attached():
                raise GuestFailure("browser_error", "the last tab was closed")
        else:
            pages = self.page_targets()
            if action.index is None or action.index >= len(pages):
                raise GuestFailure("browser_error", f"there is no tab {action.index}")
            self.switch_to(pages[action.index]["id"])

    def perform(self, action: ActionModel) -> None:
        if not self.attached():
            raise GuestFailure("unsupported_action", "this image runs no browser")
        if isinstance(action, GotoAction):
            self.command("Page.navigate", {"url": action.url})
        elif isinstance(action, ElementClickAction):
            self.evaluate(element_script(action.element_id, "rlworldElement.click();"))
        elif isinstance(action, FillAction):
            self.evaluate(fill_script(action.element_id, action.text))
        elif isinstance(action, SelectOptionAction):
            self.evaluate(select_script(action.element_id, action.option))
        elif isinstance(action, BackAction):
            self.evaluate("history.back(); true;")
        elif isinstance(action, TabAction):
            self.perform_tab(action)
        else:
            raise GuestFailure("unsupported_action", f"{action.name} is not a browser action")

    def set_cookie(self, name: str, value: str, domain: str) -> None:
        if not self.attached():
            raise GuestFailure("unsupported_action", "this image runs no browser to hold sessions")
        self.command("Network.setCookie", {"name": name, "value": value, "domain": domain, "path": "/",
                                           "secure": True, "httpOnly": True})
