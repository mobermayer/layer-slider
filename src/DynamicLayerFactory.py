from typing import List, Optional
from qgis.core import Qgis, QgsCoordinateTransformContext, QgsLayerTreeGroup, QgsRasterLayer
from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry
from osgeo import gdal
import math
import os
import shutil
import pickle
import hashlib
import threading
import time
import uuid
from .GlobalSettings import GlobalSettings

PLUGIN_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
TMP_DIR = os.path.join(PLUGIN_DIR, ".tmp")

class DynamicLayerFactory:
    CUSTOM_PROPERTY = "layerslider/dynamic_layer"
    ORIGINAL_ID_PROPERTY = "layerslider/original_id"
    GROUP_UUID_PROPERTY = "layerslider/group_uuid"
    ORIGIN_GROUP_UUID_PROPERTY = "layerslider/origin_group_uuid"
    CACHE_KEY_VERSION = 5
    _INT_OUTPUT_TYPES = {
        gdal.GDT_Byte,
        gdal.GDT_UInt16,
        gdal.GDT_Int16,
        gdal.GDT_UInt32,
        gdal.GDT_Int32,
        gdal.GDT_UInt64,
        gdal.GDT_Int64,
    }
    _FLOAT_OUTPUT_TYPES = {
        gdal.GDT_Float32,
        gdal.GDT_Float64,
    }

    _per_key_locks_guard = threading.Lock()
    _per_key_locks: dict[str, threading.Lock] = {}

    @staticmethod
    def _lock_for_cache_key(key: str) -> threading.Lock:
        with DynamicLayerFactory._per_key_locks_guard:
            lock = DynamicLayerFactory._per_key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                DynamicLayerFactory._per_key_locks[key] = lock
            return lock

    @staticmethod
    def create(raster_layers: List[QgsRasterLayer], name="Layer Slider (dynamic)", operation: str = "min"):
        # start = time.time()
        cached_path = DynamicLayerFactory.compute_or_get_cached_path(
            raster_layers,
            operation=operation,
        )
        layer = DynamicLayerFactory.layer_from_cached_path(cached_path, name)
        # print("Dynamic layer creation time:", time.time() - start)
        return layer

    @staticmethod
    def cache_key_for_layers(
        raster_layers: List[QgsRasterLayer],
        operation: str = "min",
        output_datatype: str | None = None,
    ) -> str:
        datatype = output_datatype or GlobalSettings.getComposeOutputDatatype()
        return DynamicLayerFactory._cache_key(raster_layers, operation, datatype)

    @staticmethod
    def cached_path_for_key(key: str) -> str:
        return os.path.join(DynamicLayerFactory.get_cache_dir(), f"{key}.tif")

    @staticmethod
    def layer_from_cached_path(cached_path: str, name: str) -> QgsRasterLayer:
        layer = QgsRasterLayer(cached_path, name, "gdal")
        layer.setCustomProperty(DynamicLayerFactory.CUSTOM_PROPERTY, True)
        layer.setCustomProperty(DynamicLayerFactory.ORIGINAL_ID_PROPERTY, layer.id())
        if not layer.isValid():
            raise RuntimeError(f"Failed to create dynamic layer from {cached_path}")
        return layer

    @staticmethod
    def ensure_layer_slider_group_uuid(group: QgsLayerTreeGroup) -> str:
        try:
            existing = group.customProperty(DynamicLayerFactory.GROUP_UUID_PROPERTY, "") or ""
            if isinstance(existing, str) and existing.strip():
                return existing.strip()
        except Exception:
            pass
        u = uuid.uuid4().hex
        try:
            group.setCustomProperty(DynamicLayerFactory.GROUP_UUID_PROPERTY, u)
        except Exception:
            pass
        return u

    @staticmethod
    def layer_slider_group_uuid(group: QgsLayerTreeGroup) -> str:
        try:
            u = group.customProperty(DynamicLayerFactory.GROUP_UUID_PROPERTY, "") or ""
            return str(u).strip()
        except Exception:
            return ""

    @staticmethod
    def find_group_by_layer_slider_uuid(
        root: QgsLayerTreeGroup,
        uuid_str: str | None,
    ) -> Optional[QgsLayerTreeGroup]:
        if not uuid_str or not str(uuid_str).strip():
            return None
        key = str(uuid_str).strip()
        try:
            for child in root.children():
                if isinstance(child, QgsLayerTreeGroup):
                    try:
                        if (child.customProperty(DynamicLayerFactory.GROUP_UUID_PROPERTY, "") or "") == key:
                            return child
                    except Exception:
                        pass
                    found = DynamicLayerFactory.find_group_by_layer_slider_uuid(child, key)
                    if found is not None:
                        return found
        except Exception:
            pass
        return None

    @staticmethod
    def compute_or_get_cached_path(
        raster_layers: List[QgsRasterLayer],
        operation: str = "min",
        output_datatype: str | None = None,
    ) -> str:
        if not raster_layers:
            raise ValueError("No raster layers provided")

        datatype = output_datatype or GlobalSettings.getComposeOutputDatatype()
        cache_dir = DynamicLayerFactory.get_cache_dir()
        key = DynamicLayerFactory._cache_key(raster_layers, operation, datatype)
        cached_path = os.path.join(cache_dir, f"{key}.tif")
        if os.path.exists(cached_path):
            os.utime(cached_path, None)  # update last-access timestamp for eviction order
        DynamicLayerFactory.clean_cache()  # clean after updating timestamp, but before creating new

        if os.path.exists(cached_path):
            return cached_path

        lock = DynamicLayerFactory._lock_for_cache_key(key)
        with lock:
            if os.path.exists(cached_path):
                try:
                    os.utime(cached_path, None)
                except Exception:
                    pass
                return cached_path

            run_token = uuid.uuid4().hex
            work_tag = f"{key}_{run_token}"
            gray_path = None
            alpha_path = None
            staging_path = os.path.join(TMP_DIR, f".staging_{work_tag}.tif")
            try:
                os.makedirs(TMP_DIR, exist_ok=True)
                gray_path = DynamicLayerFactory._compute_grayscale(raster_layers, work_tag, operation)
                alpha_path = DynamicLayerFactory._compute_alpha_max(raster_layers, work_tag)
                DynamicLayerFactory._combine_gray_alpha(
                    gray_path,
                    alpha_path,
                    staging_path,
                    raster_layers[0].width(),
                    raster_layers[0].height(),
                    raster_layers,
                    datatype,
                )

                os.makedirs(cache_dir, exist_ok=True)
                os.replace(staging_path, cached_path)
                staging_path = None
                return cached_path
            finally:
                for path in (gray_path, alpha_path, staging_path):
                    if not path:
                        continue
                    try:
                        os.remove(path)
                    except FileNotFoundError:
                        pass

    @staticmethod
    def _compose_expression(expr_parts: List[str], operation: str) -> str:
        if not expr_parts:
            raise ValueError("No layers provided for compose expression")

        op = (operation or "mean").lower()
        if op == "mean":
            return "(" + " + ".join(expr_parts) + f") / {len(expr_parts)}"
        if op == "sum":
            return "(" + " + ".join(expr_parts) + ")"
        if op == "min":
            return DynamicLayerFactory.binary_unbalanced_expression(expr_parts, "min")
        if op == "max":
            return DynamicLayerFactory.binary_unbalanced_expression(expr_parts, "max")
        if op == "range":
            min_expr = DynamicLayerFactory.binary_unbalanced_expression(expr_parts, "min")
            max_expr = DynamicLayerFactory.binary_unbalanced_expression(expr_parts, "max")
            return f"({max_expr}) - ({min_expr})"
        if op == "rms":
            squares = [f"(({part}) * ({part}))" for part in expr_parts]
            return f"sqrt(({ ' + '.join(squares) }) / {len(expr_parts)})"
        if op == "geomean":
            product = " * ".join([f"({part})" for part in expr_parts])
            exponent = 1.0 / len(expr_parts)
            return f"({product}) ^ {exponent:.12g}"
        if op == "stddev":
            mean_expr = "(" + " + ".join(expr_parts) + f") / {len(expr_parts)}"
            squares = [f"(({part}) * ({part}))" for part in expr_parts]
            mean_square_expr = "(" + " + ".join(squares) + f") / {len(expr_parts)}"
            variance_expr = f"({mean_square_expr}) - (({mean_expr}) * ({mean_expr}))"
            return f"sqrt(max(0, {variance_expr}))"

        return DynamicLayerFactory.binary_unbalanced_expression(expr_parts, "min")

    @staticmethod
    def _compute_grayscale(raster_layers: List[QgsRasterLayer], work_tag: str, operation: str) -> str:
        os.makedirs(TMP_DIR, exist_ok=True)
        tmp_path = os.path.join(TMP_DIR, f"{work_tag}_gray.tif")
        layer_exprs = []
        entries = []

        for i, layer in enumerate(raster_layers):
            non_alpha_bands: List[int]
            provider = layer.dataProvider()
            non_alpha_bands = [
                b for b in range(1, layer.bandCount() + 1)
                if provider.colorInterpretation(b) != Qgis.RasterColorInterpretation.AlphaBand
            ]

            if not non_alpha_bands:
                raise ValueError(f"Layer {layer.name()} has no non-alpha bands")

            refs = []
            for b in non_alpha_bands:
                entry = QgsRasterCalculatorEntry()
                entry.ref = f"layer{i}_b{b}@{b}"
                entry.raster = layer
                entry.bandNumber = b
                entries.append(entry)
                refs.append(entry.ref)
            entry_ref = "(" + " + ".join(refs) + f") / {len(refs)}"
            layer_exprs.append(entry_ref)

        expr = DynamicLayerFactory._compose_expression(layer_exprs, operation)

        calc = QgsRasterCalculator(
            expr,
            tmp_path,
            "GTiff",
            raster_layers[0].extent(),
            raster_layers[0].crs(),
            raster_layers[0].width(),
            raster_layers[0].height(),
            entries,
            QgsCoordinateTransformContext(),
        )

        if calc.processCalculation() != QgsRasterCalculator.Result.Success:
            raise RuntimeError(f"Grayscale averaging failed: {calc.lastError()}")

        return tmp_path

    @staticmethod
    def _compute_alpha_max(raster_layers: List[QgsRasterLayer], work_tag: str) -> Optional[str]:
        os.makedirs(TMP_DIR, exist_ok=True)
        tmp_path = os.path.join(TMP_DIR, f"{work_tag}_alpha.tif")
        extent = raster_layers[0].extent()
        crs = raster_layers[0].crs()
        width = raster_layers[0].width()
        height = raster_layers[0].height()

        entries: List[QgsRasterCalculatorEntry] = []
        for i, layer in enumerate(raster_layers):
            ds = gdal.Open(layer.dataProvider().dataSourceUri())
            alpha_bands = [
                b for b in range(1, ds.RasterCount + 1)
                if ds.GetRasterBand(b).GetColorInterpretation() == gdal.GCI_AlphaBand
            ]
            ds = None

            if alpha_bands:
                for b in alpha_bands:
                    entry = QgsRasterCalculatorEntry()
                    entry.ref = f"layer{i}_a{b}@{b}"
                    entry.raster = layer
                    entry.bandNumber = b
                    entries.append(entry)

        if len(entries) == 0:
            return None
        elif len(entries) == 1:
            src = entries[0].raster.source()
            shutil.copyfile(src, tmp_path)
            return tmp_path
        else:
            expr = DynamicLayerFactory.binary_unbalanced_expression([entry.ref for entry in entries], "max")
            calc = QgsRasterCalculator(
                expr,
                tmp_path,
                "GTiff",
                extent,
                crs,
                width,
                height,
                entries,
                QgsCoordinateTransformContext()
            )

            if calc.processCalculation() != QgsRasterCalculator.Result.Success:
                raise RuntimeError(f"Alpha max calculation failed: {calc.lastError()}")

            # mark as alpha band
            ds = gdal.Open(tmp_path)
            ds.GetRasterBand(1).SetColorInterpretation(gdal.GCI_AlphaBand)
            ds.FlushCache()
            ds = None

            return tmp_path

    @staticmethod
    def binary_unbalanced_expression(expr_parts, operation: str):
        """
        Generate left-heavy binary max expression for QGIS raster calculator.
        Necessary because max is a binary operation here.
        e.g. ['a','b','c','d'] -> max(a, max(b, max(c, d)))
        """
        if not expr_parts:
            raise ValueError("No layers provided for max expression")

        expr = expr_parts[-1]  # start from last element
        for part in reversed(expr_parts[:-1]):
            expr = f"{operation}({part}, {expr})"
        return expr

    @staticmethod
    def _resolve_output_datatype(raster_layers: List[QgsRasterLayer], output_datatype: str) -> int:
        if output_datatype == "first_layer":
            detected = DynamicLayerFactory._detect_first_layer_datatype(raster_layers)
            if detected in DynamicLayerFactory._INT_OUTPUT_TYPES or detected in DynamicLayerFactory._FLOAT_OUTPUT_TYPES:
                return detected
            return gdal.GDT_Float32
        resolved = GlobalSettings.getComposeOutputDatatypeGdalType(output_datatype)
        if isinstance(resolved, int):
            return resolved
        return gdal.GDT_Byte

    @staticmethod
    def _integer_bounds(datatype: int) -> Optional[tuple[int, int]]:
        if datatype == gdal.GDT_Byte:
            return (0, 255)
        if datatype == gdal.GDT_UInt16:
            return (0, 65535)
        if datatype == gdal.GDT_Int16:
            return (-32768, 32767)
        if datatype == gdal.GDT_UInt32:
            return (0, 4294967295)
        if datatype == gdal.GDT_Int32:
            return (-2147483648, 2147483647)
        if datatype == gdal.GDT_UInt64:
            return (0, 18446744073709551615)
        if datatype == gdal.GDT_Int64:
            return (-9223372036854775808, 9223372036854775807)
        return None

    @staticmethod
    def _set_band_nodata(band, nodata_value, datatype: int):
        try:
            coerced_nodata = DynamicLayerFactory._coerce_nodata_value(nodata_value, datatype)
            if coerced_nodata is None:
                return
            band.SetNoDataValue(coerced_nodata)
        except Exception:
            return

    @staticmethod
    def _coerce_nodata_value(nodata_value, datatype: int):
        if nodata_value is None:
            return None

        nodata_float = float(nodata_value)
        if not math.isfinite(nodata_float):
            return None

        if datatype in DynamicLayerFactory._INT_OUTPUT_TYPES:
            nodata_int = int(round(nodata_float))
            bounds = DynamicLayerFactory._integer_bounds(datatype)
            if bounds is not None:
                nodata_int = max(bounds[0], min(bounds[1], nodata_int))
            return nodata_int

        if datatype in DynamicLayerFactory._FLOAT_OUTPUT_TYPES:
            return nodata_float

        return None

    @staticmethod
    def _detect_first_layer_datatype(raster_layers: List[QgsRasterLayer]) -> int:
        datatype, _nodata = DynamicLayerFactory._read_first_layer_band_info(raster_layers)
        return datatype

    @staticmethod
    def _detect_first_layer_nodata(raster_layers: List[QgsRasterLayer]) -> Optional[float]:
        _datatype, nodata = DynamicLayerFactory._read_first_layer_band_info(raster_layers)
        return nodata

    @staticmethod
    def _first_non_alpha_band_index(layer: QgsRasterLayer) -> Optional[int]:
        provider = layer.dataProvider()
        for band_idx in range(1, layer.bandCount() + 1):
            try:
                if provider.colorInterpretation(band_idx) != Qgis.RasterColorInterpretation.AlphaBand:
                    return band_idx
            except Exception:
                return band_idx
        return None

    @staticmethod
    def _read_first_layer_band_info(raster_layers: List[QgsRasterLayer]) -> tuple[int, Optional[float]]:
        if not raster_layers:
            return (gdal.GDT_Byte, None)

        layer = raster_layers[0]
        provider = layer.dataProvider()
        non_alpha_band = DynamicLayerFactory._first_non_alpha_band_index(layer)
        if non_alpha_band is None:
            return (gdal.GDT_Byte, None)

        detected_datatype: Optional[int] = None
        detected_nodata: Optional[float] = None

        try:
            candidate_dtype = int(provider.dataType(non_alpha_band))
            if candidate_dtype != int(gdal.GDT_Unknown):
                detected_datatype = candidate_dtype
        except Exception:
            pass

        try:
            if hasattr(provider, "sourceHasNoDataValue") and provider.sourceHasNoDataValue(non_alpha_band):
                candidate_nodata = provider.sourceNoDataValue(non_alpha_band)
                if candidate_nodata is not None:
                    candidate_nodata_f = float(candidate_nodata)
                    if math.isfinite(candidate_nodata_f):
                        detected_nodata = candidate_nodata_f
        except Exception:
            pass

        source_uris = [provider.dataSourceUri(), layer.source()]
        seen = set()
        for source_uri in source_uris:
            if not source_uri:
                continue
            for candidate in (source_uri, source_uri.split("|", 1)[0]):
                if not candidate or candidate in seen:
                    continue
                seen.add(candidate)
                ds = gdal.Open(candidate)
                if ds is None:
                    continue
                try:
                    band_index = min(max(non_alpha_band, 1), ds.RasterCount if ds.RasterCount > 0 else 1)
                    band = ds.GetRasterBand(band_index)
                    if band is None:
                        continue
                    if detected_datatype is None and int(band.DataType) != int(gdal.GDT_Unknown):
                        detected_datatype = int(band.DataType)
                    if detected_nodata is None:
                        candidate_nodata = band.GetNoDataValue()
                        if candidate_nodata is not None:
                            candidate_nodata_f = float(candidate_nodata)
                            if math.isfinite(candidate_nodata_f):
                                detected_nodata = candidate_nodata_f
                finally:
                    ds = None
                if detected_datatype is not None and detected_nodata is not None:
                    break
            if detected_datatype is not None and detected_nodata is not None:
                break

        if detected_datatype is None:
            detected_datatype = gdal.GDT_Byte
        return (detected_datatype, detected_nodata)

    @staticmethod
    def _combine_gray_alpha(
        gray_path: str,
        alpha_path: Optional[str],
        output_path: str,
        width: int,
        height: int,
        raster_layers: List[QgsRasterLayer],
        output_datatype: str,
    ) -> str:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        ds_gray = gdal.Open(gray_path)
        non_alpha_bands = [
            b for b in range(1, ds_gray.RasterCount + 1)
            if ds_gray.GetRasterBand(b).GetColorInterpretation() != gdal.GCI_AlphaBand
        ]
        gray_rasterband = ds_gray.GetRasterBand(non_alpha_bands[0])
        gray_array = gray_rasterband.ReadAsArray()
        gray_nodata = gray_rasterband.GetNoDataValue()
        gray_mask_array = None
        try:
            gray_mask_band = gray_rasterband.GetMaskBand()
            if gray_mask_band is not None:
                gray_mask_array = gray_mask_band.ReadAsArray()
        except Exception:
            gray_mask_array = None
        datatype = DynamicLayerFactory._resolve_output_datatype(raster_layers, output_datatype)
        geotransform = ds_gray.GetGeoTransform()
        projection = ds_gray.GetProjection()
        ds_gray = None

        alpha_array = []
        if alpha_path:
          ds_alpha = gdal.Open(alpha_path)
          alpha_bands = [
              b for b in range(1, ds_alpha.RasterCount + 1)
              if ds_alpha.GetRasterBand(b).GetColorInterpretation() == gdal.GCI_AlphaBand
          ]
          if alpha_bands:
              alpha_array = ds_alpha.GetRasterBand(alpha_bands[0]).ReadAsArray()
          ds_alpha = None
        num_channels = 2 if len(alpha_array) > 0 else 1
        source_nodata = DynamicLayerFactory._detect_first_layer_nodata(raster_layers)
        output_nodata = None
        if num_channels == 1 and source_nodata is not None:
            output_nodata = DynamicLayerFactory._coerce_nodata_value(source_nodata, datatype)
            if output_nodata is not None:
                gray_array = gray_array.copy()
                invalid_mask = None
                if gray_mask_array is not None:
                    invalid_mask = (gray_mask_array == 0)
                if gray_nodata is not None:
                    nodata_float = float(gray_nodata)
                    if math.isfinite(nodata_float):
                        tolerance = max(1e-12, abs(nodata_float) * 1e-12)
                        nodata_by_value = abs(gray_array - nodata_float) <= tolerance
                        if invalid_mask is None:
                            invalid_mask = nodata_by_value
                        else:
                            invalid_mask = invalid_mask | nodata_by_value
                if invalid_mask is not None:
                    gray_array[invalid_mask] = output_nodata

        driver = gdal.GetDriverByName("GTiff")
        ds = driver.Create(
            output_path, width, height, num_channels, datatype,
            # options=[ # 1.6s; 15.9MB
            #     "COMPRESS=PACKBITS",
            #     "TILED=YES",
            #     "BIGTIFF=IF_SAFER",
            # ],
            # options=[ # 1.6s; 5.9MB
            #     "COMPRESS=ZSTD",
            #     "ZSTD_LEVEL=1"
            #     "PREDICTOR=2",
            #     "TILED=YES",
            #     "BIGTIFF=IF_SAFER",
            # ],
            options=[ # 1.7s; 5.4MB
                "COMPRESS=DEFLATE",
                "PREDICTOR=2",
                "TILED=YES",
                "ZLEVEL=1",
                "BIGTIFF=IF_SAFER",
            ],
            # options=[ # 1.8s; 5.1MB
            #     "COMPRESS=LZW",
            #     "PREDICTOR=2",
            #     "TILED=YES",
            #     "BIGTIFF=IF_SAFER",
            # ],
            # options=[ # 8.4s; 4.5MB
            #     "COMPRESS=DEFLATE",
            #     "PREDICTOR=2",
            #     "TILED=YES",
            #     "ZLEVEL=9",
            #     "BIGTIFF=IF_SAFER",
            # ],
        )
        ds.SetGeoTransform(geotransform)
        ds.SetProjection(projection)

        ds.GetRasterBand(1).WriteArray(gray_array)
        if output_nodata is not None:
            DynamicLayerFactory._set_band_nodata(ds.GetRasterBand(1), output_nodata, datatype)
        ds.GetRasterBand(1).SetColorInterpretation(gdal.GCI_GrayIndex)

        if len(alpha_array) > 0:
            ds.GetRasterBand(2).WriteArray(alpha_array)
            ds.GetRasterBand(2).SetColorInterpretation(gdal.GCI_AlphaBand)

        ds.FlushCache()
        ds = None
        return output_path

    @staticmethod
    def _raster_fs_key(layer: QgsRasterLayer):
        path = layer.dataProvider().dataSourceUri()
        try:
            st = os.stat(path)
            return (path, st.st_mtime, st.st_size)
        except FileNotFoundError:
            return (path, time.time(), -1)

    @staticmethod
    def _cache_key(raster_layers: List[QgsRasterLayer], operation: str, output_datatype: str):
        keys = [DynamicLayerFactory._raster_fs_key(l) for l in raster_layers]
        raw = pickle.dumps((DynamicLayerFactory.CACHE_KEY_VERSION, keys, operation, output_datatype))
        return hashlib.sha1(raw).hexdigest()

    @staticmethod
    def get_max_cache_bytes() -> int:
        return GlobalSettings.getMaxCacheMB() * 1024 * 1024

    @staticmethod
    def get_cache_dir() -> str:
        return GlobalSettings.getCacheDirectory()

    @staticmethod
    def clean_cache():
        """Delete oldest cached files by last-access-time until cache is under limit."""
        max_bytes = DynamicLayerFactory.get_max_cache_bytes()
        cache_dir = DynamicLayerFactory.get_cache_dir()
        if not os.path.isdir(cache_dir):
            return

        try:
            entries = []
            total_size = 0
            with os.scandir(cache_dir) as it:
                for entry in it:
                    if entry.is_file():
                        try:
                            st = entry.stat()
                            entries.append((entry.path, st.st_atime, st.st_size))
                            total_size += st.st_size
                        except FileNotFoundError:
                            pass  # file removed during scan

            if total_size <= max_bytes: return

            # Sort by oldest last-access time
            entries.sort(key=lambda x: x[1])  # (path, atime, size)

            # Remove oldest until under threshold
            for path, atime, size in entries:
                try:
                    os.remove(path)
                    total_size -= size
                except FileNotFoundError:
                    pass
                if total_size <= max_bytes:
                    break
        except Exception as e:
            print(f"Cache cleanup error: {e}")
