import json
from typing import Any, Optional

from qgis.PyQt.QtGui import QColor
from qgis.core import (
    Qgis,
    QgsBilinearRasterResampler,
    QgsContrastEnhancement,
    QgsCoordinateTransform,
    QgsCubicRasterResampler,
    QgsHueSaturationFilter,
    QgsMapLayerStyle,
    QgsProject,
    QgsRasterBandStats,
    QgsRasterLayer,
    QgsRasterMinMaxOrigin,
    QgsRasterResampleFilter,
    QgsRasterTransparency,
    QgsSingleBandGrayRenderer,
)


class DynamicLayerStyle:
    GROUP_STATE_PROPERTY = "layerslider/dynamic_style_state_v1"
    PROVIDER_RESAMPLING_METHOD_VALUES = {
        "nearest": 0,
        "bilinear": 1,
        "cubic": 2,
        "cubicspline": 3,
        "lanczos": 4,
        "average": 5,
        "mode": 6,
        "gauss": 7,
    }
    TRACKED_PROPERTIES = (
        "opacity",
        "renderer_opacity",
        "min_max",
        "gray_gradient",
        "transparency",
        "brightness",
        "contrast",
        "gamma",
        "brightness_on",
        "hue_saturation",
        "resampling",
        "scale_visibility",
        "blend_mode",
    )

    @staticmethod
    def _load_state(group) -> dict:
        try:
            raw = group.customProperty(DynamicLayerStyle.GROUP_STATE_PROPERTY, "")
            state = json.loads(str(raw)) if raw else {}
            if isinstance(state, dict):
                state.setdefault("baseline", {})
                state.setdefault("overrides", {})
                return state
        except Exception:
            pass
        return {"version": 1, "baseline": {}, "overrides": {}, "style_xml": ""}

    @staticmethod
    def _save_state(group, state: dict):
        try:
            state["version"] = 1
            encoded = json.dumps(state, separators=(",", ":"), sort_keys=True)
            existing = str(group.customProperty(DynamicLayerStyle.GROUP_STATE_PROPERTY, "") or "")
            if existing != encoded:
                group.setCustomProperty(DynamicLayerStyle.GROUP_STATE_PROPERTY, encoded)
        except Exception:
            pass

    @staticmethod
    def _same_value(left: Any, right: Any) -> bool:
        try:
            return json.dumps(left, sort_keys=True, separators=(",", ":")) == json.dumps(
                right, sort_keys=True, separators=(",", ":")
            )
        except Exception:
            return left == right

    @staticmethod
    def _enum_string(value, formatter, fallback: str = "") -> str:
        try:
            return str(formatter(value))
        except Exception:
            return str(value) if value is not None else fallback

    @staticmethod
    def _get_source_enhancement(renderer):
        if isinstance(renderer, QgsSingleBandGrayRenderer):
            return renderer.contrastEnhancement()
        try:
            return renderer.redContrastEnhancement()
        except Exception:
            return None

    @staticmethod
    def _capture_range(layer: QgsRasterLayer) -> Optional[dict]:
        renderer = layer.renderer()
        if renderer is None:
            return None
        enhancement = DynamicLayerStyle._get_source_enhancement(renderer)
        if enhancement is None:
            return None
        try:
            origin = renderer.minMaxOrigin()
            origin_data = {
                "limits": DynamicLayerStyle._enum_string(
                    origin.limits(), QgsRasterMinMaxOrigin.limitsString
                ),
                "extent": DynamicLayerStyle._enum_string(
                    origin.extent(), QgsRasterMinMaxOrigin.extentString
                ),
                "accuracy": DynamicLayerStyle._enum_string(
                    origin.statAccuracy(), QgsRasterMinMaxOrigin.statAccuracyString
                ),
                "cumulative_cut_lower": float(origin.cumulativeCutLower()),
                "cumulative_cut_upper": float(origin.cumulativeCutUpper()),
                "stddev_factor": float(origin.stdDevFactor()),
            }
        except Exception:
            origin_data = {}
        try:
            algorithm = QgsContrastEnhancement.contrastEnhancementAlgorithmString(
                enhancement.contrastEnhancementAlgorithm()
            )
        except Exception:
            algorithm = str(enhancement.contrastEnhancementAlgorithm())
        return {
            "algorithm": algorithm,
            "minimum": float(enhancement.minimumValue()),
            "maximum": float(enhancement.maximumValue()),
            "origin": origin_data,
            "manual": int(renderer.minMaxOrigin().limits()) == 0,
            "gray_source": isinstance(renderer, QgsSingleBandGrayRenderer),
        }

    @staticmethod
    def _capture_transparency(layer: QgsRasterLayer) -> Optional[list]:
        renderer = layer.renderer()
        if renderer is None:
            return None
        transparency = renderer.rasterTransparency()
        if transparency is None:
            return []
        try:
            return [
                [float(pixel.min), float(pixel.max), float(pixel.percentTransparent)]
                for pixel in transparency.transparentSingleValuePixelList()
            ]
        except Exception:
            return []

    @staticmethod
    def _capture_gray_gradient(layer: QgsRasterLayer) -> Optional[int]:
        renderer = layer.renderer()
        if not isinstance(renderer, QgsSingleBandGrayRenderer):
            return None
        try:
            return int(renderer.gradient())
        except Exception:
            return None

    @staticmethod
    def _capture_resampling(layer: QgsRasterLayer) -> dict:
        pipe = layer.pipe()
        filter_ = pipe.resampleFilter()
        provider = layer.dataProvider()
        stage = None
        try:
            stage = pipe.resamplingStage()
        except Exception:
            pass

        values = {
            "on": bool(filter_.on()) if filter_ is not None else True,
            "max_oversampling": float(filter_.maxOversampling()) if filter_ is not None else 2.0,
            "zoomed_in": "nearest",
            "zoomed_out": "nearest",
        }

        def _resampler_type(resampler):
            if resampler is None:
                return "nearest"
            try:
                return str(resampler.type()).lower()
            except Exception:
                return resampler.__class__.__name__.lower()

        if filter_ is not None:
            values["zoomed_in"] = _resampler_type(filter_.zoomedInResampler())
            values["zoomed_out"] = _resampler_type(filter_.zoomedOutResampler())

        if DynamicLayerStyle._is_provider_resampling_stage(stage) and provider is not None:
            try:
                values["zoomed_in"] = DynamicLayerStyle._provider_resampling_method_name(
                    provider.zoomedInResamplingMethod()
                )
            except Exception:
                pass
            try:
                values["zoomed_out"] = DynamicLayerStyle._provider_resampling_method_name(
                    provider.zoomedOutResamplingMethod()
                )
            except Exception:
                pass
            try:
                values["max_oversampling"] = float(provider.maxOversampling())
            except Exception:
                pass

        if stage is not None:
            try:
                values["stage"] = int(stage)
            except Exception:
                pass
        return values

    @staticmethod
    def _is_provider_resampling_stage(stage) -> bool:
        if stage is None:
            return False
        try:
            return stage == Qgis.RasterResamplingStage.Provider
        except Exception:
            pass
        try:
            return int(stage) == 1
        except Exception:
            return str(stage).lower().rsplit(".", 1)[-1] == "provider"

    @staticmethod
    def _provider_resampling_method_name(method) -> str:
        try:
            method_value = int(method)
            return next(
                name
                for name, value in DynamicLayerStyle.PROVIDER_RESAMPLING_METHOD_VALUES.items()
                if value == method_value
            )
        except Exception:
            return "nearest"

    @staticmethod
    def _capture_hue_saturation(layer: QgsRasterLayer) -> dict:
        filter_ = layer.pipe().hueSaturationFilter()
        if filter_ is None:
            return {}
        color = filter_.colorizeColor()
        return {
            "on": bool(filter_.on()),
            "saturation": int(filter_.saturation()),
            "grayscale_mode": int(filter_.grayscaleMode()),
            "colorize_on": bool(filter_.colorizeOn()),
            "colorize_strength": int(filter_.colorizeStrength()),
            "colorize_rgba": list(color.getRgb()),
            "invert_colors": bool(filter_.invertColors()),
        }

    @staticmethod
    def _capture_values(layer: QgsRasterLayer) -> dict:
        values = {}
        try:
            values["opacity"] = float(layer.opacity())
        except Exception:
            pass
        renderer = layer.renderer()
        if renderer is not None:
            try:
                values["renderer_opacity"] = float(renderer.opacity())
            except Exception:
                pass
        min_max = DynamicLayerStyle._capture_range(layer)
        if min_max is not None:
            values["min_max"] = min_max
        gray_gradient = DynamicLayerStyle._capture_gray_gradient(layer)
        if gray_gradient is not None:
            values["gray_gradient"] = gray_gradient
        transparency = DynamicLayerStyle._capture_transparency(layer)
        if transparency is not None:
            values["transparency"] = transparency

        brightness = layer.pipe().brightnessFilter()
        if brightness is not None:
            values.update({
                "brightness": int(brightness.brightness()),
                "contrast": int(brightness.contrast()),
                "gamma": float(brightness.gamma()),
                "brightness_on": bool(brightness.on()),
            })
        hue = DynamicLayerStyle._capture_hue_saturation(layer)
        if hue:
            values["hue_saturation"] = hue
        values["resampling"] = DynamicLayerStyle._capture_resampling(layer)
        values["scale_visibility"] = {
            "enabled": bool(layer.hasScaleBasedVisibility()),
            "minimum": float(layer.minimumScale()),
            "maximum": float(layer.maximumScale()),
        }
        try:
            values["blend_mode"] = int(layer.blendMode())
        except Exception:
            pass
        return values

    @staticmethod
    def capture_manual_changes(group, layer: QgsRasterLayer, save_snapshot: bool = True) -> dict:
        state = DynamicLayerStyle._load_state(group)
        baseline = state.get("baseline", {})
        overrides = state.get("overrides", {})
        current = DynamicLayerStyle._capture_values(layer)
        for key in DynamicLayerStyle.TRACKED_PROPERTIES:
            if key not in current or key not in baseline:
                continue
            if DynamicLayerStyle._same_value(current[key], baseline[key]):
                overrides.pop(key, None)
            else:
                overrides[key] = current[key]
        state["overrides"] = overrides
        if save_snapshot:
            try:
                style = QgsMapLayerStyle()
                style.readFromLayer(layer)
                state["style_xml"] = str(style.xmlData())
            except Exception:
                pass
        DynamicLayerStyle._save_state(group, state)
        return state

    @staticmethod
    def clear_overrides(group) -> dict:
        state = DynamicLayerStyle._load_state(group)
        state["overrides"] = {}
        DynamicLayerStyle._save_state(group, state)
        return state

    @staticmethod
    def _copy_origin(origin_data: dict) -> QgsRasterMinMaxOrigin:
        origin = QgsRasterMinMaxOrigin()
        try:
            origin.setLimits(QgsRasterMinMaxOrigin.limitsFromString(origin_data.get("limits", "MinMax")))
            origin.setExtent(QgsRasterMinMaxOrigin.extentFromString(origin_data.get("extent", "WholeRaster")))
            origin.setStatAccuracy(
                QgsRasterMinMaxOrigin.statAccuracyFromString(origin_data.get("accuracy", "Estimated"))
            )
            origin.setCumulativeCutLower(float(origin_data.get("cumulative_cut_lower", 0.02)))
            origin.setCumulativeCutUpper(float(origin_data.get("cumulative_cut_upper", 0.98)))
            origin.setStdDevFactor(float(origin_data.get("stddev_factor", 2.0)))
        except Exception:
            pass
        return origin

    @staticmethod
    def _range_for_composed_layer(
        layer: QgsRasterLayer,
        range_data: dict,
        canvas_extent=None,
        canvas_crs=None,
    ) -> tuple[float, float]:
        if range_data.get("manual"):
            return float(range_data["minimum"]), float(range_data["maximum"])

        origin = range_data.get("origin") or {}
        limits = str(origin.get("limits", "MinMax")).lower()
        provider = layer.dataProvider()
        if provider is None:
            return float(range_data["minimum"]), float(range_data["maximum"])

        extent_kind = str(origin.get("extent", "WholeRaster")).lower()
        if "currentcanvas" in extent_kind:
            # CurrentCanvas keeps the range captured when the source style was read.
            return float(range_data["minimum"]), float(range_data["maximum"])
        if "whole" in extent_kind or "fixedcanvas" in extent_kind:
            extent = layer.extent()
        elif "updatedcanvas" in extent_kind:
            if canvas_extent is None:
                extent = layer.extent()
            elif canvas_crs is None:
                return float(range_data["minimum"]), float(range_data["maximum"])
            elif canvas_crs == layer.crs():
                extent = canvas_extent
            else:
                try:
                    transform = QgsCoordinateTransform(
                        canvas_crs, layer.crs(), QgsProject.instance()
                    )
                    extent = transform.transformBoundingBox(canvas_extent)
                except Exception:
                    return float(range_data["minimum"]), float(range_data["maximum"])
        else:
            extent = layer.extent()
        accuracy = str(origin.get("accuracy", "Estimated")).lower()
        sample_size = 0 if "exact" in accuracy else int(QgsRasterLayer.SAMPLE_SIZE)

        try:
            stats = provider.bandStatistics(1, QgsRasterBandStats.All, extent, sample_size)
            minimum = float(stats.minimumValue)
            maximum = float(stats.maximumValue)
            if "stddev" in limits:
                factor = float(origin.get("stddev_factor", 2.0))
                return float(stats.mean - factor * stats.stdDev), float(stats.mean + factor * stats.stdDev)
            if "cumulative" in limits:
                low, high = provider.cumulativeCut(
                    1,
                    float(origin.get("cumulative_cut_lower", 0.02)),
                    float(origin.get("cumulative_cut_upper", 0.98)),
                    extent,
                    sample_size,
                )
                return float(low), float(high)
            return minimum, maximum
        except Exception:
            return float(range_data["minimum"]), float(range_data["maximum"])

    @staticmethod
    def _ensure_gray_renderer(layer: QgsRasterLayer):
        renderer = layer.renderer()
        if not isinstance(renderer, QgsSingleBandGrayRenderer):
            renderer = QgsSingleBandGrayRenderer(layer.dataProvider(), 1)
            layer.setRenderer(renderer)
            renderer = layer.renderer()
        renderer.setGrayBand(1)
        alpha_band = -1
        provider = layer.dataProvider()
        if provider is not None and provider.bandCount() > 1:
            if provider.colorInterpretation(2) == Qgis.RasterColorInterpretation.AlphaBand:
                alpha_band = 2
        renderer.setAlphaBand(alpha_band)
        return renderer

    @staticmethod
    def _apply_min_max(layer: QgsRasterLayer, value: dict, canvas_extent=None, canvas_crs=None):
        renderer = DynamicLayerStyle._ensure_gray_renderer(layer)
        renderer.setMinMaxOrigin(DynamicLayerStyle._copy_origin(value.get("origin") or {}))

        algorithm = QgsContrastEnhancement.contrastEnhancementAlgorithmFromString(
            value.get("algorithm", "StretchToMinimumMaximum")
        )
        minimum, maximum = DynamicLayerStyle._range_for_composed_layer(
            layer, value, canvas_extent, canvas_crs
        )
        enhancement = QgsContrastEnhancement(layer.dataProvider().dataType(1))
        enhancement.setContrastEnhancementAlgorithm(algorithm)
        enhancement.setMinimumValue(minimum)
        enhancement.setMaximumValue(maximum)
        renderer.setContrastEnhancement(enhancement)

    @staticmethod
    def _apply_transparency(layer: QgsRasterLayer, value: list):
        renderer = DynamicLayerStyle._ensure_gray_renderer(layer)
        transparency = QgsRasterTransparency()
        pixels = []
        for item in value or []:
            try:
                pixel = QgsRasterTransparency.TransparentSingleValuePixel()
                pixel.min = float(item[0])
                pixel.max = float(item[1])
                pixel.percentTransparent = float(item[2])
                pixels.append(pixel)
            except Exception:
                continue
        transparency.setTransparentSingleValuePixelList(pixels)
        renderer.setRasterTransparency(transparency)

    @staticmethod
    def _apply_hue_saturation(layer: QgsRasterLayer, value: dict):
        filter_ = layer.pipe().hueSaturationFilter()
        if filter_ is None:
            filter_ = QgsHueSaturationFilter()
            layer.pipe().set(filter_)
            filter_ = layer.pipe().hueSaturationFilter()
        try:
            filter_.setOn(bool(value.get("on", True)))
            filter_.setSaturation(int(value.get("saturation", 0)))
            filter_.setGrayscaleMode(type(filter_.grayscaleMode())(int(value.get("grayscale_mode", 0))))
            filter_.setColorizeOn(bool(value.get("colorize_on", False)))
            filter_.setColorizeStrength(int(value.get("colorize_strength", 100)))
            rgba = value.get("colorize_rgba", [255, 0, 0, 255])
            filter_.setColorizeColor(QColor(*[int(part) for part in rgba]))
            filter_.setInvertColors(bool(value.get("invert_colors", False)))
        except Exception:
            pass

    @staticmethod
    def _make_resampler(value: str):
        value = str(value or "nearest").lower()
        if "bilinear" in value:
            return QgsBilinearRasterResampler()
        if "cubic" in value:
            return QgsCubicRasterResampler()
        return None

    @staticmethod
    def _set_provider_resampling_method(provider, method_name: str, zoomed_in: bool):
        getter_name = "zoomedInResamplingMethod" if zoomed_in else "zoomedOutResamplingMethod"
        setter_name = "setZoomedInResamplingMethod" if zoomed_in else "setZoomedOutResamplingMethod"
        method_value = DynamicLayerStyle.PROVIDER_RESAMPLING_METHOD_VALUES.get(
            str(method_name or "nearest").lower().replace("_", ""), 0
        )
        try:
            current_method = getattr(provider, getter_name)()
            method = type(current_method)(method_value)
            getattr(provider, setter_name)(method)
        except Exception:
            pass

    @staticmethod
    def _apply_resampling(layer: QgsRasterLayer, value: dict):
        filter_ = QgsRasterResampleFilter()
        filter_.setOn(bool(value.get("on", True)))
        filter_.setMaxOversampling(float(value.get("max_oversampling", 2.0)))
        filter_.setZoomedInResampler(DynamicLayerStyle._make_resampler(value.get("zoomed_in")))
        filter_.setZoomedOutResampler(DynamicLayerStyle._make_resampler(value.get("zoomed_out")))
        layer.pipe().set(filter_)
        if DynamicLayerStyle._is_provider_resampling_stage(value.get("stage")):
            provider = layer.dataProvider()
            if provider is not None:
                DynamicLayerStyle._set_provider_resampling_method(
                    provider, value.get("zoomed_in"), zoomed_in=True
                )
                DynamicLayerStyle._set_provider_resampling_method(
                    provider, value.get("zoomed_out"), zoomed_in=False
                )
                try:
                    provider.setMaxOversampling(float(value.get("max_oversampling", 2.0)))
                except Exception:
                    pass
        if "stage" in value:
            try:
                pipe = layer.pipe()
                pipe.setResamplingStage(type(pipe.resamplingStage())(int(value["stage"])))
            except Exception:
                pass

    @staticmethod
    def _source_profile(source_layer: QgsRasterLayer, composed_layer: QgsRasterLayer, contrast_offset: int, canvas_extent=None) -> dict:
        profile = {}
        source_renderer = source_layer.renderer()
        if source_renderer is not None:
            source_range = DynamicLayerStyle._capture_range(source_layer)
            if source_range is None:
                source_range = DynamicLayerStyle._capture_range(composed_layer)
                if source_range is not None:
                    source_range["manual"] = False
                    source_range["origin"] = {
                        "limits": "MinMax",
                        "extent": "WholeRaster",
                        "accuracy": "Estimated",
                        "cumulative_cut_lower": 0.02,
                        "cumulative_cut_upper": 0.98,
                        "stddev_factor": 2.0,
                    }
            if source_range is not None:
                profile["min_max"] = source_range
            transparency = DynamicLayerStyle._capture_transparency(source_layer)
            profile["transparency"] = transparency or []
            profile["gray_gradient"] = DynamicLayerStyle._capture_gray_gradient(source_layer)
            if profile["gray_gradient"] is None:
                profile["gray_gradient"] = 0
            try:
                profile["opacity"] = float(source_layer.opacity())
            except Exception:
                pass
            try:
                profile["renderer_opacity"] = float(source_renderer.opacity())
            except Exception:
                pass

        brightness = source_layer.pipe().brightnessFilter()
        if brightness is not None:
            profile["brightness"] = int(brightness.brightness())
            profile["contrast"] = max(-100, min(100, int(brightness.contrast()) + contrast_offset))
            profile["gamma"] = float(brightness.gamma())
            profile["brightness_on"] = bool(brightness.on())
        hue = DynamicLayerStyle._capture_hue_saturation(source_layer)
        if hue:
            profile["hue_saturation"] = hue
        profile["resampling"] = DynamicLayerStyle._capture_resampling(source_layer)
        profile["scale_visibility"] = {
            "enabled": bool(source_layer.hasScaleBasedVisibility()),
            "minimum": float(source_layer.minimumScale()),
            "maximum": float(source_layer.maximumScale()),
        }
        try:
            profile["blend_mode"] = int(source_layer.blendMode())
        except Exception:
            pass
        return profile

    @staticmethod
    def _apply_property(
        layer: QgsRasterLayer,
        key: str,
        value: Any,
        canvas_extent=None,
        canvas_crs=None,
    ):
        if key == "min_max":
            DynamicLayerStyle._apply_min_max(layer, value, canvas_extent, canvas_crs)
        elif key == "gray_gradient":
            renderer = DynamicLayerStyle._ensure_gray_renderer(layer)
            try:
                renderer.setGradient(type(renderer.gradient())(int(value)))
            except Exception:
                pass
        elif key == "transparency":
            DynamicLayerStyle._apply_transparency(layer, value)
        elif key == "opacity":
            try:
                layer.setOpacity(float(value))
            except Exception:
                pass
        elif key == "renderer_opacity":
            renderer = layer.renderer()
            if renderer is not None:
                try:
                    renderer.setOpacity(float(value))
                except Exception:
                    pass
        elif key in ("brightness", "contrast", "gamma", "brightness_on"):
            filter_ = layer.pipe().brightnessFilter()
            if filter_ is None:
                return
            try:
                if key == "brightness":
                    filter_.setBrightness(int(value))
                elif key == "contrast":
                    filter_.setContrast(int(value))
                elif key == "gamma":
                    filter_.setGamma(float(value))
                else:
                    filter_.setOn(bool(value))
            except Exception:
                pass
        elif key == "hue_saturation":
            DynamicLayerStyle._apply_hue_saturation(layer, value)
        elif key == "resampling":
            DynamicLayerStyle._apply_resampling(layer, value)
        elif key == "scale_visibility":
            try:
                layer.setScaleBasedVisibility(bool(value["enabled"]))
                layer.setMinimumScale(float(value["minimum"]))
                layer.setMaximumScale(float(value["maximum"]))
            except Exception:
                pass
        elif key == "blend_mode":
            try:
                layer.setBlendMode(type(layer.blendMode())(int(value)))
            except Exception:
                pass

    @staticmethod
    def apply(
        group,
        composed_layer: QgsRasterLayer,
        source_layer: QgsRasterLayer,
        contrast_offset: int = 0,
        canvas_extent=None,
        restore_snapshot: bool = False,
        capture_current: bool = True,
        canvas_crs=None,
    ) -> dict:
        if capture_current:
            state = DynamicLayerStyle.capture_manual_changes(group, composed_layer, save_snapshot=True)
        else:
            state = DynamicLayerStyle._load_state(group)
        if restore_snapshot and state.get("style_xml"):
            try:
                QgsMapLayerStyle(state["style_xml"]).writeToLayer(composed_layer)
            except Exception:
                pass

        # The plugin identity belongs to the canonical layer and must survive style restores.
        baseline = DynamicLayerStyle._source_profile(
            source_layer, composed_layer, contrast_offset, canvas_extent
        )
        overrides = state.get("overrides", {})
        for key, value in baseline.items():
            DynamicLayerStyle._apply_property(
                composed_layer, key, value, canvas_extent, canvas_crs
            )

        automatic_values = DynamicLayerStyle._capture_values(composed_layer)
        automatic_baseline = dict(state.get("baseline", {}))
        for key in baseline:
            if key in automatic_values:
                automatic_baseline[key] = automatic_values[key]

        for key, value in overrides.items():
            if key in baseline:
                DynamicLayerStyle._apply_property(
                    composed_layer, key, value, canvas_extent, canvas_crs
                )

        # Keep the comparison point at the automatic values, apart from active overrides.
        state["baseline"] = automatic_baseline
        style = QgsMapLayerStyle()
        try:
            style.readFromLayer(composed_layer)
            state["style_xml"] = str(style.xmlData())
        except Exception:
            pass
        DynamicLayerStyle._save_state(group, state)
        composed_layer.triggerRepaint()
        return state
