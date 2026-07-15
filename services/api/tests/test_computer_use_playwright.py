"""Real local Chromium checks for the isolated Playwright adapter."""

from __future__ import annotations

import socket
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest

from claimdone_api.computer_use import (
    BrowserOperationError,
    BrowserPolicyViolation,
    ClickAction,
    ComputerUseBlockReason,
    KeypressAction,
    PlaywrightBrowserFactory,
    PortalOriginPolicy,
    TypeAction,
    WaitAction,
)
from claimdone_api.computer_use.ports import ComputerBrowserSession


class RecordingServer(ThreadingHTTPServer):
    events: list[tuple[str, str]]
    probe_port: int


class EgressProbe:
    """Count local TCP/UDP packets without accepting application data."""

    def __init__(self) -> None:
        self._stopped = Event()
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind(("127.0.0.1", 0))
        self.port = int(self._tcp.getsockname()[1])
        self._tcp.listen()
        self._tcp.settimeout(0.1)
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.bind(("127.0.0.1", self.port))
        self._udp.settimeout(0.1)
        self.tcp_connections = 0
        self.udp_packets = 0
        self._threads = (
            Thread(target=self._accept_tcp, daemon=True),
            Thread(target=self._receive_udp, daemon=True),
        )

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def snapshot(self) -> tuple[int, int]:
        return self.tcp_connections, self.udp_packets

    def settled_snapshot(self) -> tuple[int, int]:
        self._stopped.wait(0.1)
        return self.snapshot()

    def close(self) -> None:
        self._stopped.set()
        self._tcp.close()
        self._udp.close()
        for thread in self._threads:
            thread.join(timeout=2)

    def _accept_tcp(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _address = self._tcp.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.tcp_connections += 1
            connection.close()

    def _receive_udp(self) -> None:
        while not self._stopped.is_set():
            try:
                self._udp.recvfrom(64 * 1_024)
            except TimeoutError:
                continue
            except OSError:
                return
            self.udp_packets += 1


class PortalHandler(BaseHTTPRequestHandler):
    server: RecordingServer

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/review":
            name = parse_qs(parsed.query).get("name", [""])[0]
            self.server.events.append(("review", name))
            self.send_response(204)
            self.end_headers()
            return
        if parsed.path == "/file":
            body = b"blocked-download"
            self.send_response(200)
            self.send_header("Content-Disposition", 'attachment; filename="blocked.txt"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        external_port = self.server.probe_port
        pages = {
            "/fill": """
                <input id="claimantName" aria-label="Claimant name">
                <button id="continueReview" onclick="
                  fetch('/review?name=' + encodeURIComponent(
                    document.querySelector('#claimantName').value
                  ));
                  document.body.dataset.state = 'review';
                ">Continue to review</button>
            """,
            "/external": f"""
                <button id="external" onclick="
                  location.href='http://127.0.0.1:{external_port}/evil'
                ">Open external</button>
            """,
            "/popup": """
                <button id="popup" onclick="window.open('/popup-target')">Open window</button>
            """,
            "/download": """
                <a id="download" href="/file" download>Download file</a>
            """,
            "/dom-control": """
                <button id="humanApproval" onclick="fetch('/review?name=forbidden')">
                  Human action
                </button>
            """,
            "/key-sequence": """
                <input id="safeField" autofocus>
                <button id="humanApproval" onclick="fetch('/review?name=forbidden')">
                  Human action
                </button>
            """,
            "/websocket": f"""
                <script>
                  window.socket = new WebSocket('ws://127.0.0.1:{external_port}/socket');
                </script>
                <p>WebSocket test</p>
            """,
            "/webrtc": f"""
                <button id="peerConnection" onclick="
                  try {{
                    const peer = new RTCPeerConnection({{
                      iceServers: [{{urls: 'stun:127.0.0.1:{external_port}'}}]
                    }});
                    peer.createDataChannel('blocked');
                  }} catch (_) {{}}
                ">Try peer connection</button>
            """,
            "/datachannel": """
                <button id="dataChannel" onclick="
                  try { new RTCDataChannel(); } catch (_) {}
                ">Try data channel</button>
            """,
            "/webtransport": f"""
                <button id="webTransport" onclick="
                  try {{ new WebTransport('https://127.0.0.1:{external_port}/'); }}
                  catch (_) {{}}
                ">Try web transport</button>
            """,
            "/worker": f"""
                <button id="workerTrigger">Try dedicated worker</button>
                <script>
                  document.querySelector('#workerTrigger').addEventListener('click', () => {{
                    const source = `
                    try {{ new WebTransport('https://127.0.0.1:{external_port}/'); }}
                    catch (_) {{}}
                    fetch('http://127.0.0.1:{external_port}/worker');
                    `;
                    try {{
                      new Worker(
                        URL.createObjectURL(new Blob([source], {{type: 'text/javascript'}}))
                      );
                    }} catch (_) {{}}
                  }});
                </script>
            """,
            "/shared-worker": f"""
                <button id="sharedWorkerTrigger">Try shared worker</button>
                <script>
                  document.querySelector('#sharedWorkerTrigger').addEventListener('click', () => {{
                    const source = "fetch('http://127.0.0.1:{external_port}/shared');";
                    try {{
                      new SharedWorker(
                        URL.createObjectURL(new Blob([source], {{type: 'text/javascript'}}))
                      );
                    }} catch (_) {{}}
                  }});
                </script>
            """,
            "/worklet": f"""
                <button id="workletTrigger">Try worklet</button>
                <script>
                  document.querySelector('#workletTrigger').addEventListener('click', () => {{
                    const source = "fetch('http://127.0.0.1:{external_port}/worklet');";
                    try {{
                      CSS.paintWorklet.addModule(
                        URL.createObjectURL(new Blob([source], {{type: 'text/javascript'}}))
                      );
                    }} catch (_) {{}}
                  }});
                </script>
            """,
            "/dns-prefetch": f"""
                <button id="dnsPrefetch" onclick="
                  const link = document.createElement('link');
                  link.rel = 'dns-prefetch';
                  link.href = 'https://127.0.0.1:{external_port}/';
                  try {{ document.head.appendChild(link); }} catch (_) {{}}
                ">Try DNS hint</button>
            """,
            "/preconnect": f"""
                <button id="preconnect" onclick="
                  const link = document.createElement('link');
                  link.rel = 'preconnect';
                  link.href = 'https://127.0.0.1:{external_port}/';
                  try {{ document.head.appendChild(link); }} catch (_) {{}}
                ">Try connection hint</button>
            """,
            "/file-system": """
                <button id="fileSystem" onclick="
                  try { void showOpenFilePicker(); } catch (_) {}
                ">Try local file API</button>
            """,
            "/file-chooser": """
                <input id="fileChooser" type="file" aria-label="Add attachment">
            """,
            "/permission": """
                <button id="permissionRequest" onclick="
                  try {
                    navigator.geolocation.getCurrentPosition(() => {}, () => {});
                  } catch (_) {}
                ">Try permission API</button>
            """,
            "/popup-target": "<p>Popup target</p>",
        }
        body_fragment = pages.get(parsed.path)
        if body_fragment is None:
            self.send_response(404)
            self.end_headers()
            return
        body = f"""<!doctype html>
            <html><head><style>
              body {{ margin: 0; font: 16px sans-serif; }}
              input, button, a {{ position: absolute; left: 20px; width: 260px; height: 40px; }}
              input {{ top: 20px; }}
              button, a {{ top: 100px; display: block; }}
            </style></head><body>{body_fragment}</body></html>""".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@pytest.fixture(scope="module")
def egress_probe() -> Generator[EgressProbe, None, None]:
    probe = EgressProbe()
    probe.start()
    try:
        yield probe
    finally:
        probe.close()


@pytest.fixture(scope="module")
def portal_server(egress_probe: EgressProbe) -> Generator[RecordingServer, None, None]:
    server = RecordingServer(("127.0.0.1", 0), PortalHandler)
    server.events = []
    server.probe_port = egress_probe.port
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def open_page(server: RecordingServer, path: str) -> ComputerBrowserSession:
    origin = f"http://127.0.0.1:{server.server_port}"
    session = PlaywrightBrowserFactory().open_case(
        "case_chromium_001",
        policy=PortalOriginPolicy(origin),
        viewport_width=800,
        viewport_height=600,
        wait_action_seconds=0.05,
        timeout_seconds=10,
    )
    try:
        session.navigate(f"{origin}{path}", timeout_seconds=5)
    except Exception:
        session.close()
        raise
    return session


def test_real_chromium_semantically_fills_review_and_closes(
    portal_server: RecordingServer,
) -> None:
    session = open_page(portal_server, "/fill")
    try:
        session.execute(ClickAction(100, 40), timeout_seconds=2)
        session.execute(TypeAction("Synthetic Claimant"), timeout_seconds=2)
        session.execute(ClickAction(100, 120), timeout_seconds=2)
        session.execute(WaitAction(), timeout_seconds=1)
        screenshot = session.screenshot(timeout_seconds=3)
        assert screenshot.startswith(b"\x89PNG\r\n\x1a\n")
        assert ("review", "Synthetic Claimant") in portal_server.events
    finally:
        session.close()
        session.close()

    with pytest.raises(BrowserOperationError):
        session.assert_safe()


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("/external", ComputerUseBlockReason.NAVIGATION_NOT_ALLOWED),
        ("/popup", ComputerUseBlockReason.POPUP_BLOCKED),
        ("/download", ComputerUseBlockReason.DOWNLOAD_BLOCKED),
        ("/dom-control", ComputerUseBlockReason.APPROVAL_ACTION_BLOCKED),
    ],
)
def test_real_chromium_latches_network_and_ui_policy_violations(
    portal_server: RecordingServer,
    egress_probe: EgressProbe,
    path: str,
    reason: ComputerUseBlockReason,
) -> None:
    before_egress = egress_probe.snapshot()
    session = open_page(portal_server, path)
    try:
        with pytest.raises(BrowserPolicyViolation) as captured:
            session.execute(ClickAction(100, 120), timeout_seconds=3)
        assert captured.value.reason is reason
        with pytest.raises(BrowserPolicyViolation) as latched:
            session.assert_safe()
        assert latched.value.reason is reason
        assert egress_probe.settled_snapshot() == before_egress
    finally:
        session.close()


def test_real_chromium_rechecks_active_target_before_each_key(
    portal_server: RecordingServer,
) -> None:
    before = list(portal_server.events)
    session = open_page(portal_server, "/key-sequence")
    try:
        with pytest.raises(BrowserPolicyViolation) as captured:
            session.execute(KeypressAction(("TAB", "ENTER")), timeout_seconds=3)
        assert captured.value.reason is ComputerUseBlockReason.APPROVAL_ACTION_BLOCKED
        assert portal_server.events == before
    finally:
        session.close()


def test_real_chromium_blocks_external_websocket_with_latched_policy(
    portal_server: RecordingServer,
    egress_probe: EgressProbe,
) -> None:
    before_egress = egress_probe.snapshot()
    origin = f"http://127.0.0.1:{portal_server.server_port}"
    session = PlaywrightBrowserFactory().open_case(
        "case_chromium_ws",
        policy=PortalOriginPolicy(origin),
        viewport_width=800,
        viewport_height=600,
        wait_action_seconds=0.05,
        timeout_seconds=10,
    )
    try:
        with pytest.raises(BrowserPolicyViolation) as captured:
            session.navigate(f"{origin}/websocket", timeout_seconds=5)
        assert captured.value.reason is ComputerUseBlockReason.NAVIGATION_NOT_ALLOWED
        with pytest.raises(BrowserPolicyViolation):
            session.assert_safe()
        assert egress_probe.settled_snapshot() == before_egress
    finally:
        session.close()


@pytest.mark.parametrize(
    ("path", "point", "reason"),
    [
        (
            "/webrtc",
            (100, 120),
            ComputerUseBlockReason.NETWORK_CAPABILITY_BLOCKED,
        ),
        (
            "/datachannel",
            (100, 120),
            ComputerUseBlockReason.NETWORK_CAPABILITY_BLOCKED,
        ),
        (
            "/webtransport",
            (100, 120),
            ComputerUseBlockReason.NETWORK_CAPABILITY_BLOCKED,
        ),
        (
            "/worker",
            (100, 120),
            ComputerUseBlockReason.NETWORK_CAPABILITY_BLOCKED,
        ),
        (
            "/shared-worker",
            (100, 120),
            ComputerUseBlockReason.NETWORK_CAPABILITY_BLOCKED,
        ),
        (
            "/worklet",
            (100, 120),
            ComputerUseBlockReason.NETWORK_CAPABILITY_BLOCKED,
        ),
        (
            "/dns-prefetch",
            (100, 120),
            ComputerUseBlockReason.NETWORK_CAPABILITY_BLOCKED,
        ),
        (
            "/preconnect",
            (100, 120),
            ComputerUseBlockReason.NETWORK_CAPABILITY_BLOCKED,
        ),
        (
            "/file-system",
            (100, 120),
            ComputerUseBlockReason.FILE_ACCESS_BLOCKED,
        ),
        (
            "/file-chooser",
            (100, 40),
            ComputerUseBlockReason.FILE_ACCESS_BLOCKED,
        ),
        (
            "/permission",
            (100, 120),
            ComputerUseBlockReason.PERMISSION_REQUEST_BLOCKED,
        ),
    ],
)
def test_real_chromium_blocks_non_http_network_and_file_capabilities(
    portal_server: RecordingServer,
    egress_probe: EgressProbe,
    path: str,
    point: tuple[int, int],
    reason: ComputerUseBlockReason,
) -> None:
    before_egress = egress_probe.snapshot()
    session = open_page(portal_server, path)
    try:
        with pytest.raises(BrowserPolicyViolation) as captured:
            session.execute(ClickAction(*point), timeout_seconds=3)
        assert captured.value.reason is reason
        with pytest.raises(BrowserPolicyViolation) as latched:
            session.assert_safe()
        assert latched.value.reason is reason
        assert egress_probe.settled_snapshot() == before_egress
    finally:
        session.close()


def test_mouse_action_rejects_non_modifier_key_before_browser_use() -> None:
    with pytest.raises(ValueError):
        ClickAction(10, 10, keys=cast(tuple[str, ...], ("ENTER",)))
