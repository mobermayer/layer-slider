from typing import List


def layer_ranges_for_count(
    num_layers: int,
    avgrasters_enabled: bool,
    num_avgrasters: int = 1,
    distinct: bool = False,
    offset: int = 0,
) -> List[range]:
    if not avgrasters_enabled:
        return [range(i, i + 1) for i in range(num_layers)]

    num = num_avgrasters
    if not distinct:
        return [range(i, min(i + num, num_layers)) for i in range(num_layers - num + 1)]

    offset_base = min(num, num_layers - 1)
    if offset_base <= 0:
        if num_layers <= 0:
            return []
        return [range(0, num_layers)]
    off = offset % offset_base
    if off == 0:
        return [range(i, min(i + num, num_layers)) for i in range(0, num_layers, num)]
    return [range(max(i, 0), min(i + num, num_layers)) for i in range(-num + off, num_layers, num)]


def limit_slider_index(idx: int, layer_ranges: List[range]) -> int:
    if idx < 0:
        return 0
    if idx >= len(layer_ranges):
        return 0
    return idx


def layer_index_from_slider_index(
    slider_idx: int,
    layer_ranges: List[range],
    prefer_range_end: bool = False,
) -> int:
    if not layer_ranges:
        return 0
    slider_idx = min(max(0, slider_idx), len(layer_ranges) - 1)
    layer_range = layer_ranges[slider_idx]
    return max(layer_range) if prefer_range_end else min(layer_range)


def slider_index_for_layer_index(layer_index: int, layer_ranges: List[range]) -> int:
    if layer_index < 0:
        return 0
    for range_index, layer_range in enumerate(layer_ranges):
        if layer_index in layer_range:
            return range_index
    return limit_slider_index(layer_index, layer_ranges)
