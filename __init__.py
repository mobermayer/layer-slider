# -*- coding: utf-8 -*-
"""
Layer Slider – QGIS Plugin
Navigate and compose ordered layers with a slider, keyboard shortcuts,
and dynamic raster compositing.

Copyright (C) 2024-2026 Maximilian Obermayer <software@mobermayer.at>

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 3 of the License, or
(at your option) any later version.
"""


# noinspection PyPep8Naming
def classFactory(iface):  # pylint: disable=invalid-name
    from .src.LayerSlider import LayerSlider
    return LayerSlider(iface)
