"""Single source for Whisper model names across both local backends.

``WHISPER_MODEL`` holds one alias (``large-v3``). Each backend needs its own
name for the same model: mlx-whisper wants a Hugging Face repo id, whisper.cpp
wants a ``ggml-<alias>.bin`` file. This module is a leaf (no package imports) so
config code can use it lazily without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

GGML_DOWNLOAD_COMMAND = "bash vendor/whisper.cpp/models/download-ggml-model.sh"


@dataclass(frozen=True)
class ModelSpec:
    alias: str
    mlx_repo_id: str
    ggml_filename: str


MODEL_MAP: dict[str, ModelSpec] = {
    spec.alias: spec
    for spec in (
        ModelSpec("tiny", "mlx-community/whisper-tiny-mlx", "ggml-tiny.bin"),
        ModelSpec("base", "mlx-community/whisper-base-mlx", "ggml-base.bin"),
        ModelSpec("medium", "mlx-community/whisper-medium-mlx", "ggml-medium.bin"),
        ModelSpec(
            "large-v3-turbo",
            "mlx-community/whisper-large-v3-turbo",
            "ggml-large-v3-turbo.bin",
        ),
        # q8_0 is the whisper.cpp 8-bit quantisation of large-v3-turbo (scripts/install.ps1 writes it);
        # mlx has no such file, so it uses the plain large-v3-turbo repo.
        ModelSpec(
            "large-v3-turbo-q8_0",
            "mlx-community/whisper-large-v3-turbo",
            "ggml-large-v3-turbo-q8_0.bin",
        ),
        ModelSpec("large-v3", "mlx-community/whisper-large-v3-mlx", "ggml-large-v3.bin"),
    )
}


def valid_aliases() -> list[str]:
    return sorted(MODEL_MAP)


def _unknown(name: str) -> ValueError:
    return ValueError(
        f"Unknown Whisper model {name!r}. Valid aliases: {', '.join(valid_aliases())}. "
        "For mlx-whisper you can also give a full Hugging Face repo id (owner/name)."
    )


def get_spec(name: str) -> ModelSpec:
    """Return the spec for an alias; raise ValueError listing the valid aliases."""
    key = (name or "").strip()
    spec = MODEL_MAP.get(key)
    if spec is None:
        raise _unknown(key)
    return spec


def resolve_mlx_repo(name: str) -> str:
    """Alias -> mlx repo id. A full ``owner/name`` repo id passes through unchanged."""
    key = (name or "").strip()
    if "/" in key:
        return key
    return get_spec(key).mlx_repo_id


def resolve_ggml_filename(name: str) -> str:
    """Alias -> whisper.cpp ggml file name. Full repo ids are not valid here."""
    key = (name or "").strip()
    if "/" in key:
        raise ValueError(
            f"Whisper model {key!r} is a repo id; whisper.cpp needs an alias "
            f"({', '.join(valid_aliases())}) or an explicit WHISPER_CPP_MODEL_PATH."
        )
    return get_spec(key).ggml_filename


def download_hint(name: str) -> str:
    """The command that fetches the ggml file for ``name`` (empty if not an alias)."""
    key = (name or "").strip()
    if key not in MODEL_MAP:
        return ""
    return f"{GGML_DOWNLOAD_COMMAND} {key}"
