# CLAUDE.md

Context for whichever Claude session picks this project up next. Written at
the end of the session that built it, not inferred from the code afterward —
treat the reasoning here as more reliable than re-deriving it from scratch,
but re-verify anything you're about to act on rather than trusting a stale
summary of it. If something here conflicts with what you read in the repo or
observe on the box, the repo and the box win.

## What this is

A self-hosted stem separator and mixer, built for exactly two named people:
Reed (the operator, works from an iPhone via Termius into an Oracle Cloud ARM
box) and one friend, a recording artist/producer on Android (Galaxy S24
Ultra, Z Flip 6). Not a public product. Read `README.md` for the actual
architecture, API, and configuration — this file is deployment state,
history, and what's still open, none of which belongs in a README meant for
a stranger cloning the repo.

## Where it actually runs

- Oracle Cloud ARM instance (Ampere A1, `standard-a1-flex`), reachable only
  through Reed's Termius session — this Claude session has never had direct
  access to that box and still doesn't. Everything about it below is
  reconstructed from what Reed pasted back, not from having run anything
  there directly.
- Public URL: `https://stem.crosbynews.com`, Reed's own domain, already in
  his Cloudflare account.
- Two systemd services, both confirmed `enabled` and `active (running)`,
  independent of any terminal session:
  - `stem.service` — the app itself. Unit lives at `deploy/stem.service` in
    this repo; the deployed copy is `/etc/systemd/system/stem.service` on
    the box, paths hardcoded to `/home/ubuntu/stem`.
  - `cloudflared.service` — the tunnel. **Not managed by this repo at all.**
    Reed set it up himself, outside anything Claude walked him through; this
    session doesn't know its exact config (which tunnel, which credentials
    file, named vs. quick tunnel) beyond "it's a systemd service and it
    works." If it ever needs touching, ask Reed how it's configured before
    assuming — don't reconstruct it from the systemd unit alone if that unit
    isn't in this repo.
- The box's `~/stem` checkout tracks `main` on GitHub (`reloru/stem`). Started
  private, Reed made it public partway through this session (to sidestep a
  GitHub-auth problem on the box, not for any reason tied to the app itself)
  — check its current visibility before assuming either way if that ever
  matters. Deploys are `git pull` + `sudo systemctl restart
  stem`, done by Reed, by hand, each time. There is no CI and no auto-deploy
  — if you push a fix, Reed still has to pull and restart it himself, and
  won't necessarily do that the moment you merge. Don't assume a merged fix
  is live until he confirms it.

## Working agreement specific to this project

- **Reed is on a phone the entire time.** Every command handed to him has to
  be short, single-line, and copy-pasteable — no multi-step interactive
  sequences, no "run this, then when you see X type Y." If a fix needs more
  than one command, give them as separate copy-pasteable blocks, not a
  narrated sequence.
- **He forwards you exactly what he sees** — screenshots, pasted terminal
  output, sometimes cut off by Termius line-wrapping or (once) by this app's
  own error-truncation bug. Don't assume a paste is complete; a truncated
  traceback missing its actual exception line cost real time twice in this
  session before the truncation itself turned out to be the bug.
- **He is a real user of his own product**, not just relaying bug reports
  from the friend — several of this session's bugs (the PWA top bar, the
  cold-launch state loss, the Quick Look download issue) came from Reed
  using the installed PWA on his own iPhone. Don't assume "the friend's
  device" is the only non-dev environment that matters.
- **He interrupts often, sometimes mid-tool-call, sometimes by accident**
  (stray characters like a trailing `*` land as their own message). Distinct
  from a real course-correction — read the next message before assuming an
  interruption meant "stop, that was wrong."
- **This repo's own CLAUDE.md (this file) sits alongside the operator's
  global `~/.claude/CLAUDE.md`**, which governs the iPhone-only workflow,
  factual-discipline rules, and git/PR conventions in more general terms.
  This file is project-specific state; that one is standing operating
  instructions. Both apply; this one doesn't repeat what that one already
  covers well (e.g. "keep commands short" is a global rule, restated above
  only because it's load-bearing for how you'll actually talk to Reed).

## Everything that's landed, in order (9 PRs, all squash-merged to `main`)

1. **Initial build.** Python stdlib HTTP server (`http.server`, no
   framework), hand-rolled streaming multipart parser (`cgi` is gone in
   3.13, `email` buffers whole bodies), `audio-separator` (not Demucs
   directly — Demucs is archived and unmaintained) running `htdemucs` in a
   subprocess. Vanilla HTML/CSS/JS front end, no build step, CSP with no
   inline script/style. Export sums stems via `amix normalize=0` with a
   measured-peak constant attenuation instead of a limiter (a limiter's
   lookahead delay, measured at 219 samples, would misalign the export
   against its own stems).
2. **Fader-position persistence.** `localStorage`, keyed by job id, written
   as faders/mute/solo change. Built because mobile browsers discard
   backgrounded tabs under memory pressure and this is an expensive tab.
3. **PWA install support + duration cap lowered 600s → 300s.** Manifest,
   icons rasterized from the SVG mark, no service worker (verified against
   current Chrome installability criteria that one isn't required). The
   duration cap was lowered because the mixer decodes all four stems as
   `AudioBuffer`s client-side — measured at 511 MB resident for a 4-minute
   track in a 390×844 mobile-Chrome context — and the old 600s cap let the
   server accept uploads its own mobile mixer couldn't hold in memory.
4. **Fixed a real install bug:** `audio-separator` imports `audioread`
   directly but never declares it as a dependency; it was only ever present
   because `librosa` used to pull it in, and `librosa` 1.0.0 dropped it.
   Installed explicitly in `setup.sh` now. Found because Reed's fresh
   install on the actual ARM box failed where this session's own (older,
   cached) test venv hadn't.
5. **Fixed the Android file-picker bug:** `accept="audio/*"` on the upload
   `<input>` greyed out real MP3s in Android's document picker (provider-
   reported MIME type is unreliable). Reed found and fixed this live on the
   box with a `sed` one-liner before this session shipped the same fix
   properly.
6. **Fixed the truncated-error bug + added the instrumental export.** The
   error-scrubbing code sliced the *first* 1000 characters of a subprocess
   failure message; a Python traceback's only useful line is its *last*
   one, so every deep failure was showing a cut-off stack frame instead of
   the actual exception, everywhere — the browser, the API, and the on-disk
   job record alike. This is what made bug #7 (below) invisible for two
   full round-trips with Reed before it was fixed. Separately: an
   "Instrumental" export (vocals out, everything else at unity, ignoring
   live fader state) was added next to the regular mix export, reusing the
   same mixdown endpoint with fixed gains.
7. **Fixed the actual separation crash.** Once #6 stopped hiding the real
   error, it turned out to be `numba` (a `librosa` dependency) failing to
   write its JIT disk cache: `deploy/stem.service`'s own
   `ProtectHome=read-only` + `ReadWritePaths=only data/` locks out *both*
   places numba tries (in-tree next to the source file, and a
   `$HOME`-based fallback) simultaneously. A plain read-only filesystem
   makes numba degrade to running uncached; losing both at once makes it
   raise instead. This was **caused by the systemd hardening this same
   session wrote**. Fixed by pointing `NUMBA_CACHE_DIR` at a directory
   under `STEM_DATA_DIR` (already covered by `ReadWritePaths`), set via
   `Config.ensure_directories()` so the separator subprocess inherits it —
   no unit file change, no sandboxing weakened. Reproduced properly this
   time: an isolated mount namespace with both paths remounted read-only
   *together* and a cold numba cache, on a from-scratch Python 3.12 venv
   (matching the box) rather than the 3.11 venv every earlier test that
   session had used without noticing the version gap. Two earlier,
   narrower read-only reproductions (venv alone, `$HOME` alone) had already
   been tried and correctly ruled out — the gap was Python 3.11 vs. 3.12,
   not the theory.
8. **Fixed PWA cold-launch state loss + added a copy-link button.** An
   installed PWA's `start_url` is fixed by the manifest spec — every
   relaunch from the home-screen icon lands there, with none of the
   last-open-tab restoration a normal browser tab gets. `init()` now falls
   back to the most recently touched job from the same `localStorage` data
   item 2 above already wrote, silently, on a cold launch with nothing in
   the URL.
   Added an icon-only "copy link" button in the top bar — reusing a text
   button crowded the track name down to two visible characters on a real
   390px phone, measured before deciding against it. Also documented (not
   fixed — no code-side fix exists) that exporting from inside the
   installed iOS PWA can strand Safari on an unclosable Quick Look sheet; a
   confirmed WebKit limitation, not this app's bug, with the copy-link
   button as the practical workaround (open the same job in a normal Safari
   tab instead).
9. **Fixed the iOS notch/Dynamic Island bug.** `viewport-fit=cover` (needed
   so the installed app fills the screen instead of looking letterboxed)
   draws content under the status bar/Dynamic Island unless something
   explicitly reserves that space with `env(safe-area-inset-*)`. The top
   bar wasn't doing that — Reed's own report: invisible and untappable at
   rest, briefly exposed mid-drag by a pull-down overscroll, gone again on
   release. It was never actually appearing and disappearing; it was
   rendering behind the status bar the entire time, and the drag gesture
   was the only thing ever exposing it. Fixed with `env(safe-area-inset-*)`
   padding on `.topbar`. **Confirmed working by Reed on his real device**
   after this fix shipped — one of the only things in this whole project
   that got genuine on-hardware iOS confirmation rather than staying an
   documented-but-unverified caveat.

## Patterns worth knowing before you touch this repo again

**The squash-merge dance.** Every PR in this project has been squash-merged.
That means after PR N merges, the local branch (still holding PR N's
pre-squash commit) is *behind* `origin/main`'s squashed version of the same
change, and a plain `git push` gets rejected as non-fast-forward every
single time. The fix used throughout, every time: verify the old branch tip
is actually fully merged (`git diff --stat` against `origin/main` is empty,
`git cherry origin/main <branch>` shows nothing needing to be applied), then
`git checkout -B <branch> origin/main`, restore any uncommitted work via
`git stash`/`git stash pop` across that checkout, and only then commit and
push. Force-with-lease is safe here specifically *because* the verification
step confirms nothing but already-merged history is being discarded — don't
skip the verification and force anyway.

**Reproduce before fixing, every time, and be honest when a reproduction
comes back negative.** Two systemd-hardening theories (#7 above) were tested
and correctly ruled out before the actual cause was found — that wasn't
wasted effort, it was the process working. The failure mode this session hit
once and shouldn't repeat: testing a theory using an environment that had
already been "warmed up" by earlier successful runs (a populated numba cache
from prior testing masked the bug the first two times it was checked). If a
fix depends on something being freshly initialized — a cache, a first-run
code path, a cold install — the test environment has to actually be cold,
not just theoretically supposed to be.

**Don't trust "same file, same box" without checking Python version.** The
gap between this session's own test venvs (3.11) and the box's actual
Python (3.12) was invisible for a while because everything else — same
OS, same architecture family in spirit (though the box is ARM and testing
happened on x86_64, which is its own gap, see below) — looked matched.
Check `python3 -V` on the actual target before trusting a local
reproduction to represent it.

**ARM was never available to this session.** Every reproduction, every
benchmark, every "verified" claim about performance in this codebase's
history came from an x86_64 container. The ARM numbers in the README's
"measured in the field" section are Reed's own timestamps and screenshots,
not this session's own measurement — real evidence, but a different kind
and a different confidence level than a controlled benchmark. If a future
task involves ARM-specific behavior, say plainly that it can't be verified
locally and needs Reed to run it, the same way this session had to.

**iOS/WebKit is real now, not a documented gap to wave at.** Early in this
session, iOS support was reasoned about and explicitly deprioritized on the
theory that "the friend is Android, so WebKit doesn't matter." That
reasoning was correct about the friend and wrong about the actual usage
pattern — Reed tests everything on his own iPhone first. Two shipped bugs
existed only on iOS and would not have been caught by the Chromium-only
Playwright suite this session built. Don't repeat the "only one platform
was named as the target, so the other doesn't need checking" mistake;
ask, or check, rather than assume.

## Testing infrastructure that already exists (don't rebuild it)

This session installed `cloudflared` and `librsvg2-bin` (for icon
rasterization) directly into its own sandbox — those aren't part of the
repo and won't persist to a fresh session, but installing them again is
fast (`apt-get install`) if needed. Playwright's Chromium was already
available at `/opt/pw-browsers/chromium-1194/chrome-linux/chrome` — check
whether a future session's sandbox still has this before trying to install
`playwright` proper (only `playwright-core` was installed via `npm i`,
deliberately, to reuse the pre-installed browser rather than downloading
one).

The pattern used throughout for local verification: a Python venv running
the real `audio-separator` install, a `srv.sh`-style helper script
(recreate it if gone — it was scratchpad-only, never committed) to
start/stop the local `stemapp` server against that venv, and small
Playwright scripts driving real Chromium against it at both desktop
(1440×900) and phone (390×844, and once 844×390 landscape) viewport sizes,
with the access key seeded via `context.addInitScript` writing to
`localStorage` before the page loads. Server-side claims (mix rendering,
clip protection, the numba fix) were verified by direct Python calls against
`stemapp`'s own modules, not just by exercising the HTTP API — e.g. the
instrumental export was checked by comparing its rendered output against an
independently-ffmpeg-summed reference file, bit-for-bit, not just by
checking that the request succeeded.

## Open items, honestly ranked

1. **iOS Quick Look download lockup** — not fixed, likely not fixable from
   this app's code. The workaround (copy-link → open in real Safari) is
   shipped and documented. If Reed reports this is actually blocking him
   regularly rather than a rare annoyance, the only real lever left is an
   undocumented Apple URL scheme (`x-safari-https://`) to force-escape the
   standalone PWA context — deliberately not built into this session's
   work because Apple can break an undocumented scheme without notice.
   Don't build it without saying that trade-off out loud first.
2. **`cloudflared.service` is unmanaged by this repo.** If it ever needs
   changing, this session doesn't have its config captured anywhere. Ask
   Reed for its current state before assuming anything about it.
3. **ARM separation throughput (~3× realtime) is the real planning number**
   now, not the x86_64 1.75×. If a future change touches anything
   perf-sensitive, use the ARM number, or get a fresh one — don't quietly
   fall back to the x86_64 figure because it's the one with a controlled
   benchmark behind it.
4. **The Chrome-on-Android "Install Stem" banner** was raised as a possible
   annoyance and explicitly *not* acted on — Reed's friend didn't complain
   about it, and Reed decided not to suppress it once the trade-off
   (Chrome's own install nudge working as intended) was laid out. Don't
   revisit this unprompted; if it comes up again, the mechanism
   (`beforeinstallprompt` + `preventDefault()`, documented since Chrome 76)
   is already known and doesn't need re-researching.
5. **No CI exists.** Every "verified" claim in this project's PR history was
   verified by this session running things directly, by hand, before
   pushing — there is no automated check that will catch a regression later.
   If that ever changes, update the PR-workflow guidance in this file too,
   since it currently assumes there's nothing to wait on.
