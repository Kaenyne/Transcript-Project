"""Speech-to-text backends.

Both run the same model — `whisper-large-v3-turbo` — so switching between them
changes cost and speed but not the character of the output. Local is the
default; Groq exists for weeks when the queue is too long to wait on CPU.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from . import audio

log = logging.getLogger(__name__)

# Groq rejects uploads over 25 MB; stay under it after compression.
GROQ_MAX_BYTES = 24 * 1024 * 1024
GROQ_MODEL = "whisper-large-v3-turbo"
LOCAL_MODEL = "large-v3-turbo"


class ASRError(Exception):
    pass


class LocalWhisper:
    """faster-whisper on CPU.

    The model is loaded once and reused — loading costs far more than
    transcribing a single episode, so a fresh model per episode would dominate
    the runtime of a batch.
    """

    name = "whisper-local"

    def __init__(self, model_size: str = LOCAL_MODEL, compute_type: str = "int8"):
        self.model_size = model_size
        self.compute_type = compute_type
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info(
                "loading %s (%s) — first run downloads ~1.5 GB",
                self.model_size,
                self.compute_type,
            )
            self._model = WhisperModel(
                self.model_size, device="cpu", compute_type=self.compute_type
            )
        return self._model

    def transcribe(self, path: Path) -> str:
        # vad_filter drops long silences and music beds, which otherwise
        # produce hallucinated repeats in Whisper-family models.
        segments, info = self.model.transcribe(
            str(path), beam_size=5, vad_filter=True, language=None
        )
        log.info("detected language %s (p=%.2f)", info.language, info.language_probability)
        return " ".join(s.text.strip() for s in segments if s.text.strip()).strip()

    @property
    def provenance(self) -> str:
        return f"whisper-local/{self.model_size}"


class GroqWhisper:
    """Groq-hosted whisper-large-v3-turbo. ~100x realtime, ~$0.04/hour."""

    name = "groq"

    def __init__(self, model: str = GROQ_MODEL, api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")

    def _client(self):
        if not self.api_key:
            raise ASRError(
                "GROQ_API_KEY is not set. Add it to .env or the environment, "
                "or run with --stt local."
            )
        try:
            from groq import Groq
        except ImportError as e:
            raise ASRError("groq is not installed. Run: pip install groq") from e
        return Groq(api_key=self.api_key)

    def transcribe(self, path: Path) -> str:
        client = self._client()
        work = path.parent / "prepared"

        # Compress first — a 128 kbps stereo episode is several times the cap,
        # while 16 kHz mono costs no accuracy for speech.
        prepared = audio.compress_for_upload(path, work / f"{path.stem}.16k.mp3")

        parts = (
            [prepared]
            if prepared.stat().st_size <= GROQ_MAX_BYTES
            else audio.split(prepared, work / path.stem)
        )
        if len(parts) > 1:
            log.info("%s exceeds the upload cap; sending %d parts", path.name, len(parts))

        out: list[str] = []
        for i, part in enumerate(parts, 1):
            if part.stat().st_size > GROQ_MAX_BYTES:
                raise ASRError(
                    f"{part.name} is still {part.stat().st_size / 1e6:.0f} MB after "
                    f"compression — lower audio.CHUNK_SECONDS."
                )
            log.info("groq: part %d/%d (%.1f MB)", i, len(parts), part.stat().st_size / 1e6)
            with part.open("rb") as fh:
                resp = client.audio.transcriptions.create(
                    file=(part.name, fh.read()),
                    model=self.model,
                    response_format="text",
                )
            out.append(resp if isinstance(resp, str) else getattr(resp, "text", ""))

        return " ".join(t.strip() for t in out if t and t.strip()).strip()

    @property
    def provenance(self) -> str:
        return f"groq/{self.model}"


def get_backend(name: str, *, model: str | None = None) -> LocalWhisper | GroqWhisper:
    if name == "local":
        return LocalWhisper(model_size=model or LOCAL_MODEL)
    if name == "groq":
        return GroqWhisper(model=model or GROQ_MODEL)
    raise ASRError(f"Unknown STT backend: {name!r}. Use 'local' or 'groq'.")
