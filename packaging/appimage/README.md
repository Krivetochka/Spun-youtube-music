# Spun AppImage

`build.sh` produces a mostly self-contained `Spun-x86_64.AppImage`.

## What is bundled

- The Spun app and the Qt 6.8 libraries it needs, including the **FFmpeg**
  multimedia backend, so local playback and YouTube streaming work without a
  system GStreamer.
- The optional **YouTube runtime**: a standalone Python with `ytmusicapi` and
  `yt-dlp`, the `bgutil` PO-token provider, and the **Node** and **Deno**
  binaries they use. Nothing extra needs installing for the YouTube features.

## Building

Run inside a clean Ubuntu 22.04 environment (a distrobox is ideal) so the binary
links an old glibc:

```sh
distrobox create --name spun-build --image ubuntu:22.04
distrobox enter spun-build -- bash /path/to/Spun/packaging/appimage/build.sh
```

The script installs its own build dependencies (via `apt`), fetches Qt with
`aqtinstall`, builds TagLib 2.x and Spun, assembles the runtime, and packs the
AppImage with `linuxdeploy` + `appimagetool`. Output and a reusable work tree
land in `~/spun-appimage-build` (override with `SPUN_APPIMAGE_WORKDIR`).

## First run

Make it executable and run it:

```sh
chmod +x Spun-x86_64.AppImage
./Spun-x86_64.AppImage
```

For YouTube: open the **Account** tab, sign in by pasting request headers from a
signed-in `music.youtube.com` session (see the main README), and playback will
read live cookies from your browser.
