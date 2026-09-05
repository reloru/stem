# stem

Upload a track, get its four stems, adjust them in a browser mixer, download
what you made. Self-hosted: the audio never leaves the machine you run this on.

Four stems come out — `vocals`, `drums`, `bass`, `other` — as 44.1 kHz 16-bit
stereo WAV, individually or as one zip. The mixer plays all four in sync with a
fader, mute and solo per stem; exporting renders the balance server-side from
the lossless stems, so nothing you download has been through the lossy copies
the browser uses for playback.

## How it works

```
upload ──▶ ffmpeg ──▶ audio-separator ──▶ ffmpeg ──▶ ffmpeg
           decode      htdemucs            16-bit    192 kbps MP3
           44.1k/16    4 × WAV             normalise  previews
                                                          │
                          browser ◀── 4 × MP3 ◀────────────┘
                          Web Audio: 4 sources ▶ 4 gains ▶ monitor ▶ out
                                                          │
                          export ──▶ POST gains ──▶ ffmpeg measures the summed
                                                    peak, applies one constant
                                                    attenuation if it would
                                                    clip, then sums to WAV/MP3
```

Separation is neural, so a model and a runtime are unavoidable — that is the
one dependency this project has. Everything else is the Python standard library
and ffmpeg: the HTTP server is `http.server`, the multipart parser is
hand-written (`cgi` is gone as of Python 3.13 and `email` buffers whole bodies
in memory), and the front end is plain HTML, CSS and JavaScript with no build
step and no framework.

**Why `audio-separator` rather than Demucs directly.** Meta archived
[facebookresearch/demucs](https://github.com/facebookresearch/demucs) on
2025-01-01 and its author states it is no longer maintained.
[nomadkaraoke/python-audio-separator](https://github.com/nomadkaraoke/python-audio-separator)
is MIT, actively maintained, and runs the same `htdemucs` weights, so you get
the model without depending on an archived repository.

**Why a measured attenuation instead of a limiter on export.** A limiter
applies gain reduction that varies over time, changing the dynamics you
balanced, and ffmpeg's `alimiter` also delays its output by its lookahead
window — measured at 219 samples (5.0 ms) at the default attack — which would
leave every exported mix out of alignment with the stems it came from. Instead
the summed peak is measured in floating point first, and a single constant gain
is applied only if the sum would exceed −0.3 dBFS. The API reports how much was
applied.

## Requirements

- Python 3.10 or newer (3.12 on Ubuntu 24.04 is fine)
- `ffmpeg` and `ffprobe` — `sudo apt install -y ffmpeg`
- Disk for the virtualenv, plus 81 MB of model weights and job storage
- 4 GB of RAM free while a job runs

The virtualenv measured **6.0 GB on x86_64**, because PyPI's default `torch`
wheel for that platform bundles CUDA libraries even under the `[cpu]` extra —
the install here reported `2.14.0+cu130`. No CUDA wheels exist for
`manylinux aarch64`, so an ARM install pulls the CPU-only build and will be
considerably smaller; that size was not measured. Linux `aarch64` wheels are
published for `torch`, `onnxruntime`, `numpy` and `soundfile`, so the install
works from wheels on an ARM box with no compiler.

`setup.sh` also installs `audioread` explicitly, alongside `audio-separator`
rather than as part of it. `audio_separator/separator/uvr_lib_v5/spec_utils.py`
imports `audioread` directly at module load, but `audio-separator`'s own
dependency list never declares it — checked against its PyPI metadata, which
lists `librosa>=0.10` with no upper bound and nothing else that would supply
it. It was reaching the environment only because `librosa` used to depend on
it too. `librosa`'s newest release, `1.0.0` — also checked directly against
its PyPI metadata — dropped `audioread` from its own dependencies, so
whether it ends up installed at all now depends on which `librosa` version
pip's resolver happens to pick, which is exactly the kind of thing that
differs between two installs of the same command on two different machines.
Installing it ourselves removes the dependency on that resolver outcome.

## Install

```
bash setup.sh
```

It creates `.venv`, installs `audio-separator[cpu]`, writes a `.env` with a
freshly generated access key, downloads the `htdemucs` weights, and verifies
that every external tool answers. Re-running it keeps an existing `.env`.

## Run

```
bash run.sh
```

As a service:

```
sudo cp deploy/stem.service /etc/systemd/system/stem.service
sudo systemctl daemon-reload && sudo systemctl enable --now stem
```

Edit the paths and `User` in the unit if your checkout is not at
`/home/ubuntu/stem`.

The unit's `ProtectHome=read-only` combined with `ReadWritePaths=.../data`
locks out both locations `librosa`'s `numba` JIT compiler tries for its disk
cache (in-tree next to the source file, and a `$HOME`-based fallback) at the
same time. Unlike a plain read-only filesystem -- where numba degrades to
running uncached -- losing *both* candidate locations at once makes numba
raise instead: `RuntimeError: cannot cache function '__o_fold': no locator
available for file .../librosa/core/notation.py`, surfacing as a bare
`audio-separator exited with code 1`. Reproduced directly by remounting a
venv and `$HOME` read-only together in an isolated mount namespace with a
cold numba cache, and fixed by pointing `NUMBA_CACHE_DIR` at a directory
under `STEM_DATA_DIR` (already covered by `ReadWritePaths`) before the
separator subprocess is ever spawned, so it inherits the setting. This needs
no unit file changes and weakens none of the sandboxing.

## Reaching it from a phone

The server binds `127.0.0.1` by default and speaks plain HTTP. Do not change
that to `0.0.0.0` and open the port — the access key would cross the network in
clear text. Put something in front of it:

- **SSH forward**, nothing to install: `ssh -L 8080:127.0.0.1:8080 you@box`,
  then open `http://127.0.0.1:8080`. Fine from a laptop, awkward from a phone.
- **Cloudflare Tunnel**, if you want a URL that works from anywhere: install
  `cloudflared` (a different binary from `wrangler`) and point a tunnel at
  `http://127.0.0.1:8080`. TLS and the public hostname are handled for you and
  no inbound port opens on the box.
- **A reverse proxy** you already run, terminating TLS.

Once it's reachable over HTTPS, Chrome on Android will offer **Install app**
from its menu (the page ships a manifest, so this needs no extra step here).
Installing removes the address bar and gives the mixer a home-screen icon —
real screen space back on a phone. This relies on HTTPS, with one exception the W3C Secure Contexts spec carves
out for loopback addresses: a plain SSH-forwarded `http://127.0.0.1:8080` is
still a secure context, so the install option is available there too, tunnel
or not. iOS Safari
reads the `apple-mobile-web-app-*` tags in `index.html` for the same effect
via *Add to Home Screen*, but that path was not tested — your friend is on
Android, where it was.

## Access model

Two tiers, matching an unlisted-link app for a couple of people:

- **The access key gates anything that costs CPU or creates state** — uploading,
  exporting a mix, deleting a job. It is sent as `X-Stem-Key` and compared with
  `hmac.compare_digest`. The browser stores it in `localStorage`.
- **A job's audio is gated by its id**, 192 bits from `secrets.token_urlsafe`,
  which appears in no listing. Anyone you send a `#job=…` link to can play and
  download that job's stems without the key. That is deliberate — it is how you
  share a result — but it means a job link is the audio.

Jobs are deleted after `STEM_JOB_TTL_HOURS`, checked every 10 minutes and on
startup.

## Configuration

Everything is read from the environment at startup; `setup.sh` writes `.env`
and `run.sh` and the systemd unit both load it.

| Variable | Default | Meaning |
| --- | --- | --- |
| `STEM_ACCESS_KEY` | — | Required, minimum 12 characters. Startup fails without it. |
| `STEM_ALLOW_OPEN` | `0` | Set to `1` to run with no key at all. |
| `STEM_HOST` | `127.0.0.1` | Bind address. |
| `STEM_PORT` | `8080` | Bind port. |
| `STEM_DATA_DIR` | `./data` | Jobs and downloaded models. |
| `STEM_MODEL_DIR` | `<data>/models` | Model weights. |
| `STEM_WEB_DIR` | `./web` | Static front end. |
| `STEM_MODEL` | `htdemucs.yaml` | Any four-stem model `audio-separator` knows. |
| `STEM_MAX_UPLOAD_MB` | `100` | Rejected before the body is read. |
| `STEM_MAX_DURATION_S` | `300` | Checked with ffprobe after upload. Set with the mobile mixer's memory cost in mind — see below. |
| `STEM_JOB_TTL_HOURS` | `24` | Age at which a job's directory is deleted. |
| `STEM_WORKERS` | `1` | Concurrent separations. Raising this on a 4-core box makes both jobs slower without finishing either sooner. |
| `STEM_PREVIEW_BITRATE` | `192k` | Playback copies only; downloads are unaffected. |
| `STEM_SEPARATOR_TIMEOUT_S` | `3600` | Separation is killed past this. |
| `STEM_FFMPEG`, `STEM_FFPROBE`, `STEM_SEPARATOR_BIN` | from `PATH` | Explicit binary paths. |

`python -m stemapp --check` prints the resolved configuration and verifies the
external tools without starting the server.

## API

`K` marks routes that require `X-Stem-Key`.

| | Route | |
| --- | --- | --- |
| `GET` | `/api/config` | Limits, stem names, accepted formats. |
| `POST` | `/api/key` | K — verifies a key. |
| `POST` | `/api/jobs` | K — `multipart/form-data`, field `file`. Returns the job. |
| `GET` | `/api/jobs/{id}` | State, progress, duration, error. |
| `DELETE` | `/api/jobs/{id}` | K — deletes the job and its files. |
| `GET` | `/api/jobs/{id}/preview/{stem}.mp3` | Playback copy. Supports `Range`. |
| `GET` | `/api/jobs/{id}/stems/{stem}.wav` | Lossless stem. |
| `GET` | `/api/jobs/{id}/stems.zip` | All four, built on first request. |
| `POST` | `/api/jobs/{id}/mix` | K — `{"gains":{…},"format":"wav"\|"mp3"}`. Returns a URL plus the measured peak and any attenuation applied. |
| `GET` | `/api/jobs/{id}/mix/{mix}.{fmt}` | The rendered mix. |

A job moves through `queued → preparing → separating → encoding → done`, or to
`error` with a message. During `separating`, `progress` is the percentage of
the current model pass and `separation_pass` says which pass that is —
`htdemucs` runs the model over the track more than once and the underlying
progress bar restarts each time.

## Mixer

Faders travel from −48 dB to +12 dB with unity at 0.8 of the way up; dragging
snaps to unity, keyboard steps do not. Double-click or press Enter on a fader to
reset it. Waveforms share one vertical scale across the four stems, so a stem
that is genuinely quiet looks quiet.

| Key | |
| --- | --- |
| Space | play / pause |
| ← → | seek 5 seconds |
| 1–4 | solo that stem |
| Shift+1–4 | mute that stem |

The monitor fader is playback only and is not part of an export.

**Instrumental** is a fixed-preset export next to the regular mix export:
vocals out, everything else at unity, ignoring whatever the faders currently
say. It reuses the same server-side mixdown path as a regular export (same
clip protection, same lossless-stem source) with the gains hardcoded rather
than read from the mixer state.

Fader positions, mutes and solos are written to `localStorage` as you set them
and restored when you reopen the same job. That is there for phones: a mobile
browser discards backgrounded tabs under memory pressure, and this is an
expensive tab to keep, so without it every adjustment would be lost the moment
someone switched apps. The last 20 tracks are remembered; nothing leaves the
browser.

Installed as a home-screen PWA, this same data does a second job. A PWA's
manifest `start_url` is fixed -- relaunching from the icon always lands there,
with none of the last-open-tab restoration a normal browser tab gets, so
without a fallback every relaunch would land back on the bare upload screen
with no way back to a job in progress or already finished. A cold launch with
no job in the URL now looks up the most recently touched job from that same
`localStorage` record and resumes it automatically; only an explicit **New
track** clears it. The **link icon** in the top bar copies that job's URL, for
opening the same job in an ordinary browser tab or handing it to someone
else -- there is no address bar to read it from inside an installed PWA.

That second point matters on iOS specifically: exporting a file from inside
the installed PWA can leave Safari stuck on an unclosable Quick Look-style
sheet instead of a normal download. This is a long-standing WebKit limitation
scoped to standalone/installed PWA mode -- the identical `download` attribute
works cleanly in an ordinary Safari tab -- confirmed against independent
reports spanning iOS 12 through current releases, not specific to anything
this app does; the documented workarounds all depend on an undocumented Apple
URL scheme that isn't something to build the export path around. If exporting
locks up, use the copied link in regular Safari rather than the installed
icon.

## Disk per job

For a five-minute track, roughly: 210 MB of stems, 30 MB of previews, and
another 210 MB once someone downloads the zip. The decoded source WAV is
deleted as soon as the stems exist. Everything goes when the TTL expires.

## What was measured, and what was not

Measured in a 4-core x86_64 container with 15 GB of RAM:

| Input | End to end |
| --- | --- |
| 20.0 s | 34.2 s |
| 240.0 s | 420.0 s |

Both numbers cover decode, separation, 16-bit normalisation and MP3 previews.
The short one is dominated by a fixed model-loading cost, so the 4-minute
figure — **about 1.75× the length of the track** — is the one worth planning
around on comparable hardware.

- Summing the four stems at unity reproduced the input with a **−20.1 dB**
  residual at **zero sample lag**. That was a synthetic signal — sine tones and
  filtered noise — which is nothing like the material `htdemucs` was trained
  on, so the residual on real music will differ and was not measured.
- Export clip protection: against a deliberately low −25 dB test ceiling the
  renderer applied −8.41 dB and the output landed at −24.998 dBFS.
- The front end was driven end to end in Chromium at 1440×900, 390×844 and
  844×390: four strips, vertical faders on desktop and horizontal in the phone
  layout, waveforms drawn, transport advancing, mute and solo, WAV export,
  balances surviving a reload — no console errors, no CSP violations, no
  horizontal overflow at any of the three sizes.

### The mixer's memory cost scales with track length

Opening a 4-minute track in a 390×844 mobile-Chrome context, measured as
resident memory across every Chromium process:

| | |
| --- | --- |
| Network to reach a playable mixer | 22.0 MB (4 × 5.76 MB MP3) |
| Time to playable, on localhost | 6.7 s |
| RSS attributable to the mixer | 511 MB |

Roughly 339 MB of that is the four decoded stems and is not
implementation-specific: `AudioBuffer` holds 32-bit float per sample per
channel, so four stereo stems at the 44.1 kHz context rate cost
`4 × 240 × 44100 × 2 × 4` bytes on any engine. The remaining ~172 MB is
Chromium's own overhead, measured on desktop Linux, and may differ on a phone.

It scales linearly, so `STEM_MAX_DURATION_S` now defaults to **300** rather
than the 600 it shipped with. Projecting the measured 240 s figure forward
linearly (339 MB ÷ 240 s ≈ 1.41 MB/s of decoded audio) — this is arithmetic
from the one measurement above, not a separate measurement at 300 s — a track
at the new cap needs roughly 424 MB of decoded audio plus the ~172 MB
Chromium overhead, call it **~600 MB** total. That is still real weight for a
phone to hold in one tab, just no longer the ~1 GB a 10-minute upload would
have demanded under the old default. If a track near the cap gets a tab
killed in practice, the actual fix is changing the mixer to stream from
`<audio>` elements instead of decoding whole buffers, not lowering the limit
further — halving it again would start constraining ordinary song lengths.

### Not measured

Anything on ARM. The wheel availability above says the install will work on
`aarch64`; separation speed on an Ampere A1 core is unknown and has to be timed
on the box.

Anything on WebKit. Chromium covers Chrome on Android, which shares its engine.
It does not cover Chrome on iOS: Apple's App Review Guideline 2.5.6 states
*"Apps that browse the web must use the appropriate WebKit framework and WebKit
JavaScript,"* with alternative-engine entitlements limited to the EU and Japan,
so iOS Chrome is Safari's engine wearing a different icon. No Firefox run
either.

## Licence

MIT, see `LICENSE`. `audio-separator` and the `htdemucs` weights carry their own
licences; check them before doing anything commercial with the output.
