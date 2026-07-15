"""Concrete isolated Playwright Chromium adapter for CU-001."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import suppress
from typing import TypeVar

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Download,
    FileChooser,
    Page,
    Playwright,
    Request,
    Route,
    WebSocketRoute,
    sync_playwright,
)
from playwright.sync_api import (
    Error as PlaywrightError,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from .models import (
    ClickAction,
    ComputerAction,
    ComputerUseBlockReason,
    DoubleClickAction,
    DragAction,
    KeypressAction,
    MoveAction,
    ScreenshotAction,
    ScrollAction,
    TypeAction,
    WaitAction,
)
from .policy import PortalOriginPolicy
from .ports import (
    BrowserOperationError,
    BrowserOperationTimeout,
    BrowserPolicyViolation,
    ComputerBrowserSession,
)

_T = TypeVar("_T")
_POLICY_SETTLE_SECONDS = 0.05
_NETWORK_POLICY_BINDING = "__claimdoneNetworkCapabilityBlocked"
_FILE_POLICY_BINDING = "__claimdoneFileAccessBlocked"
_PERMISSION_POLICY_BINDING = "__claimdonePermissionRequestBlocked"
_CHROMIUM_POLICY_ARGS = [
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-domain-reliability",
    "--disable-extensions",
    "--disable-file-system",
    "--disable-preconnect",
    "--disable-quic",
    "--disable-sync",
    "--dns-prefetch-disable",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
    "--metrics-recording-only",
    "--no-pings",
    "--no-proxy-server",
]
_APPROVAL_SEMANTIC = re.compile(
    r"(?:approv(?:e|ed|al)?|authoriz(?:e|ation)|receipt|"
    r"submit(?:ted|ting)?|submission)",
    re.IGNORECASE,
)
_TARGET_SEMANTICS_SCRIPT = """
([x, y]) => {
  const clip = (value) => String(value ?? '').slice(0, 256);
  const hit = document.elementFromPoint(x, y);
  const target = hit?.closest(
    'button,a,input,select,textarea,[role="button"],[role="link"],'
      + '[role="menuitem"],[tabindex]'
  );
  if (!target) return '';
  const labels = target.labels
    ? Array.from(target.labels).slice(0, 8).map((label) => clip(label.textContent))
    : [];
  return [
    clip(target.getAttribute('aria-label')),
    clip(target.getAttribute('data-action')),
    clip(target.getAttribute('href')),
    clip(target.getAttribute('id')),
    clip(target.getAttribute('name')),
    clip(target.getAttribute('role')),
    clip(target.getAttribute('title')),
    clip(target.getAttribute('value')),
    clip(target.textContent),
    ...labels,
  ].filter(Boolean).join(' ').slice(0, 4096);
}
"""
_ACTIVE_SEMANTICS_SCRIPT = """
() => {
  const clip = (value) => String(value ?? '').slice(0, 256);
  const target = document.activeElement;
  if (!target) return '';
  const labels = target.labels
    ? Array.from(target.labels).slice(0, 8).map((label) => clip(label.textContent))
    : [];
  return [
    clip(target.getAttribute('aria-label')),
    clip(target.getAttribute('data-action')),
    clip(target.getAttribute('href')),
    clip(target.getAttribute('id')),
    clip(target.getAttribute('name')),
    clip(target.getAttribute('role')),
    clip(target.getAttribute('title')),
    clip(target.getAttribute('value')),
    clip(target.textContent),
    ...labels,
  ].filter(Boolean).join(' ').slice(0, 4096);
}
"""
_CAPABILITY_GUARD_SCRIPT = r"""
(() => {
  'use strict';
  const NativeDOMException = globalThis.DOMException;
  const networkBinding = globalThis.__claimdoneNetworkCapabilityBlocked;
  const fileBinding = globalThis.__claimdoneFileAccessBlocked;
  const permissionBinding = globalThis.__claimdonePermissionRequestBlocked;
  const signal = (binding) => {
    if (typeof binding === 'function') {
      try {
        void binding().catch(() => {});
      } catch (_) {}
    }
  };
  const deny = (binding) => {
    signal(binding);
    throw new NativeDOMException('Blocked', 'SecurityError');
  };
  const lock = (target, name, value) => {
    if (!target) return;
    try {
      Object.defineProperty(target, name, {
        value,
        configurable: false,
        enumerable: false,
        writable: false,
      });
    } catch (_) {}
  };
  const blockedNetworkConstructor = function () { deny(networkBinding); };
  for (const name of [
    'RTCPeerConnection',
    'webkitRTCPeerConnection',
    'RTCDataChannel',
    'WebTransport',
    'Worker',
    'SharedWorker',
    'ServiceWorker',
    'Worklet',
    'AudioWorkletNode',
    'AudioContext',
    'webkitAudioContext',
    'OfflineAudioContext',
  ]) {
    lock(globalThis, name, blockedNetworkConstructor);
  }
  for (const name of [
    'showOpenFilePicker',
    'showSaveFilePicker',
    'showDirectoryPicker',
    'chooseFileSystemEntries',
  ]) {
    lock(globalThis, name, function () { deny(fileBinding); });
  }
  const blockMethod = (target, name, binding) => {
    if (!target || typeof target[name] !== 'function') return;
    lock(target, name, function () { deny(binding); });
  };
  blockMethod(globalThis.navigator?.storage, 'getDirectory', fileBinding);
  blockMethod(globalThis.DataTransferItem?.prototype, 'getAsFileSystemHandle', fileBinding);
  blockMethod(globalThis.navigator?.serviceWorker, 'register', networkBinding);
  const blockedWorklet = Object.freeze({
    addModule: function () { deny(networkBinding); },
  });
  for (const name of ['paintWorklet', 'animationWorklet', 'layoutWorklet']) {
    const worklet = globalThis.CSS?.[name];
    if (worklet) blockMethod(worklet, 'addModule', networkBinding);
    else lock(globalThis.CSS, name, blockedWorklet);
  }
  for (const name of ['getUserMedia', 'getDisplayMedia']) {
    blockMethod(globalThis.navigator?.mediaDevices, name, permissionBinding);
  }
  for (const name of ['getCurrentPosition', 'watchPosition']) {
    blockMethod(globalThis.navigator?.geolocation, name, permissionBinding);
  }
  for (const [object, methods] of [
    [globalThis.navigator?.clipboard, ['read', 'readText', 'write', 'writeText']],
    [globalThis.navigator?.credentials, ['create', 'get']],
    [globalThis.navigator?.bluetooth, ['requestDevice']],
    [globalThis.navigator?.usb, ['requestDevice']],
    [globalThis.navigator?.serial, ['requestPort']],
    [globalThis.navigator?.hid, ['requestDevice']],
  ]) {
    for (const name of methods) blockMethod(object, name, permissionBinding);
  }
  lock(globalThis, 'queryLocalFonts', function () { deny(permissionBinding); });

  const restrictedSelector = [
    'link[rel~="dns-prefetch" i]',
    'link[rel~="preconnect" i]',
    'link[rel~="prefetch" i]',
    'link[rel~="prerender" i]',
  ].join(',');
  const restrictedMarkup = /\b(?:dns-prefetch|preconnect|prefetch|prerender)\b/i;
  const restrictedLink = (node) => {
    if (!node) return null;
    if (node.nodeType === 1 && node.matches?.(restrictedSelector)) return node;
    return node.querySelector?.(restrictedSelector) ?? null;
  };
  const rejectRestrictedNode = (node) => {
    if (restrictedLink(node)) deny(networkBinding);
  };
  const wrapNodeMethod = (target, name, nodeIndex) => {
    const original = target?.[name];
    if (typeof original !== 'function') return;
    lock(target, name, function (...args) {
      rejectRestrictedNode(args[nodeIndex]);
      return Reflect.apply(original, this, args);
    });
  };
  wrapNodeMethod(globalThis.Node?.prototype, 'appendChild', 0);
  wrapNodeMethod(globalThis.Node?.prototype, 'insertBefore', 0);
  wrapNodeMethod(globalThis.Node?.prototype, 'replaceChild', 0);
  wrapNodeMethod(globalThis.Element?.prototype, 'insertAdjacentElement', 1);
  for (const name of ['append', 'prepend', 'before', 'after', 'replaceWith']) {
    const original = globalThis.Element?.prototype?.[name];
    if (typeof original !== 'function') continue;
    lock(globalThis.Element.prototype, name, function (...args) {
      for (const node of args) rejectRestrictedNode(node);
      return Reflect.apply(original, this, args);
    });
  }
  const wrapMarkupMethod = (target, name, markupIndex) => {
    const original = target?.[name];
    if (typeof original !== 'function') return;
    lock(target, name, function (...args) {
      if (typeof args[markupIndex] === 'string' && restrictedMarkup.test(args[markupIndex])) {
        deny(networkBinding);
      }
      return Reflect.apply(original, this, args);
    });
  };
  wrapMarkupMethod(globalThis.Element?.prototype, 'insertAdjacentHTML', 1);
  wrapMarkupMethod(globalThis.Range?.prototype, 'createContextualFragment', 0);
  wrapMarkupMethod(globalThis.Document?.prototype, 'write', 0);
  wrapMarkupMethod(globalThis.Document?.prototype, 'writeln', 0);

  const observeRestrictedLinks = new MutationObserver((records) => {
    for (const record of records) {
      const direct = restrictedLink(record.target);
      if (direct) {
        direct.remove();
        signal(networkBinding);
        return;
      }
      for (const node of record.addedNodes ?? []) {
        const nested = restrictedLink(node);
        if (nested) {
          nested.remove();
          signal(networkBinding);
          return;
        }
      }
    }
  });
  observeRestrictedLinks.observe(document, {
    attributes: true,
    attributeFilter: ['href', 'rel'],
    childList: true,
    subtree: true,
  });
})();
"""


class PlaywrightBrowserFactory:
    """Launch one browser process and one fresh Chromium context per case."""

    def __init__(self, *, headless: bool = True) -> None:
        if type(headless) is not bool:
            raise ValueError("headless must be a strict boolean")
        self._headless = headless

    def open_case(
        self,
        case_id: str,
        *,
        policy: PortalOriginPolicy,
        viewport_width: int,
        viewport_height: int,
        wait_action_seconds: float,
        timeout_seconds: float,
    ) -> ComputerBrowserSession:
        """Create resources transactionally so partial launches cannot leak."""

        if type(case_id) is not str or not case_id:
            raise BrowserOperationError
        playwright: Playwright | None = None
        browser: Browser | None = None
        context: BrowserContext | None = None
        try:
            timeout_ms = _timeout_ms(timeout_seconds)
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(
                headless=self._headless,
                chromium_sandbox=True,
                env={},
                args=_CHROMIUM_POLICY_ARGS,
                timeout=timeout_ms,
            )
            context = browser.new_context(
                accept_downloads=False,
                permissions=[],
                service_workers="block",
                viewport={"width": viewport_width, "height": viewport_height},
            )
            return PlaywrightBrowserSession(
                playwright=playwright,
                browser=browser,
                context=context,
                policy=policy,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                wait_action_seconds=wait_action_seconds,
            )
        except BrowserOperationTimeout:
            _close_partial(context, browser, playwright)
            raise
        except PlaywrightTimeoutError:
            _close_partial(context, browser, playwright)
            raise BrowserOperationTimeout from None
        except Exception:
            _close_partial(context, browser, playwright)
            raise BrowserOperationError from None


class PlaywrightBrowserSession:
    """One latched-policy, case-owned Playwright context."""

    def __init__(
        self,
        *,
        playwright: Playwright,
        browser: Browser,
        context: BrowserContext,
        policy: PortalOriginPolicy,
        viewport_width: int,
        viewport_height: int,
        wait_action_seconds: float,
    ) -> None:
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._policy = policy
        self._viewport_width = viewport_width
        self._viewport_height = viewport_height
        self._wait_action_seconds = float(wait_action_seconds)
        self._violation: ComputerUseBlockReason | None = None
        self._closed = False
        context.route("**/*", self._route_request)
        context.route_web_socket("**/*", self._route_websocket)
        context.expose_binding(
            _NETWORK_POLICY_BINDING,
            self._on_network_capability,
        )
        context.expose_binding(
            _FILE_POLICY_BINDING,
            self._on_file_access,
        )
        context.expose_binding(
            _PERMISSION_POLICY_BINDING,
            self._on_permission_request,
        )
        context.add_init_script(script=_CAPABILITY_GUARD_SCRIPT)
        context.clear_permissions()
        self._page = context.new_page()
        context.on("page", self._on_context_page)
        context.on("download", self._on_download)
        self._page.on("popup", self._on_popup)
        self._page.on("filechooser", self._on_file_chooser)

    def navigate(self, url: str, *, timeout_seconds: float) -> None:
        self._policy.require_allowed_url(url)
        timeout_ms = _timeout_ms(timeout_seconds)
        self._call(
            lambda: self._page.goto(
                url,
                timeout=timeout_ms,
                wait_until="domcontentloaded",
            )
        )
        self.assert_safe()

    def execute(self, action: ComputerAction, *, timeout_seconds: float) -> None:
        timeout_ms = _timeout_ms(timeout_seconds)
        if not isinstance(action, ScreenshotAction | WaitAction) and (
            timeout_seconds < _POLICY_SETTLE_SECONDS
        ):
            raise BrowserOperationTimeout
        self._page.set_default_timeout(timeout_ms)
        self.assert_safe()
        self._block_approval_target(action)

        if isinstance(action, ClickAction):
            self._call(
                lambda: self._with_modifiers(
                    action.keys,
                    lambda: self._page.mouse.click(
                        action.x,
                        action.y,
                        button=action.button,
                    ),
                )
            )
        elif isinstance(action, DoubleClickAction):
            self._call(
                lambda: self._with_modifiers(
                    action.keys,
                    lambda: self._page.mouse.dblclick(
                        action.x,
                        action.y,
                        button=action.button,
                    ),
                )
            )
        elif isinstance(action, DragAction):
            self._call(lambda: self._execute_drag(action))
        elif isinstance(action, MoveAction):
            self._call(
                lambda: self._with_modifiers(
                    action.keys,
                    lambda: self._page.mouse.move(action.x, action.y),
                )
            )
        elif isinstance(action, ScrollAction):
            self._call(lambda: self._execute_scroll(action))
        elif isinstance(action, KeypressAction):
            self._call(lambda: self._execute_keypress(action))
        elif isinstance(action, TypeAction):
            self._call(lambda: self._page.keyboard.type(action.text))
        elif isinstance(action, WaitAction):
            if timeout_seconds < self._wait_action_seconds:
                raise BrowserOperationTimeout
            self._call(lambda: self._page.wait_for_timeout(self._wait_action_seconds * 1_000))
        elif not isinstance(action, ScreenshotAction):
            raise BrowserPolicyViolation(ComputerUseBlockReason.UNSUPPORTED_ACTION)
        if not isinstance(action, ScreenshotAction | WaitAction):
            self._call(lambda: self._page.wait_for_timeout(_POLICY_SETTLE_SECONDS * 1_000))
        self.assert_safe()

    def screenshot(self, *, timeout_seconds: float) -> bytes:
        timeout_ms = _timeout_ms(timeout_seconds)
        self.assert_safe()
        screenshot = self._call(
            lambda: self._page.screenshot(type="png", timeout=timeout_ms)
        )
        self.assert_safe()
        if type(screenshot) is not bytes:
            raise BrowserOperationError
        return screenshot

    def assert_safe(self) -> None:
        if self._closed:
            raise BrowserOperationError
        if self._violation is not None:
            raise BrowserPolicyViolation(self._violation)
        self._policy.require_allowed_url(self._page.url)
        if self._violation is not None:
            raise BrowserPolicyViolation(self._violation)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failed = False
        for close in (self._context.close, self._browser.close, self._playwright.stop):
            try:
                close()
            except Exception:
                failed = True
        if failed:
            raise BrowserOperationError

    def _route_request(self, route: Route, request: Request) -> None:
        reason = self._policy.reason_url_is_blocked(request.url)
        if reason is not None:
            self._latch(reason)
            with suppress(PlaywrightError):
                route.abort("blockedbyclient")
            return
        with suppress(PlaywrightError):
            route.continue_()

    def _route_websocket(self, websocket: WebSocketRoute) -> None:
        reason = self._policy.reason_websocket_is_blocked(websocket.url)
        if reason is not None:
            self._latch(reason)
            # Leaving a routed websocket unconnected prevents any network egress.
            # Calling the synchronous close API from its own route callback can
            # deadlock Playwright while the route event is still being handled.
            return
        with suppress(PlaywrightError):
            websocket.connect_to_server()

    def _on_context_page(self, page: Page) -> None:
        if self._closed or page is self._page:
            return
        self._latch(ComputerUseBlockReason.POPUP_BLOCKED)
        _close_page(page)

    def _on_popup(self, page: Page) -> None:
        if self._closed:
            return
        self._latch(ComputerUseBlockReason.POPUP_BLOCKED)
        _close_page(page)

    def _on_download(self, download: Download) -> None:
        if self._closed:
            return
        self._latch(ComputerUseBlockReason.DOWNLOAD_BLOCKED)
        with suppress(PlaywrightError):
            download.cancel()

    def _on_file_chooser(self, chooser: FileChooser) -> None:
        if self._closed:
            return
        self._latch(ComputerUseBlockReason.FILE_ACCESS_BLOCKED)
        with suppress(PlaywrightError):
            chooser.set_files([], timeout=1_000)

    def _on_network_capability(self, _source: object) -> None:
        if not self._closed:
            self._latch(ComputerUseBlockReason.NETWORK_CAPABILITY_BLOCKED)

    def _on_file_access(self, _source: object) -> None:
        if not self._closed:
            self._latch(ComputerUseBlockReason.FILE_ACCESS_BLOCKED)

    def _on_permission_request(self, _source: object) -> None:
        if not self._closed:
            self._latch(ComputerUseBlockReason.PERMISSION_REQUEST_BLOCKED)

    def _latch(self, reason: ComputerUseBlockReason) -> None:
        if self._violation is None:
            self._violation = reason

    def _block_approval_target(self, action: ComputerAction) -> None:
        semantics = ""
        if isinstance(action, ClickAction | DoubleClickAction):
            self._require_viewport_point(action.x, action.y)
            semantics = self._target_semantics(action.x, action.y)
        elif isinstance(action, DragAction):
            for point in (action.path[0], action.path[-1]):
                self._require_viewport_point(point.x, point.y)
                semantics = f"{semantics} {self._target_semantics(point.x, point.y)}"
        elif isinstance(action, MoveAction | ScrollAction):
            self._require_viewport_point(action.x, action.y)
        elif isinstance(action, KeypressAction | TypeAction):
            semantics = self._active_semantics()
        if _APPROVAL_SEMANTIC.search(semantics):
            self._latch(ComputerUseBlockReason.APPROVAL_ACTION_BLOCKED)
            raise BrowserPolicyViolation(ComputerUseBlockReason.APPROVAL_ACTION_BLOCKED)

    def _target_semantics(self, x: int, y: int) -> str:
        value = self._call(lambda: self._page.evaluate(_TARGET_SEMANTICS_SCRIPT, [x, y]))
        return value[:4_096] if type(value) is str else ""

    def _active_semantics(self) -> str:
        value = self._call(lambda: self._page.evaluate(_ACTIVE_SEMANTICS_SCRIPT))
        return value[:4_096] if type(value) is str else ""

    def _execute_keypress(self, action: KeypressAction) -> None:
        for key in action.keys:
            if _APPROVAL_SEMANTIC.search(self._active_semantics()):
                self._latch(ComputerUseBlockReason.APPROVAL_ACTION_BLOCKED)
                raise BrowserPolicyViolation(
                    ComputerUseBlockReason.APPROVAL_ACTION_BLOCKED
                )
            self._page.keyboard.press(_normalize_key(key))
            self.assert_safe()

    def _execute_drag(self, action: DragAction) -> None:
        start = action.path[0]

        def drag() -> None:
            self._page.mouse.move(start.x, start.y)
            self._page.mouse.down()
            try:
                for point in action.path[1:]:
                    self._page.mouse.move(point.x, point.y)
            finally:
                self._page.mouse.up()

        self._with_modifiers(action.keys, drag)

    def _execute_scroll(self, action: ScrollAction) -> None:
        def scroll() -> None:
            self._page.mouse.move(action.x, action.y)
            self._page.mouse.wheel(action.scroll_x, action.scroll_y)

        self._with_modifiers(action.keys, scroll)

    def _with_modifiers(self, keys: tuple[str, ...], callback: Callable[[], None]) -> None:
        normalized = tuple(_normalize_key(key) for key in keys)
        pressed: list[str] = []
        try:
            for key in normalized:
                self._page.keyboard.down(key)
                pressed.append(key)
            callback()
        finally:
            for key in reversed(pressed):
                self._page.keyboard.up(key)

    def _require_viewport_point(self, x: int, y: int) -> None:
        if not 0 <= x < self._viewport_width or not 0 <= y < self._viewport_height:
            raise BrowserPolicyViolation(ComputerUseBlockReason.INVALID_PROVIDER_RESPONSE)

    def _call(self, callback: Callable[[], _T]) -> _T:
        try:
            return callback()
        except BrowserPolicyViolation:
            raise
        except PlaywrightTimeoutError:
            raise BrowserOperationTimeout from None
        except PlaywrightError:
            if self._violation is not None:
                raise BrowserPolicyViolation(self._violation) from None
            raise BrowserOperationError from None


def _timeout_ms(timeout_seconds: float) -> float:
    if type(timeout_seconds) not in {int, float} or not 0.001 <= float(timeout_seconds) <= 90:
        raise BrowserOperationTimeout
    return float(timeout_seconds) * 1_000


def _normalize_key(key: str) -> str:
    return {
        "ALT": "Alt",
        "ARROWDOWN": "ArrowDown",
        "ARROWLEFT": "ArrowLeft",
        "ARROWRIGHT": "ArrowRight",
        "ARROWUP": "ArrowUp",
        "BACKSPACE": "Backspace",
        "CTRL": "Control",
        "DEL": "Delete",
        "DELETE": "Delete",
        "END": "End",
        "ENTER": "Enter",
        "ESC": "Escape",
        "ESCAPE": "Escape",
        "HOME": "Home",
        "INSERT": "Insert",
        "META": "Meta",
        "PAGEDOWN": "PageDown",
        "PAGEUP": "PageUp",
        "RETURN": "Enter",
        "SHIFT": "Shift",
        "SPACE": "Space",
        "TAB": "Tab",
    }.get(key, key)


def _close_page(page: Page) -> None:
    with suppress(PlaywrightError):
        page.close()


def _close_partial(
    context: BrowserContext | None,
    browser: Browser | None,
    playwright: Playwright | None,
) -> None:
    for close in (
        None if context is None else context.close,
        None if browser is None else browser.close,
        None if playwright is None else playwright.stop,
    ):
        if close is None:
            continue
        with suppress(Exception):
            close()
