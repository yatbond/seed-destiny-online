# SEED Destiny Online 0.3.2 — Windows experimental release

[Download the Windows installer](https://github.com/yatbond/seed-destiny-online/releases/tag/v0.3.2).
This prerelease adds display synchronization for Enhanced mode. Visual confirmation is pending; missing scenery and separate-PC Internet qualification remain outstanding. Bring your own disc ISO and dongle BIN.

Runs the original NM00034 / GSD1 **SEED Destiny: Rengou vs. Z.A.F.T. II** with a modified Play! emulator and private, passcode-protected four-player rooms. Each player supplies their original disc ISO and dongle BIN. **No separate arcade BIOS is required.**

Local four-instance 2-on-2 combat has been verified through the encrypted room relay, including movement, attacks and damage. A fresh-profile launcher test completed a timed round and showed normal results on all four cabinets. Enhanced 3× now synchronizes presentation to the display to address reported flashing. The driver setting is verified; visual confirmation is pending and missing scenery remains unresolved. Original Vulkan remains recommended. Internet gameplay between separate PCs remains unverified; router reachability and real Internet latency need an external test.

## Install and play

Use `dist/SeedDestinyOnline-Setup-0.3.2.exe`. Installation is per Windows account, with no administrator rights needed. It includes the launcher, emulator, libraries and corresponding source archives. Original game files and saved machine states are excluded. The older 0.2.1 installer is superseded.

1. Open **Game & graphics**, select your disc ISO and original dongle BIN, and save.
2. In **Playroom**, paste the host's invitation. Enter your name and separately supplied passcode, then join.
3. Select **Launch my game**. First launch initializes cabinet settings automatically; allow up to a minute. Cabinet IDs 1–4 follow the room seats.
4. Wait for all four players to launch and finish linking. Insert credit, start, and select teams and arena in the original game menus. The launcher's Ready/start buttons only coordinate players.

Keyboard: **Backspace** inserts credit, **Enter** starts, arrows move, and **A / S / X / Z** map to Square / Triangle / Circle / Cross. Configure a gamepad in the emulator's controller settings. **Alt+Enter** toggles fullscreen. Avoid saved machine states during linked play.

**Original · Vulkan** uses native resolution and is the recommended mode. **Enhanced 3× · OpenGL** uses 1920 × 1344, preserving the original 4:3 shape. It sharpens existing assets; no replacement textures, lighting or geometry are included. Version 0.3.2 enables display synchronization before the enhanced window is created, replacing unsynchronized presentation. The NVIDIA driver reports swap interval 1 and double buffering. This is a targeted change for reported flashing; a successful automated run does not prove temporal visual stability. User confirmation is pending. Black regions in some distant scenery remain unresolved. Original Vulkan remains the stable option.

Separate cabinet settings and saves live under `%LOCALAPPDATA%/SeedDestinyOnline/players/seat-N/Play/`. Original disc/dongle files are read-only inputs. Uninstalling preserves settings and saves. Avoid two instances using the same seat profile. Leaving a room disconnects the game link but keeps the game window open.

## Host a private Internet room

In **Host on this PC**, enter your reachable Internet address or DNS name and TCP port **7443**, then start the encrypted server. Create a room in Playroom with a passcode of at least eight characters. Copy the invitation and share it privately; send the passcode separately. Keep the host app and PC running.

Forward TCP 7443 to the host PC in the router and allow the launcher through Windows Firewall on the appropriate network. The installer does not change router, firewall or VPN settings. Carrier-grade NAT or VPN routing may prevent inbound access. Test from a different Internet connection before arranging a match. Do not expose the emulator's loopback bridge port.

The host connects locally. Invitations contain the advertised Internet address and public certificate, never the private key or passcode. Address changes or certificate renewal require fresh invitations. Certificates are stored under the app's settings folder. The **Local test server** accepts connections only from the same PC.

The source server requires Python 3.11+ and cryptography:

```sh
python -m seedrooms.transport --host 0.0.0.0 --port 7443 --cert fullchain.pem --key privkey.pem
```

Passcodes use salted scrypt hashes; remote connections require certificate-verified TLS. Rooms support four players, isolated traffic, host transfer, readiness and explicit rejoin. No game files are transferred. Rooms disappear when the last player leaves or the server restarts. The relay preserves arcade synchronization; it does not add rollback netcode. Latency can affect speed.

## Validation and limits

- `evidence/play-linked-battle300.json`: four actual emulators, encrypted room, separate viewpoints, opposing pairs, movement, melee and damage; normal exits.
- `evidence/play-linked-round420.json`: longer linked run returned all four games to attract without bridge errors; no intermediate result screen in that run.
- `evidence/play-opengl-hd180.json`: 1920 × 1344 mobile suits, terrain and HUD, advancing combat on RTX 5090.
- `evidence/play-launcher-four420.json`: fresh automatic preparation and actual launcher launches; completed timed round, normal results on all four cabinets, 136,443 frames sent and 409,329 delivered, no bridge errors. One OpenGL 3× player and three Vulkan native players. An earlier run with ignored settings produced a result-screen communication error; the corrected configuration passed, but the individual causal setting has not been isolated.
- `evidence/play-network-clock-fix.json`: HLE clock status correction restoring the original network maintenance timers.
- `tests/`: socket/TLS authentication, room isolation, framing, invitations, profile isolation and GUI room-controller checks.

Run `python -m unittest discover -s tests -v` here. Tests use temporary local ports and do not launch the game. `SeedDestinyOnline.exe --self-test output.json` checks installed dependencies and four-client encrypted transport; it is not a game or Internet test. Original binaries, saved game states and investigation screenshots are excluded from distribution.

Outstanding checks include separate-PC Internet matches, actual gameplay disconnect/rejoin, other GPUs, and clean-machine installation beyond this development PC. A local installer does not imply a published download or public room server.

## Source and credits

Based on [Play!](https://github.com/jpd002/Play-) commit `d44bfa1814b729d4a3ed3c6c3a945432d5ee5bbe`, with a USB Ethernet model adapted from [PCSX2x6](https://github.com/boostedmans-hub/pcsx2x6/blob/743953a3905b4fe482cd62e72bd724015ea28880/pcsx2/USB/usb-eth/usb-uepcb.cpp). The combined modified emulator is GPL-3.0-or-later; original Play! BSD notices are retained. Qt 5.15.2 is dynamically linked.

Corresponding source, build instructions and notices are installed under `sources` and `licenses`. This includes patched Play! and submodules, Qt Base 5.15.2, zlib 1.3.2 and launcher source. Compatible modified Qt DLLs can replace the supplied libraries. Microsoft runtime DLLs are redistributables. Preserve sources and notices when sharing. See `release/PlayBuild.txt`.

[REA](https://github.com/morluto/rea) informed artifact inspection. Available Windows native analysis did not support this MIPS target; source review and bounded local diagnostics supplied additional evidence. Original game code is not patched or redistributed. This is an unofficial community project.


The launcher source is licensed under GPL-3.0-or-later. See COPYING. Bundled third-party components retain their own notices.
