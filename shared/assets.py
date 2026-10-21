"""Pinned downloadable-asset registry - Phase 6.1.

Every optional heavy asset HERMES can offer (the Whisper speech models, the optional
NVIDIA GPU runtime) lives here as plain data behind list_assets() / gpu_runtime_asset().
Nothing here ever touches the network at import time - every URL, size, and hash below
was pulled from the Hugging Face and PyPI APIs on 2026-07-19 and hardcoded, the same
pinning discipline shared/video_utils.py uses for ffmpeg. If a source repo, commit, or
package version ever moves, a future release updates the constants below.

Leaf module: only shared.paths for install-dir resolution. Never imports modules/.
ffmpeg is NOT described here - modules/downloads.py's ffmpeg row calls
shared/video_utils.py directly, unchanged from the row it replaced in Home.
"""

import os

from shared import paths

_HF_URL = "https://huggingface.co/{repo}/resolve/{commit}/{name}"


def _hf_file(repo, commit, name, size, sha256=None):
    return {
        "name": name,
        "url": _HF_URL.format(repo=repo, commit=commit, name=name),
        "size": size,
        "sha256": sha256,
    }


def _whisper_asset(asset_id, name, blurb, recommended, repo, commit, model_bytes,
                    model_sha256, vocab_name, vocab_bytes, config_bytes, tokenizer_bytes,
                    preproc_bytes, details):
    files = [
        _hf_file(repo, commit, "model.bin", model_bytes, model_sha256),
        _hf_file(repo, commit, "config.json", config_bytes),
        _hf_file(repo, commit, "tokenizer.json", tokenizer_bytes),
        _hf_file(repo, commit, vocab_name, vocab_bytes),
    ]
    if preproc_bytes:
        files.append(_hf_file(repo, commit, "preprocessor_config.json", preproc_bytes))
    return {
        "id": asset_id,
        "group": "subtitles",
        "name": name,
        "blurb": blurb,
        "recommended": recommended,
        "files": files,
        "details": details,
    }


# Order = best accuracy first. recommended = large-v3-turbo.
WHISPER_MODELS = [
    _whisper_asset(
        "whisper-large-v3", "Large", "Best accuracy, slowest.", False,
        "Systran/faster-whisper-large-v3", "edaa852ec7e145841d8ffdb056a99866b5f0a478",
        3087284237,
        "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1",
        "vocabulary.json", 1068114, 2394, 2480617, 340,
        {"download": "~2.9 GB", "ram": "~3.5 GB",
         "speed_cpu": "Moderate (about 2x realtime)", "speed_gpu": "Very fast",
         "accuracy": "Best"},
    ),
    _whisper_asset(
        "whisper-large-v3-turbo", "Large Turbo",
        "Nearly Large's accuracy, much faster. Recommended.", True,
        "deepdml/faster-whisper-large-v3-turbo-ct2", "4df90f75321148c3a29a9e2351b7ddf8f5b115a8",
        1617884929,
        "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da",
        "vocabulary.json", 1068114, 2263, 2710337, 340,
        {"download": "~1.6 GB", "ram": "~2 GB",
         "speed_cpu": "Fast (about 3x realtime)", "speed_gpu": "Fastest of all",
         "accuracy": "Great (a hair under Large)"},
    ),
    _whisper_asset(
        "whisper-medium", "Medium", "Balanced speed and accuracy.", False,
        "Systran/faster-whisper-medium", "08e178d48790749d25932bbc082711ddcfdfbc4f",
        1527906378,
        "9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae",
        "vocabulary.txt", 459861, 2257, 2203239, None,
        {"download": "~1.5 GB", "ram": "~2 GB",
         "speed_cpu": "Fast (about 3x realtime)", "speed_gpu": "Very fast",
         "accuracy": "Good"},
    ),
    _whisper_asset(
        "whisper-small", "Small", "Fast, clean audio only.", False,
        "Systran/faster-whisper-small", "536b0662742c02347bc0e980a01041f333bce120",
        483546902,
        "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
        "vocabulary.txt", 459861, 2370, 2203239, None,
        {"download": "~0.47 GB", "ram": "~1 GB",
         "speed_cpu": "Very fast (about 8x realtime)", "speed_gpu": "Very fast",
         "accuracy": "Okay, clean audio only"},
    ),
]

# The optional GPU runtime (NVIDIA CUDA 12 DLLs faster-whisper/CTranslate2 need for GPU
# inference). Not installed by the faster-whisper wheels; sourced from the official
# nvidia-*-cu12 PyPI wheels and extracted here rather than added as an app dependency -
# that would bundle ~1.4 GB into every install. A wheel is a zip; the DLLs live under
# each package's bin/ folder and are extracted wholesale (cuDNN ships many, e.g.
# cudnn_ops64_9.dll / cudnn_cnn64_9.dll / cudnn_graph64_9.dll - all of them are needed,
# not just one).
GPU_RUNTIME = {
    "id": "cuda-runtime",
    "group": "gpu",
    "name": "GPU mode (CUDA)",
    "blurb": "NVIDIA-only. About 10x faster, and can be more accurate than CPU.",
    "files": [
        {"name": "nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl",
         "url": "https://files.pythonhosted.org/packages/20/e2/"
                "fc9a0e985249d873150276d5afb02e39a66817fedbf1a385724393e505ed/"
                "nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl",
         "size": 553162896,
         "sha256": "623f43027d40d44ceadf0043f002bd25cf353e8f13ce90b9a87057019f560661"},
        {"name": "nvidia_cudnn_cu12-9.24.0.43-py3-none-win_amd64.whl",
         "url": "https://files.pythonhosted.org/packages/29/28/"
                "2c9a2a97a8b3fedcf74a14f38fd5edfae12274380a829fdc6b16ce29be4c/"
                "nvidia_cudnn_cu12-9.24.0.43-py3-none-win_amd64.whl",
         "size": 737103728,
         "sha256": "cbd41a0ab084422c936dc9fb2fc89be5ea9a85bc421c6f23d0243bdfc945fbef"},
        {"name": "nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl",
         "url": "https://files.pythonhosted.org/packages/52/de/"
                "823919be3b9d0ccbf1f784035423c5f18f4267fb0123558d58b813c6ec86/"
                "nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl",
         "size": 76408187,
         "sha256": "72972ebdcf504d69462d3bcd67e7b81edd25d0fb85a2c46d3ea3517666636349"},
    ],
}


def list_assets():
    """Every Whisper model descriptor, best-accuracy first."""
    return list(WHISPER_MODELS)


def get_asset(asset_id):
    """A single model descriptor by id, or None."""
    return next((a for a in WHISPER_MODELS if a["id"] == asset_id), None)


def gpu_runtime_asset():
    return GPU_RUNTIME


def recommended_model_id():
    return next(a["id"] for a in WHISPER_MODELS if a["recommended"])


def installed_model_ids():
    """Model ids whose install dir already has every file at the expected size.
    Cheap presence/size check only (no hash re-verify) - the same trust level
    find_ffmpeg() gives its downloaded copy on every subsequent launch."""
    return [a["id"] for a in WHISPER_MODELS if is_model_installed(a)]


def is_model_installed(asset):
    d = model_dir(asset["id"])
    for f in asset["files"]:
        path = os.path.join(d, f["name"])
        if not os.path.exists(path):
            return False
        if os.path.getsize(path) != f["size"]:
            return False
    return True


def model_dir(asset_id):
    return os.path.join(paths.get_path("models"), asset_id)


def model_total_bytes(asset):
    return sum(f["size"] for f in asset["files"])


def gpu_total_bytes():
    return sum(f["size"] for f in GPU_RUNTIME["files"])


def gpu_installed():
    """True if at least one DLL was extracted into the cuda dir. Cheap presence check,
    mirrors is_model_installed's trust level."""
    d = paths.get_path("cuda")
    try:
        return any(name.lower().endswith(".dll") for name in os.listdir(d))
    except OSError:
        return False


# ── Plain-language copy (5.1-5.3), reused verbatim by Subtitle Studio later ──────────
RECOMMENDATION_TEXT = (
    "Not sure which to pick? Get Large Turbo. It is nearly as accurate as the best "
    "model but much faster, and it is the best choice for most clips. If your PC has "
    "an NVIDIA graphics card, also get GPU mode below - it makes every model "
    "dramatically faster. You can install more than one model and switch between them "
    "any time to see what works best for your audio."
)

ACCURACY_NOTE_TEXT = (
    "Small and Medium are very fast and fine on clean, single-voice audio, but they "
    "make more mistakes. The more background noise, music, or overlapping voices in "
    "your clip, the more the Large models are worth it."
)

GPU_BLURB_TEXT = (
    "GPU mode (NVIDIA only). Downloads the graphics-card runtime (about 1.4 GB) so "
    "transcription can run on your NVIDIA GPU instead of the processor - roughly "
    "10x faster, and the bigger the model the bigger the speedup. GPU mode can also "
    "catch a word CPU mode misses, since it runs at higher numeric precision by "
    "default - not guaranteed on every clip, but never worse. Needs an NVIDIA card "
    "(GeForce GTX 900-series/2014 or newer, RTX any generation, or a comparable "
    "Quadro). AMD and Intel graphics are not supported - those PCs use the processor, "
    "which still works fine. Only shown when an NVIDIA card is detected."
)

GPU_CPU_ONLY_NOTE_TEXT = (
    "GPU acceleration is available on NVIDIA cards only. Your PC will transcribe on "
    "the processor, which works fine."
)

MODEL_DETAIL_NOTES = {
    "speed_cpu": "How fast it transcribes without a graphics card. Even Large runs "
                 "faster than the clip's real length on a normal CPU; the smaller "
                 "models are much faster still.",
    "speed_gpu": "With the GPU boost installed and an NVIDIA card, every model "
                 "finishes in about a second or two on a short clip; Large Turbo is "
                 "the fastest.",
    "ram": "System memory used during transcription; it drops back after.",
    "accuracy_small_medium": "Expect more wrong words on noisy or multi-voice clips - "
                              "use a Large model there.",
}
