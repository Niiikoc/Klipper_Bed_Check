"""Per-pixel statistical background model of the empty bed.

Stores sufficient statistics rather than mean/std directly, so batch reference
building and slow online adaptation are the same update with a different decay
factor. Per-pixel sigma is what makes this tolerant: glossy spots and
reflections naturally earn a wide sigma and stop generating alarms, while matte
stable regions stay strict.

The model is channel-generic — see detect.features() for what is fed in."""
from __future__ import annotations

import os

import numpy as np


class ModelError(RuntimeError):
    pass


class BackgroundModel:
    __slots__ = ("channels", "n", "s1", "s2", "anchor", "shape")

    def __init__(self, shape: tuple[int, int], channels: tuple[str, ...]):
        self.shape = tuple(shape)
        self.channels = tuple(channels)
        self.n = 0.0
        self.s1 = {c: np.zeros(shape, np.float64) for c in self.channels}
        self.s2 = {c: np.zeros(shape, np.float64) for c in self.channels}
        self.anchor: np.ndarray | None = None

    # -- statistics -----------------------------------------------------
    def add(self, feats: dict[str, np.ndarray], decay: float = 1.0) -> None:
        missing = set(self.channels) - set(feats)
        if missing:
            raise ModelError(f"missing feature channels: {sorted(missing)}")
        first = feats[self.channels[0]]
        if tuple(first.shape) != self.shape:
            raise ModelError(
                f"frame {first.shape} does not match model {self.shape}; "
                "rebuild the reference after changing bed size or px_per_mm"
            )
        if self.anchor is None:
            self.anchor = feats["int"].astype(np.float32).copy()

        self.n = self.n * decay + 1.0
        for c in self.channels:
            x = feats[c]
            self.s1[c] *= decay
            self.s2[c] *= decay
            self.s1[c] += x
            self.s2[c] += np.square(x, dtype=np.float64)

    def stats(self, channel: str) -> tuple[np.ndarray, np.ndarray]:
        if self.n <= 0:
            raise ModelError("reference model is empty")
        mean = self.s1[channel] / self.n
        var = np.maximum(self.s2[channel] / self.n - np.square(mean), 0.0)
        return mean.astype(np.float32), np.sqrt(var).astype(np.float32)

    @property
    def samples(self) -> float:
        return self.n

    # -- persistence ----------------------------------------------------
    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload: dict = {
            "n": self.n,
            "channels": np.array(self.channels),
            "anchor": self.anchor,
        }
        for c in self.channels:
            payload[f"s1_{c}"] = self.s1[c]
            payload[f"s2_{c}"] = self.s2[c]
        # savez_compressed appends .npz unless the name already ends in it.
        tmp = path + ".tmp.npz"
        np.savez_compressed(tmp, **payload)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "BackgroundModel":
        if not os.path.exists(path):
            raise ModelError("no reference captured yet")
        with np.load(path, allow_pickle=False) as z:
            try:
                channels = tuple(str(c) for c in z["channels"])
                shape = z[f"s1_{channels[0]}"].shape
            except KeyError as exc:
                raise ModelError(
                    "reference was built by an older version — rebuild it"
                ) from exc
            m = cls(shape, channels)
            m.n = float(z["n"])
            for c in channels:
                m.s1[c] = z[f"s1_{c}"]
                m.s2[c] = z[f"s2_{c}"]
            m.anchor = z["anchor"]
        if m.n <= 0:
            raise ModelError("reference model is empty")
        return m
