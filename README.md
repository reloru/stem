# stem

Upload a track, get its stems, adjust them in a browser mixer, download what
you made. Self-hosted: the audio never leaves the machine you run this on.

Two separations are offered, chosen per track at upload:

| | Stems |
| --- | --- |
| **4 stems** (`htdemucs`) | `vocals` `drums` `bass` `other` |
| **6 stems** (`htdemucs_6s`) | `vocals` `drums` `bass` `guitar` `piano` `other` |

They are different decompositions rather than one extending the other: the
six-stem model carves `guitar` and `piano` out of what the four-stem model
calls `other`, so a six-stem `other` is not a four-stem `other`. Both sum back
to the source. A job records which model separated it and keeps that layout
for its lifetime.

Stems come out as 44.1 kHz 16-bit stereo WAV, individually or as one zip. The
mixer plays them in sync with a fader, mute and solo per stem; exporting
renders the balance server-side from the lossless stems, so nothing you
download has been through the lossy copies the browser uses for playback.

**On the six-stem model's quality.** demucs' own README reports: *"Quick
testing seems to show okay quality for `guitar`, but a lot of bleeding and
artifacts for the `piano` source."* That is the model authors' assessment, not
a limitation of this app. That quality caveat, not CPU cost, is why the model
is a per-track choice: six-stem separation was measured at roughly 9% *faster*
than four-stem on the deployment box (see below), so there is no performance
reason to avoid it.

## How it works

```
upload ──▶ ffmpeg ──▶ audio-separator ──▶ ffmpeg ──▶ ffmpeg
           decode      htdemucs[_6s]       16-bit    128 kbps MONO
           44.1k/16    4 or 6 × WAV        stereo    MP3 previews
                                           normalise      │
                          browser ◀── N × MP3 ◀────────────┘
                          Web Audio: N sources ▶ N gains ▶ monitor ▶ out
                                                          │
                          export ──▶ POST gains ──▶ ffmpeg measures the summed
                                                    peak, applies one constant
                                                    attenuation if it would
                                                    clip, then sums to WAV/MP3
```

The previews the browser streams are mono; everything you can download —
individual stems, the zip, every rendered mix — is stereo. See *The mixer's
memory cost* below for why.

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
- Disk for the virtualenv, plus 133 MB of model weights (84 MB `htdemucs`,
  55 MB `htdemucs_6s`; `setup.sh` fetches both) and job storage
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
or not. iOS Safari reads the `apple-mobile-web-app-*` tags in `index.html`
for the same effect via *Add to Home Screen*.

Both platforms have now actually been used in the field, not just reasoned
about. Android (Chrome, Galaxy S24 Ultra and Z Flip 6) worked end to end on
first deploy: upload, separation, mixer, and WAV stem downloads, no issues
reported. iOS (installed PWA, iPhone with Dynamic Island) surfaced two real
bugs that plain reasoning about the manifest spec would not have caught,
both now fixed and both covered in the Mixer section below: the top bar
rendering invisibly behind the status bar/Dynamic Island, and a relaunch of
the installed app losing the current job outright. A third iOS issue —
exporting a file from inside the installed PWA can leave Safari stuck on an
unclosable Quick Look sheet — is a confirmed WebKit limitation with no code
fix available; see the Mixer section.

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
| `STEM_MODEL` | `htdemucs.yaml` | Which model an upload gets when it does not pick one. Must be one of the registered models (`htdemucs.yaml`, `htdemucs_6s.yaml`); anything else is fatal at startup. |
| `STEM_MAX_UPLOAD_MB` | `100` | Rejected before the body is read. |
| `STEM_MAX_DURATION_S` | `300` | Checked with ffprobe after upload. Set with the mobile mixer's memory cost in mind — see below. |
| `STEM_JOB_TTL_HOURS` | `24` | Age at which a job's directory is deleted. |
| `STEM_WORKERS` | `1` | Concurrent separations. Raising this on a 4-core box makes both jobs slower without finishing either sooner. |
| `STEM_PREVIEW_BITRATE` | `128k` | Playback copies only; downloads are unaffected. Previews are mono, so this spends more bits per channel than the 192 kbps stereo it replaced. |
| `STEM_SEPARATOR_TIMEOUT_S` | `3600` | Separation is killed past this. |
| `STEM_FFMPEG`, `STEM_FFPROBE`, `STEM_SEPARATOR_BIN` | from `PATH` | Explicit binary paths. |

Models are a fixed registry (`server/stemapp/config.py`) rather than a string
passed through to `audio-separator`, because nothing downstream works without
knowing a model's stem names in advance: they appear in URLs, in on-disk
paths, in the job record and in the mixer's channel list. Adding a model means
adding its stem order and its `--custom_output_names` mapping there — after
which `setup.sh` fetches its weights and the upload picker offers it without
further changes.

`python -m stemapp --check` prints the resolved configuration and verifies the
external tools without starting the server.

## Tests

```
bash test.sh
```

Standard-library `unittest`, no ffmpeg, no `audio-separator`, no network and no
data directory — so it runs on the deployment box itself, between `git pull`
and `sudo systemctl restart stem`, on the same Python the service uses. It
covers the multipart parser, `Range` parsing, filename sanitisation, error
scrubbing, the mixdown filter graph at both stem counts, configuration and
model resolution, and the job store's identity, expiry and restart behaviour.
There is no CI; running this before a restart is what catches a regression.

## API

`K` marks routes that require `X-Stem-Key`.

| | Route | |
| --- | --- | --- |
| `GET` | `/api/config` | Limits, the available models and their stems, accepted formats. |
| `POST` | `/api/key` | K — verifies a key. |
| `POST` | `/api/jobs` | K — `multipart/form-data`, field `file`, optional field `model` naming one of the models `/api/config` lists. Returns the job. |
| `GET` | `/api/jobs/{id}` | State, progress, duration, error. |
| `DELETE` | `/api/jobs/{id}` | K — deletes the job and its files. |
| `GET` | `/api/jobs/{id}/preview/{stem}.mp3` | Playback copy. Supports `Range`. |
| `GET` | `/api/jobs/{id}/stems/{stem}.wav` | Lossless stem. |
| `GET` | `/api/jobs/{id}/stems.zip` | Every stem this job has, built on first request. |
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
reset it. Waveforms share one vertical scale across the stems, so a stem
that is genuinely quiet looks quiet.

| Key | |
| --- | --- |
| Space | play / pause |
| ← → | seek 5 seconds |
| 1–6 | solo that stem |
| Shift+1–6 | mute that stem |

The monitor fader is playback only and is not part of an export.

A fader resets on Enter or a double-click, deliberately not on Space. Dragging
a fader leaves it focused, and a keystroke there reaches both the fader's own
handler and the global shortcut handler — so while the fader also treated
Space as a reset, one press snapped the fader you had just set back to unity
*and* toggled the transport. Space is play/pause everywhere now, focused fader
or not.

**Instrumental** is a fixed-preset export next to the regular mix export:
vocals out, everything else at unity, ignoring whatever the faders currently
say. It reuses the same server-side mixdown path as a regular export (same
clip protection, same lossless-stem source) with the gains hardcoded rather
than read from the mixer state. It is written against whatever stems the job
has, so on a six-stem job it keeps guitar and piano too.

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

The viewport tag's `viewport-fit=cover` -- needed so the installed app fills
the screen edge-to-edge instead of looking letterboxed -- draws content
underneath the notch/Dynamic Island and status bar unless something explicitly
reserves that space back with `env(safe-area-inset-*)`. The top bar wasn't
doing that: reported from the field as the bar being invisible and untappable
at rest, briefly appearing mid-drag during a pull-down overscroll and
vanishing again on release -- the bar was rendering *behind* the status bar
the whole time, and the drag was only ever exposing it, never fixing
anything. Fixed by padding the top bar with `env(safe-area-inset-top)` (and
left/right, for landscape). Verified to change nothing on every platform this
project's own testing can reach -- Chromium has no notch to clear, so the
added padding evaluates to its zero fallback there -- but actually clearing a
real notch or Dynamic Island could not be confirmed in that environment and
needed on-device confirmation.

## Disk per job

For a five-minute four-stem track, roughly: 210 MB of stems, 30 MB of
previews, and another 210 MB once someone downloads the zip. A six-stem job is
half again as much stem and zip data — around 315 MB each — while previews stay
near 30 MB, since going mono offsets the two extra stems. The decoded source
WAV is deleted as soon as the stems exist. Everything goes when the TTL
expires.

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

Measured when six-stem support was added, on the same x86_64 hardware:

- A real `htdemucs_6s` job ran through the whole app — upload, probe, decode,
  separation over two passes, 16-bit normalisation, previews — and produced
  six stereo stems and six mono previews. **6.0 s of audio took 38.5 s**, which
  is model-loading cost, not a throughput figure; no longer input was timed.
- The `--custom_output_names` mapping was confirmed by running the separator,
  not only by reading it: `htdemucs_6s` wrote `vocals`, `drums`, `bass`,
  `guitar`, `piano` and `other` under exactly those filenames.
- A six-stem mixdown at unity came out **bit-for-bit identical** to the same
  six stems summed by an independent `ffmpeg amix=normalize=0` invocation.
- Clip protection at the production −0.3 dBFS ceiling: a six-stem sum peaking
  at +2.14 dBFS was attenuated by −2.44 dB and the output measured
  **−0.300 dBFS**.
- A `job.json` written before the `model` field existed still loads, reports
  the four-stem layout, serves its stems and renders a mix; requesting
  `guitar` from it returns 404.
- Chromium at 1440×900 and 390×844 on a six-stem job: six strips, three
  columns wrapping to two rows on desktop, one column on the phone layout, all
  six waveforms sized, keys `5` and `6` reaching the new stems, and Space on a
  focused fader toggling transport without moving the fader. Four-stem jobs
  re-checked at both sizes and unchanged.

### The mixer's memory cost, and why previews are mono

Opening a 4-minute track in a 390×844 mobile-Chrome context, measured as
resident memory across every Chromium process, back when previews were stereo:

| | |
| --- | --- |
| Network to reach a playable mixer | 22.0 MB (4 × 5.76 MB MP3) |
| Time to playable, on localhost | 6.7 s |
| RSS attributable to the mixer | 511 MB |

Roughly 339 MB of that was the four decoded stems, and that part is not
implementation-specific: `AudioBuffer` holds 32-bit float per sample per
channel, so four *stereo* stems at the 44.1 kHz context rate cost
`4 × 240 × 44100 × 2 × 4` bytes on any engine. The remaining ~172 MB is
Chromium's own overhead, measured on desktop Linux, and may differ on a phone.

That cost is linear in stem count, so six stereo stems would not fit. The
arithmetic below is projection from the single measurement above, not further
measurement, at 0.353 MB per stem-second stereo and half that mono:

| At the 300 s cap | Decoded audio | With ~172 MB overhead |
| --- | --- | --- |
| 4 stems, stereo (previous behaviour) | 424 MB | ~596 MB |
| 6 stems, stereo | 635 MB | ~807 MB |
| **6 stems, mono (current)** | **318 MB** | **~490 MB** |
| 4 stems, mono (current) | 212 MB | ~384 MB |

`STEM_MAX_DURATION_S` had already dropped from 600 to 300 to keep the
four-stem stereo case near 600 MB. Six stereo stems would have blown through
that and forced the cap down again, to around 200 s — which cuts most finished
songs. Mono previews cost exactly half, which buys the two extra stems and
then some: a six-stem job now holds *less* decoded audio than a four-stem one
did before.

The trade is that monitoring is mono, so a hard-panned stem sounds centred
while you are setting faders. Nothing downloadable is affected — stems, the
zip and every rendered mix are summed server-side from the stereo lossless
files, which the preview encoder only reads. Preview bandwidth is unchanged
too: six mono stems at 128 kbps over 300 s measured **29.4 MB**, against
28.8 MB for the four stereo stems at 192 kbps they replace.

If a track near the cap still gets a tab killed, the fix remains changing the
mixer to stream from `<audio>` elements rather than decoding whole buffers —
mono buys headroom, it does not remove the ceiling.

### ARM, now measured in the field

Not by this session's own tooling — no stopwatch-precise, controlled benchmark
the way the x86_64 numbers above are — but by the same production deployment,
consistently, across three separate runs, on an Oracle Cloud Ampere A1
(`standard-a1-flex`, the box this project actually runs on): **roughly 3×
realtime** for `htdemucs` separation alone (decode and encoding are fast
`ffmpeg` steps on any hardware and don't move this number much). Two manual
`audio-separator` CLI runs on ~3 minute tracks took ~9 minutes each. One
app-driven run logged precisely by the job's own progress display — not an
estimate — was still 9:06 into a 3:15 track partway through its second
`htdemucs` pass, putting the final time at noticeably more than 2.8×. That's
roughly 1.7× slower than the 1.75× realtime measured on the 4-core x86_64
container above, for the same model.

### Six stems costs less than four, measured

Both models were then run on the same box against the **same 167 s track**,
back to back, with elapsed time read from each job's own record rather than a
stopwatch:

| Model | Audio | Elapsed | Realtime |
| --- | --- | --- | --- |
| `htdemucs` (4 stems) | 167 s | 508 s | **3.04×** |
| `htdemucs_6s` (6 stems) | 167 s | 463 s | **2.77×** |
| `htdemucs_6s` (6 stems) | 331 s | 906 s | **2.74×** |

Six-stem separation is about **9% faster** than four-stem on this hardware, not
slower. That is the opposite of what was expected when the six-stem option was
added — the reasoning was that a model producing more sources must cost more —
and it is worth stating plainly that the expectation was wrong rather than
quietly correcting the number. Why the larger output set is cheaper here has
not been established and is not guessed at.

The two six-stem figures also agree across a 2× difference in track length
(2.77× and 2.74×), which makes this a throughput number rather than one skewed
by the fixed model-loading cost. It supersedes the earlier four-stem ~3×
estimate for planning: that estimate came from stopwatch timings of different
tracks, while the 3.04× above is the same model measured properly.

`STEM_SEPARATOR_TIMEOUT_S` stays at 3600. At 2.8× realtime the 300 s duration
cap implies roughly 14 minutes of separation, well inside it.

### WebKit, now exercised in the field

Chromium testing covers Chrome on Android, which shares its engine. It does
not cover Chrome on iOS: Apple's App Review Guideline 2.5.6 states *"Apps
that browse the web must use the appropriate WebKit framework and WebKit
JavaScript,"* with alternative-engine entitlements limited to the EU and
Japan, so iOS Chrome is Safari's engine wearing a different icon. No
automated WebKit run exists in this project — there is no Safari test
tooling available to it — but the app has been used directly on real iOS
hardware, which is a different and in some ways stronger check: it found two
real bugs neither reasoning nor a Chromium-only test suite would have caught
(both in the Mixer section above), and confirmed a third as a WebKit
platform limitation rather than an app bug. No Firefox run either way.

## Licence

MIT, see `LICENSE`. `audio-separator` and the `htdemucs` weights carry their own
licences; check them before doing anything commercial with the output.
