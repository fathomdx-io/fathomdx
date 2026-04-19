"""CLIP embedding for deltas.

Lazy-loads the model on first use. Provides text and image embedding
into a shared 512D vector space.
"""

from __future__ import annotations

import open_clip
import torch

_model = None
_tokenizer = None
_preprocess = None
_device = None

MODEL_NAME = "ViT-B-32"
PRETRAINED = "laion2b_s34b_b79k"


def _load():
    global _model, _tokenizer, _preprocess, _device
    if _model is not None:
        return
    _device = "cuda" if torch.cuda.is_available() else "cpu"
    _model, _, _preprocess = open_clip.create_model_and_transforms(
        MODEL_NAME, pretrained=PRETRAINED, device=_device
    )
    _tokenizer = open_clip.get_tokenizer(MODEL_NAME)
    _model.eval()


def embed_text(text: str) -> list[float]:
    """Embed a single text string. Returns 512D vector."""
    _load()
    tokens = _tokenizer([text]).to(_device)
    with torch.no_grad():
        features = _model.encode_text(tokens)
        features /= features.norm(dim=-1, keepdim=True)
    return features[0].cpu().tolist()


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of text strings. Returns list of 512D vectors."""
    if not texts:
        return []
    _load()
    tokens = _tokenizer(texts).to(_device)
    with torch.no_grad():
        features = _model.encode_text(tokens)
        features /= features.norm(dim=-1, keepdim=True)
    return features.cpu().tolist()


def embed_image(image_path: str) -> list[float]:
    """Embed an image file. Returns 512D vector in same space as text."""
    from PIL import Image

    _load()
    image = _preprocess(Image.open(image_path)).unsqueeze(0).to(_device)
    with torch.no_grad():
        features = _model.encode_image(image)
        features /= features.norm(dim=-1, keepdim=True)
    return features[0].cpu().tolist()
