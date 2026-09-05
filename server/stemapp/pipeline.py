"""External-process stages: probe, decode, separate, encode, mix, package.

Everything expensive happens in a subprocess rather than in-process. Two
reasons: `audio-separator` pulls in a multi-gigabyte tensor runtime that would
otherwise stay resident in the web server for the life of the process, and a
job that wedges can be killed by timeout without taking the server with it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .config import MIX_FORMATS, SEPARATOR_STEM_LABELS, STEM_NAMES, Config

# tqdm renders "  45%|####      | 45/100" and rewrites the line with \r.
_PERCENT_RE = re.compile(rb"(\d{1,3})%")

ProgressCallback = Callable[[float], None]


class StageError(RuntimeError):
    """A pipeline stage failed. The message is shown to the user verbatim."""


def _run(
    command: Sequence[str],
    *,
    timeout: int,
    on_progress: ProgressCallback | None = None,
    tail_lines: int = 25,
) -> None:
    """Run a command, streaming its output so progress can be reported.

    stdout and stderr are merged because the tools involved split their
    progress reporting across both. The last few lines are kept so a failure
    can say something more useful than the exit code.
    """
    try:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # Unbuffered: a BufferedReader's read() blocks until it has the
            # full request, which holds progress output back until roughly a
            # kilobyte has accumulated. Reads here return whatever the pipe
            # has, so a progress bar is seen as it is written.
            bufsize=0,
        )
    except OSError as exc:
        raise StageError(f"could not start {command[0]}: {exc}") from exc

    tail: list[str] = []
    assert process.stdout is not None
    try:
        for line in _iter_output_lines(process.stdout):
            text = line.decode("utf-8", "replace").strip()
            if text:
                tail.append(text)
                if len(tail) > tail_lines:
                    del tail[0]
            if on_progress is not None:
                match = _PERCENT_RE.search(line)
                if match:
                    value = min(100.0, float(match.group(1)))
                    on_progress(value)
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise StageError(
            f"{Path(command[0]).name} exceeded its {timeout}s time limit"
        ) from None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    if returncode != 0:
        detail = " | ".join(tail[-6:]) or "no output"
        raise StageError(
            f"{Path(command[0]).name} exited with code {returncode}: {detail}"
        )


def _iter_output_lines(stream) -> Iterable[bytes]:
    """Yield output split on both newline and carriage return.

    Progress bars only emit \\r, so reading with readline() would block until
    the bar finished and report nothing in between.
    """
    buffer = bytearray()
    while True:
        chunk = stream.read(1024)
        if not chunk:
            break
        buffer += chunk
        start = 0
        for index, byte in enumerate(buffer):
            if byte in (0x0A, 0x0D):
                yield bytes(buffer[start:index])
                start = index + 1
        del buffer[:start]
    if buffer:
        yield bytes(buffer)


# -- stages ---------------------------------------------------------------


def probe_audio(path: Path, cfg: Config) -> dict:
    """Return duration, sample rate and channel count for the first stream."""
    command = [
        cfg.ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-select_streams",
        "a:0",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StageError(f"ffprobe could not read the file: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise StageError(f"this file is not readable as audio: {detail}")

    try:
        payload = json.loads(completed.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise StageError("ffprobe returned unparseable output") from exc

    streams = payload.get("streams") or []
    if not streams:
        raise StageError("no audio stream found in this file")
    stream = streams[0]

    duration = stream.get("duration") or payload.get("format", {}).get("duration")
    try:
        duration_seconds = float(duration)
    except (TypeError, ValueError):
        raise StageError("could not determine the duration of this file") from None

    return {
        "duration_seconds": duration_seconds,
        "sample_rate": int(stream.get("sample_rate") or 0) or None,
        "channels": int(stream.get("channels") or 0) or None,
        "codec": stream.get("codec_name") or "unknown",
    }


def decode_to_wav(source: Path, destination: Path, cfg: Config) -> None:
    """Normalise any input to 44.1 kHz stereo 16-bit PCM.

    Separation quality is defined at 44.1 kHz stereo, and pinning the format
    here means every later stage sees identical input regardless of what was
    uploaded.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            cfg.ffmpeg_bin,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "a:0",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        timeout=900,
    )
    if not destination.is_file() or destination.stat().st_size == 0:
        raise StageError("decoding produced an empty file")


def separate(
    source: Path,
    output_dir: Path,
    cfg: Config,
    on_progress: ProgressCallback | None = None,
) -> None:
    """Run the separation model, writing one WAV per stem into output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        cfg.separator_bin,
        str(source),
        "--model_filename",
        cfg.model_filename,
        "--model_file_dir",
        str(cfg.model_dir),
        "--output_dir",
        str(output_dir),
        "--output_format",
        "WAV",
        "--custom_output_names",
        json.dumps(SEPARATOR_STEM_LABELS),
        "--log_level",
        "info",
    ]
    _run(
        command,
        timeout=cfg.separator_timeout_seconds,
        on_progress=on_progress,
    )

    missing = [
        name for name in STEM_NAMES if not (output_dir / f"{name}.wav").is_file()
    ]
    if missing:
        produced = sorted(p.name for p in output_dir.glob("*"))
        raise StageError(
            "separation did not produce "
            f"{', '.join(missing)}; it wrote {produced or ['nothing']}"
        )


def normalise_stems(stem_dir: Path, cfg: Config) -> None:
    """Re-encode each stem to 16-bit 44.1 kHz stereo.

    The separator's WAV output can be 32-bit float depending on the model
    backend. Pinning to 16-bit halves the download size and is the format a
    DAW expects for delivered stems.
    """
    for name in STEM_NAMES:
        original = stem_dir / f"{name}.wav"
        converted = stem_dir / f"{name}.norm.wav"
        _run(
            [
                cfg.ffmpeg_bin,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(original),
                "-ac",
                "2",
                "-ar",
                "44100",
                "-c:a",
                "pcm_s16le",
                str(converted),
            ],
            timeout=600,
        )
        converted.replace(original)


def encode_previews(stem_dir: Path, preview_dir: Path, cfg: Config) -> None:
    """Produce the lossy copies the browser mixer streams.

    Four lossless stems of a four-minute track are roughly 170 MB; MP3 is the
    only lossy format `decodeAudioData` handles on every current browser
    including iOS Safari, so previews are MP3 regardless of what else is
    available.
    """
    preview_dir.mkdir(parents=True, exist_ok=True)
    for name in STEM_NAMES:
        _run(
            [
                cfg.ffmpeg_bin,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(stem_dir / f"{name}.wav"),
                "-c:a",
                "libmp3lame",
                "-b:a",
                cfg.preview_bitrate,
                str(preview_dir / f"{name}.mp3"),
            ],
            timeout=600,
        )


def _mix_graph(stem_dir: Path, gains: dict[str, float]) -> tuple[list[str], list[str]]:
    """Build the ffmpeg inputs and the per-stem gain + sum filter chain.

    `amix` with normalize=0 sums its inputs rather than dividing by the input
    count. That is what recombining stems requires: with every fader at unity
    the sum has to reproduce the original mix, not a quarter of it.
    """
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    for index, name in enumerate(STEM_NAMES):
        inputs.extend(["-i", str(stem_dir / f"{name}.wav")])
        gain = max(0.0, min(4.0, float(gains.get(name, 1.0))))
        label = f"g{index}"
        filters.append(f"[{index}:a]volume={gain:.6f}[{label}]")
        labels.append(f"[{label}]")
    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=longest:normalize=0[summed]"
    )
    return inputs, filters


def measure_mix_peak(
    stem_dir: Path, gains: dict[str, float], cfg: Config
) -> float:
    """Peak level of the summed mix in dBFS, measured before any clipping.

    The sum is forced to float before `astats` sees it, so a peak above full
    scale is reported as a positive number instead of being clamped by an
    integer sample format.
    """
    inputs, filters = _mix_graph(stem_dir, gains)
    filters.append(
        "[summed]aformat=sample_fmts=fltp,"
        "astats=measure_perchannel=none:measure_overall=Peak_level[out]"
    )
    command = [cfg.ffmpeg_bin, "-nostdin", "-v", "info", "-y"]
    command.extend(inputs)
    command.extend(["-filter_complex", ";".join(filters), "-map", "[out]", "-f", "null", "-"])

    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=900, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StageError(f"could not measure the mix level: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[-300:]
        raise StageError(f"could not measure the mix level: {detail}")

    output = completed.stderr.decode("utf-8", "replace")
    match = re.search(r"Peak level dB:\s*(-?inf|-?\d+(?:\.\d+)?)", output)
    if not match:
        raise StageError("ffmpeg did not report a peak level for the mix")
    value = match.group(1)
    return float("-inf") if value.endswith("inf") else float(value)


def render_mix(
    stem_dir: Path,
    gains: dict[str, float],
    destination: Path,
    cfg: Config,
    output_format: str,
    *,
    ceiling_db: float = -0.3,
) -> dict[str, float]:
    """Sum the lossless stems at the supplied gains into one file.

    Clipping is prevented with a single constant attenuation derived from the
    measured peak, not with a limiter. A limiter would apply gain reduction
    that varies over time -- changing the dynamics the user balanced -- and
    ffmpeg's `alimiter` additionally delays its output by the lookahead window
    (measured here at 219 samples / 5.0 ms at the default attack), which would
    put every exported mix out of alignment with the stems it came from.

    Returns the measured peak and the attenuation applied, both in dB, so the
    caller can tell the user when a boosted balance was pulled back.
    """
    if output_format not in MIX_FORMATS:
        raise StageError(f"unsupported mix format: {output_format!r}")
    _, codec_args = MIX_FORMATS[output_format]
    destination.parent.mkdir(parents=True, exist_ok=True)

    peak_db = measure_mix_peak(stem_dir, gains, cfg)
    attenuation_db = min(0.0, ceiling_db - peak_db) if peak_db > ceiling_db else 0.0
    scale = 10.0 ** (attenuation_db / 20.0)

    inputs, filters = _mix_graph(stem_dir, gains)
    filters.append(f"[summed]volume={scale:.8f}[out]")

    # Two browsers asking for the same balance at the same time would have
    # ffmpeg writing one path twice; each render gets its own file and the
    # rename at the end is atomic.
    staging = destination.with_name(
        f"{destination.stem}.{os.getpid()}.{threading.get_ident()}{destination.suffix}"
    )
    command = [cfg.ffmpeg_bin, "-nostdin", "-v", "error", "-y"]
    command.extend(inputs)
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-ar",
            "44100",
            "-ac",
            "2",
        ]
    )
    command.extend(codec_args)
    command.append(str(staging))
    try:
        _run(command, timeout=900)
        if not staging.is_file() or staging.stat().st_size == 0:
            raise StageError("the mixdown came out empty")
        staging.replace(destination)
    finally:
        staging.unlink(missing_ok=True)

    return {
        "peak_db": peak_db if peak_db != float("-inf") else -120.0,
        "attenuation_db": round(attenuation_db, 2),
    }


def build_stem_zip(stem_dir: Path, destination: Path, base_name: str) -> None:
    """Package all four stems, named after the original upload."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(
        f"{destination.stem}.{os.getpid()}.{threading.get_ident()}.zip"
    )
    try:
        with zipfile.ZipFile(staging, "w", zipfile.ZIP_STORED) as archive:
            for name in STEM_NAMES:
                source = stem_dir / f"{name}.wav"
                if source.is_file():
                    archive.write(source, f"{base_name}/{name}.wav")
        staging.replace(destination)
    finally:
        staging.unlink(missing_ok=True)


_JOB_PATH_RE = re.compile(
    r"(?:jobs/)?[A-Za-z0-9_-]{16,64}/(input|source|stems|preview|mixes)"
)
_SITE_PACKAGES_RE = re.compile(r'[^\s"]*/site-packages/')
_MESSAGE_LIMIT = 1000


def scrub(message: str, cfg: Config) -> str:
    """Strip server paths out of a message before the client sees it.

    Stage failures quote the failing tool's own stderr, which names the file it
    was handed -- an absolute path inside the data directory -- and, for a
    Python traceback, the full path to every installed package under the
    virtualenv. Neither is of use to the person who uploaded the track, both
    leak the server's layout and username, and both eat into the truncation
    budget below for no diagnostic value.

    A Python traceback's only truly load-bearing line is its last one -- the
    exception type and message -- with everything above it just showing how
    execution got there. So truncation here keeps the *end* of the message,
    not the start: a front truncation would, and for months of this project
    silently did, cut every deep failure off partway through the second stack
    frame, before the actual error was ever visible anywhere it was looked at
    -- the browser, the API response, and the job record on disk alike, since
    all three serve this same scrubbed string.
    """
    for path in (str(cfg.data_dir), str(cfg.model_dir)):
        message = message.replace(path + "/", "").replace(path, "")
    message = _SITE_PACKAGES_RE.sub("", message)
    message = _JOB_PATH_RE.sub(r"\1", message)
    message = " ".join(message.split())
    if len(message) <= _MESSAGE_LIMIT:
        return message
    return "…" + message[-_MESSAGE_LIMIT:]


def check_tools(cfg: Config) -> list[str]:
    """Verify the external tools exist and answer a version query."""
    problems: list[str] = []
    for label, binary, args in (
        ("ffmpeg", cfg.ffmpeg_bin, ["-version"]),
        ("ffprobe", cfg.ffprobe_bin, ["-version"]),
        ("audio-separator", cfg.separator_bin, ["--version"]),
    ):
        if shutil.which(binary) is None and not Path(binary).is_file():
            problems.append(f"{label}: not found (looked for {binary!r})")
            continue
        try:
            completed = subprocess.run(
                [binary, *args], capture_output=True, timeout=120, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            problems.append(f"{label}: could not be executed ({exc})")
            continue
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip()[:200]
            problems.append(f"{label}: exited {completed.returncode} ({detail})")
    return problems
