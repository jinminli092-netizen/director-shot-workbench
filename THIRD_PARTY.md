# Third-party components

This repository contains only its own Python/JavaScript/CSS source. It does
not bundle model weights, sample videos, fonts, or API keys.

## ffmpeg

Invoked as a separate executable for probing, scene detection, thumbnails and
clip extraction. ffmpeg is not distributed with this repository. Install it
from your package manager or from https://ffmpeg.org/ and comply with the
license of the build you install (LGPL or GPL depending on configuration).

## TransNetV2 (optional)

`seedance/video_shots.py` can call a separately installed local environment
under `.venv-audio`. No TransNetV2 code or weights are included. If you add
them, check the upstream repository's license and cite it in your deployment.

## Vision model

The app sends frames to whichever OpenAI-compatible endpoint the operator
configures. That provider's terms, data-retention policy and pricing apply.
This repository does not include any provider SDK.
