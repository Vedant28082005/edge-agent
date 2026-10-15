"""Image and audio intake: magic-byte sniffing (never the declared type), size/pixel limits, audio to 16 kHz mono WAV.
Everything stays in RAM: no temp file (the ffmpeg path uses an anonymous memfd, or a pipe where memfd does not exist)."""
from __future__ import annotations

import asyncio
import io
import os
import struct
import wave
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from .errors import ApiError

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BODY = 6 * 1024 * 1024
MAX_AUDIO_BODY = 11 * 1024 * 1024
MAX_PARAMS_BYTES = 16 * 1024
MAX_SIDE = 4096
MAX_PIXELS = 16 * 1024 * 1024        # 16 megapixels (= 4096 x 4096)
MAX_AUDIO_MS = 60_000

Image.MAX_IMAGE_PIXELS = MAX_PIXELS  # Pillow's own bomb guard; the header is checked against our limits before any decode


@dataclass
class ImageInfo:
    mime: str
    width: int
    height: int


def sniff_image(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ApiError("unsupported_media_type")


_FMT = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


def inspect_image(data: bytes) -> ImageInfo:
    """Magic bytes, then a header-only parse for the dimensions. No pixel decode happens before the limits pass."""
    if not data:
        raise ApiError("bad_request")
    mime = sniff_image(data)
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            fmt = (im.format or "").upper()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise ApiError("bad_image")
    if _FMT[mime] != fmt or w < 1 or h < 1 or w > MAX_SIDE or h > MAX_SIDE or w * h > MAX_PIXELS:
        raise ApiError("bad_image")
    return ImageInfo(mime, w, h)


def decode_rgb(data: bytes) -> Image.Image:
    """Full decode (YOLO only), after inspect_image passed. Corrupt or truncated data -> bad_image."""
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        return im.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise ApiError("bad_image")


# ------------------------------------------------------------------------------------------------ audio
def sniff_audio(data: bytes) -> str:
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    if data[4:8] == b"ftyp":
        return "mp4"
    if len(data) > 2 and data[0] == 0xFF and (data[1] & 0xF6) == 0xF0:
        return "aac"
    raise ApiError("unsupported_media_type")


def wav_header(pcm_len: int) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + pcm_len) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
            + b"data" + struct.pack("<I", pcm_len))


_FFMPEG_FORMAT = {"wav": "wav", "webm": "matroska,webm", "mp4": "mov,mp4,m4a,3gp,3g2,mj2", "aac": "aac"}


async def to_wav16k(data: bytes, kind: str, ffmpeg: str = "ffmpeg") -> tuple[bytes, int]:
    """Returns (16 kHz mono 16-bit WAV, duration_ms). Plain PCM16 16k mono WAV passes through; everything else goes
    through ffmpeg with the demuxer forced by the sniffed type and only the file/pipe protocols allowed."""
    if not data:
        raise ApiError("bad_request")
    if kind == "wav":
        try:
            with wave.open(io.BytesIO(data)) as w:
                if (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getcomptype()) == (1, 2, 16000, "NONE"):
                    pcm = w.readframes(w.getnframes())
                    return _finish(pcm)
        except (wave.Error, EOFError):
            pass                              # not plain PCM wav: let ffmpeg decide (or fail -> bad_audio)
    return _finish(await _ffmpeg_pcm(data, kind, ffmpeg))


def _finish(pcm: bytes) -> tuple[bytes, int]:
    ms = len(pcm) // 2 * 1000 // 16000
    if ms == 0 or ms > MAX_AUDIO_MS:
        raise ApiError("bad_audio")
    return wav_header(len(pcm)) + pcm, ms


async def _ffmpeg_pcm(data: bytes, kind: str, ffmpeg: str) -> bytes:
    cap = (MAX_AUDIO_MS // 1000 + 1) * 32000
    args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "quiet", "-protocol_whitelist", "file,pipe",
            "-f", _FFMPEG_FORMAT[kind]]
    memfd = None
    pass_fds: tuple[int, ...] = ()
    stdin_data: bytes | None = None
    if hasattr(os, "memfd_create"):    # anonymous RAM file: seekable (MP4 with the moov atom last), never on disk
        memfd = os.memfd_create("a")
        os.write(memfd, data)
        args += ["-i", f"/proc/self/fd/{memfd}"]
        pass_fds = (memfd,)
    else:
        args += ["-i", "pipe:0"]
        stdin_data = data
    args += ["-vn", "-ac", "1", "-ar", "16000", "-t", str(MAX_AUDIO_MS // 1000 + 1), "-f", "s16le", "pipe:1"]
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, pass_fds=pass_fds)
        except OSError:
            raise ApiError("model_error")      # ffmpeg missing

        async def feed() -> None:
            if stdin_data is not None and proc.stdin:
                try:
                    proc.stdin.write(stdin_data)
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    proc.stdin.close()

        async def drain() -> bytes:
            buf = bytearray()
            assert proc.stdout
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    return bytes(buf)
                buf += chunk
                if len(buf) > cap:
                    raise ApiError("bad_audio")

        try:
            _, pcm = await asyncio.gather(feed(), drain())
            if await proc.wait() != 0:
                raise ApiError("bad_audio")
            return pcm
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
    finally:
        if memfd is not None:
            os.close(memfd)
