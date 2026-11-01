"""Pairing with a PC: the "pair by address" exchange, over TCP to Beamer's pairing port.

Beamer's own `PairingService` does this, but it brings a discovery thread, a beacon and a peer
store with it. This app needs one direction only -- type an address and the six digits the PC is
showing -- so it drives `core.pairing.PairingClient` over the socket itself. The exchange, the
CPace maths and every check are upstream's; only the loop is here.

The password-authenticated exchange means a wrong code pairs with nobody and leaks nothing, and
upstream judges exactly one answer per attempt so a listener gets one guess at the code and no
more. That rule is kept: `client.accept` is called once, and a failure sends `client.abort()` so
the PC drops the code instead of showing it for the rest of its minute.
"""

import logging
import socket
import time

from core import pairing

LOGGER = logging.getLogger("capturekvm.pair")

CONNECT_SECONDS = pairing.TCP_CONNECT_SECONDS
EXCHANGE_SECONDS = pairing.TCP_EXCHANGE_SECONDS

# This app only ever initiates, so it offers no link port. `peer_entry` reads 0 as "never dial
# this machine", which is exactly right: the PC must not try to send its keyboard here.
OUR_LINK_PORT = 0

AlreadyPaired = pairing.AlreadyPaired
PairingError = pairing.PairingError


def pair_by_address(address: str, code: str, *, machine_id: bytes, name: str, platform: str,
                    peers=None, port: int = pairing.PAIRING_PORT) -> dict:
    """Pairs with the PC at `address` showing `code`, and returns the peer entry to store.

    Raises `PairingError` naming why not, or `AlreadyPaired` when this machine has that PC
    already. Blocks for up to ten seconds; call it off the UI thread."""
    code = (code or "").strip().replace(" ", "").replace("-", "")
    if not code.isdigit() or len(code) != pairing.CODE_DIGITS:
        raise PairingError(f"the code is {pairing.CODE_DIGITS} digits")
    resolved = _resolve(address, port)
    return _exchange(resolved, port, code, None, machine_id, name, platform, peers, pin_address=True)


def pair_by_qr(text: str, *, machine_id: bytes, name: str, platform: str, peers=None) -> dict:
    """Pairs from the text of a Beamer pairing QR (`beamer://pair?...`), which carries the host,
    the port, the code and a secret that strengthens it."""
    fields = pairing.read_qr(text)
    return _exchange(_resolve(fields["host"], fields["port"]), fields["port"], fields["code"],
                     fields["secret"], machine_id, name, platform, peers)


def _resolve(address: str, port: int) -> str:
    try:
        return socket.getaddrinfo((address or "").strip(), port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except (OSError, IndexError, UnicodeError) as exc:
        raise PairingError(f"no address for {address!r}") from exc


def _exchange(address: str, port: int, code: str, secret, machine_id: bytes, name: str,
              platform: str, peers, *, pin_address: bool = False) -> dict:
    listed = list(peers() if callable(peers) else (peers or []))
    if len(listed) >= pairing.MAX_PEERS:
        raise PairingError(f"this app already holds {pairing.MAX_PEERS} PCs; forget one first")

    try:
        sock = socket.create_connection((address, port), timeout=CONNECT_SECONDS)
    except OSError as exc:
        raise PairingError(f"nothing answered on {address}:{port} -- is Beamer running and showing a code?") from exc

    deadline = time.monotonic() + EXCHANGE_SECONDS
    with sock:
        try:
            beacon = pairing._recv_frame(sock, deadline)
            if beacon.get("type") != pairing.MSG_BEACON:
                raise PairingError("that port answered, but not as Beamer's pairing")
            version = beacon.get("pairing")
            if type(version) is not int or version != pairing.PAIRING_V3:
                raise PairingError(f"the PC pairs with version {version!r}, this app speaks {pairing.PAIRING_V3}")
            pair_id = beacon.get("pair")
            if not isinstance(pair_id, str) or not pair_id.isascii() or not 0 < len(pair_id) <= pairing.MAX_PAIR_ID_CHARS:
                raise PairingError("the PC is not showing a pairing code right now")

            client = pairing.PairingClient(
                pair_id, code, name, machine_id,
                version=pairing.PAIRING_V3, platform=platform, port=OUR_LINK_PORT,
                qr_secret=secret, peers=lambda: listed,
            )
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            pairing._send_frame(sock, client.start())

            answer = pairing._recv_frame(sock, deadline)
            if answer.get("type") != pairing.MSG_PAIR_ANSWER:
                raise PairingError("the PC did not answer the pairing request")
            try:
                confirm = client.accept(answer)
            except pairing.AlreadyPaired:
                _try_send(sock, client.abort())
                raise
            except PairingError:
                # The code has now had its one guess. Telling the PC makes it drop the code
                # rather than leave it live for the rest of its minute.
                _try_send(sock, client.abort())
                raise
            pairing._send_frame(sock, confirm)
            client.finish(pairing._recv_frame(sock, deadline))
        except OSError as exc:
            raise PairingError(f"the pairing connection failed: {exc}") from exc

    entry = client.entry(address)
    if pin_address and entry.get("host"):
        entry["host_pinned"] = True
    LOGGER.info("paired with %s (%s) at %s", entry.get("name"), entry.get("platform"), address)
    return entry


def _try_send(sock, message: dict) -> None:
    try:
        pairing._send_frame(sock, message)
    except OSError:
        pass


def error_text(exc: Exception) -> str:
    """Upstream raises `PairingError` carrying a wire reason word for the cases the protocol
    names; everything else already reads as a sentence."""
    reason = str(exc)
    return {
        pairing.ERROR_NOT_PAIRING: "the PC is not showing a pairing code right now",
        pairing.ERROR_REFUSED: "the code did not match -- ask the PC for a fresh one",
        pairing.ERROR_VERSION: "the PC's Beamer pairs with a different version",
        pairing.ERROR_KNOWN: "this app is paired with that PC already",
        pairing.ERROR_FULL: "there is no room for another PC",
        "no_answer": "nothing answered -- check the address and that Beamer is showing a code",
        "not_saved": "the pairing worked but could not be saved",
        "busy": "another pairing attempt is already running",
    }.get(reason, reason)
