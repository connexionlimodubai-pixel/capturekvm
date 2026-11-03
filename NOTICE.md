# Third-party code and licensing

## CaptureKVM is GPL-3.0, and has to be

`beamer-vigem/` is a git subtree of a fork of
[kalkman-code/beamer](https://github.com/kalkman-code/beamer), which is **GPL-3.0-or-later**:

The whole of Beamer is in there, upstream's history included, plus this project's own gamepad
capability on top (`beamer-vigem/FORK.md` lists exactly what was added). The parts the client
leans on are:

| File | What it is |
|---|---|
| `core/protocol.py` | wire version 6: the handshake, the keys, the frames, every message builder and validator |
| `core/pairing.py` | the CPace pairing exchange |
| `core/keytable.py` | WIRE.md section 7's key names and the cross-family modifier mapping |
| `core/gamepad.py` | the pad messages — this project's addition, not upstream's |
| `core/receiver.py` | the responder; the client's end-to-end check runs the real one |

- Upstream version: see `beamer-vigem/VERSION`
- The subtree came in with its history whole — 44 commits back to Beamer 1.2.0. Note that no
  path-filtered `git log -- beamer-vigem` will show them: the imported commits hold those files at
  the repository root, so path filtering cannot follow them across the import. Walk the merge's
  second parent instead:

  ```bash
  git log --oneline 56be2eb^2      # the subtree-add merge
  ```
- `LICENSE` in this repository is upstream's GPL-3.0 text.

The Windows app imports that package directly, so it is a derivative work and this whole project
is GPL-3.0-or-later. That is fine for private use and for sharing the source; it does mean you
cannot ship a closed-source build of it.

**Why vendor rather than reimplement.** The wire's keys are bound to both preambles and the
direction, and the pairing is a password-authenticated exchange where a single wrong byte anywhere
in the transcript agrees nothing and explains nothing. Using the code that is known to work is the
only way to be sure the Windows client is correct, and it means upstream's validation — every
length check, every canonical-base64 check, every malformed-field rule — applies here too.

**Why the Android app does not vendor it.** It cannot: that code is Python. The Kotlin under
`android/app/src/main/java/com/capturekvm/wire/` is a transcription of the same modules, which is
still a derivative work of them. `tools/make_vectors.py` generates test vectors from the vendored
Python and `WireVectorsTest` asserts the Kotlin against them, so the transcription is checked
rather than hoped for.

## Other dependencies

Not redistributed here — resolved at build time.

| Dependency | Licence | Used by |
|---|---|---|
| [UVCAndroid](https://github.com/shiyinghan/UVCAndroid) (`com.herohan:UVCAndroid`) | Apache-2.0 (bundles libusb — LGPL-2.1, libuvc — BSD-3-Clause, libjpeg-turbo) | Android: UVC capture and preview |
| [Bouncy Castle](https://www.bouncycastle.org/) (`bcprov-jdk18on`) | MIT-style (Bouncy Castle Licence) | Android: X25519, ChaCha20-Poly1305 |
| [PySide6](https://doc.qt.io/qtforpython/) | LGPL-3.0 | Windows: the window |
| [PyNaCl](https://github.com/pyca/pynacl) | Apache-2.0 (bundles libsodium — ISC) | Windows: X25519, ChaCha20-Poly1305, via the vendored core |
| [OpenCV](https://opencv.org/) (`opencv-python`) | Apache-2.0 | Windows: UVC capture through Media Foundation and DirectShow |
| [ViGEmClient](https://github.com/nefarius/ViGEmClient) (`ViGEmClient.dll`, x64, as shipped in [vgamepad](https://github.com/yannbouteiller/vgamepad) 0.2.0) | MIT | Windows host mode: plugging the virtual Xbox pad into the ViGEmBus driver. Fetched at build time by `tools/fetch_vigem_client.py`, pinned by SHA-256, not committed |
| [sounddevice](https://python-sounddevice.readthedocs.io/) (bundles PortAudio — MIT) | MIT | Windows: playing the capture card's audio |
| AndroidX, Material Components, kotlinx-coroutines | Apache-2.0 | Android |

LGPL note: PySide6 and libusb are both LGPL and both are used as separate shared libraries that the
user can replace, which is what the LGPL asks for.

## Beamer itself

Beamer is Toby George Read Kalkman's work (Kalkman Code). This project is not affiliated with it
and is not endorsed by it. It is a client that speaks Beamer's protocol; the gamepad messages in
`docs/PAD-PROTOCOL.md` are **not** part of upstream Beamer and exist only to talk to a fork that
adds a ViGEmBus target.
