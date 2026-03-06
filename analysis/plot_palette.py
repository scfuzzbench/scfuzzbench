#!/usr/bin/env python3
from __future__ import annotations

from typing import Dict, Iterable, List

import matplotlib.pyplot as plt

FUZZER_BASE_COLORS = list(plt.get_cmap("tab20").colors)
FUZZER_COLOR_SEQUENCE = FUZZER_BASE_COLORS[::2] + FUZZER_BASE_COLORS[1::2]
NON_FUZZER_CMAP = plt.get_cmap("Purples")


def collect_fuzzer_names(*groups: Iterable[object]) -> List[str]:
    names: List[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw_name in group:
            name = str(raw_name).strip()
            if not name or name in seen:
                continue
            names.append(name)
            seen.add(name)
    return names


def non_fuzzer_shades(
    count: int, *, min_shade: float = 0.45, max_shade: float = 0.9
) -> List[tuple]:
    if count <= 0:
        return []
    if count == 1:
        return [NON_FUZZER_CMAP(max_shade)]
    return [
        NON_FUZZER_CMAP(
            min_shade + (max_shade - min_shade) * (idx / float(count - 1))
        )
        for idx in range(count)
    ]


def build_fuzzer_color_map(fuzzers: Iterable[object]) -> Dict[str, tuple]:
    ordered_fuzzers = sorted(
        {
            str(fuzzer).strip()
            for fuzzer in fuzzers
            if str(fuzzer).strip()
        }
    )
    if not ordered_fuzzers:
        return {}

    colors = list(FUZZER_COLOR_SEQUENCE[: len(ordered_fuzzers)])
    extra = len(ordered_fuzzers) - len(colors)
    if extra > 0:
        hsv = plt.get_cmap("hsv")
        for idx in range(extra):
            hue = (idx * 0.61803398875) % 1.0
            colors.append(hsv(hue))

    return {fuzzer: colors[idx] for idx, fuzzer in enumerate(ordered_fuzzers)}


def build_non_fuzzer_color_map(
    labels: Iterable[object], *, min_shade: float = 0.45, max_shade: float = 0.9
) -> Dict[str, tuple]:
    ordered_labels = sorted(
        {str(label).strip() for label in labels if str(label).strip()}
    )
    shades = non_fuzzer_shades(
        len(ordered_labels),
        min_shade=min_shade,
        max_shade=max_shade,
    )
    return {label: shades[idx] for idx, label in enumerate(ordered_labels)}
