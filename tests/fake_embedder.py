"""Deterministic offline embedders for tests: no model, no network."""

import hashlib
import re

import numpy as np


class FakeEmbedder:
    """Hashed character-trigram bag of words: related spellings (stems, file paths, hyphenated
    names) land near each other, unrelated text does not. Fully deterministic."""

    is_cloud = False

    def __init__(self, name: str = "fake:trigram", dim: int = 256) -> None:
        self.name = name
        self.dim = dim

    def embed(self, texts):
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                padded = f"^{word}$"
                grams = [padded[j : j + 3] for j in range(max(len(padded) - 2, 1))]
                for g in grams:
                    h = int.from_bytes(hashlib.md5(g.encode()).digest()[:4], "little")
                    out[i, h % self.dim] += 1.0
        return out


class FakeCloudEmbedder(FakeEmbedder):
    """Pretends to be a hosted embedding API; records every text it is asked to embed."""

    is_cloud = True

    def __init__(self) -> None:
        super().__init__(name="fakecloud:trigram")
        self.seen: list[str] = []

    def embed(self, texts):
        self.seen.extend(texts)
        return super().embed(texts)
