# Third-Party Licenses

Render Mapper Pro bundles the following third-party software.

## FFmpeg / FFprobe

This application distributes static **FFmpeg** and **FFprobe** binaries
(`ffmpeg`, `ffprobe`) used for media probing and frame extraction.

- **Project:** FFmpeg — https://ffmpeg.org
- **Build used:** static GPL builds from
  [`eugeneware/ffmpeg-static`](https://github.com/eugeneware/ffmpeg-static)
  (release `b6.0`; overridable at build time via the `FFMPEG_STATIC_VERSION` /
  `FFMPEG_STATIC_BASE` environment variables — see `tools/fetch_ffmpeg.py`).
- **License:** these are **GPL** builds, distributed under the
  **GNU General Public License, version 3** (or, at your option, any later
  version). The full license text is available at
  https://www.gnu.org/licenses/gpl-3.0.html

### Written offer for source code

In accordance with the GPL, the complete corresponding source code for the
bundled FFmpeg build is available from the FFmpeg project
(https://ffmpeg.org/download.html) and from the build scripts and release
artifacts at https://github.com/eugeneware/ffmpeg-static. On request, Toy Robot
Media will also provide the corresponding source for the exact version shipped
with this application.

> Note: FFmpeg is bundled and invoked as a separate executable; it is not linked
> into the application. The application's own source is the property of Toy Robot
> Media and is not placed under the GPL by this bundling.

## PySide6 / Qt

The user interface is built with **PySide6** (the official Python bindings for
the Qt framework), distributed under the **GNU Lesser General Public License
(LGPL) v3**. See https://www.qt.io/licensing and
https://www.gnu.org/licenses/lgpl-3.0.html
