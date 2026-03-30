import os
from typing import Optional
from qgis.PyQt.QtCore import QSettings, Qt
from osgeo import gdal


class GlobalSettings():
    PREFIX = 'LayerSelector'
    PLUGIN_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    DEFAULT_CACHE_DIR = os.path.join(PLUGIN_DIR, ".cache")
    COMPOSE_OPERATIONS = (
        ("mean", "Mean", "Arithmetic Mean"),
        ("min", "Min", "Minimum"),
        ("max", "Max", "Maximum"),
        ("rms", "RMS", "Root Mean Square"),
        ("geomean", "GeoMean", "Geometric Mean"),
        ("sum", "Sum", "Sum"),
        ("range", "Range", "Range (Max - Min)"),
        ("stddev", "StdDev", "Standard Deviation"),
    )
    COMPOSE_OUTPUT_DATATYPES = (
        ("first_layer", "First input layer", None),
        ("byte", "BYTE", "GDT_Byte"),
        ("uint16", "UInt16", "GDT_UInt16"),
        ("int16", "Int16", "GDT_Int16"),
        ("uint32", "UInt32", "GDT_UInt32"),
        ("int32", "Int32", "GDT_Int32"),
        ("uint64", "UInt64", "GDT_UInt64"),
        ("int64", "Int64", "GDT_Int64"),
        ("float32", "Float32", "GDT_Float32"),
        ("float64", "Float64", "GDT_Float64"),
        ("cint16", "CInt16", "GDT_CInt16"),
        ("cint32", "CInt32", "GDT_CInt32"),
        ("cfloat32", "CFloat32", "GDT_CFloat32"),
        ("cfloat64", "CFloat64", "GDT_CFloat64"),
    )
    DISABLED_COMPOSE_OUTPUT_DATATYPES = {
        "cint16",
        "cint32",
        "cfloat32",
        "cfloat64",
    }


    @classmethod
    def getWidgetLocation(cls) -> Qt.DockWidgetArea:
        serialized: Optional[int] = QSettings().value(f"{cls.PREFIX}/widgetLocation")
        if serialized is None:
            return Qt.DockWidgetArea.LeftDockWidgetArea
        else:
            return Qt.DockWidgetArea(serialized)

    @classmethod
    def setWidgetLocation(cls, value: Optional[Qt.DockWidgetArea]):
        serialized = None if value is None else int(value)
        QSettings().setValue(f"{cls.PREFIX}/widgetLocation", serialized)


    @classmethod
    def getWidgetVisible(cls) -> bool:
        return QSettings().value(f"{cls.PREFIX}/widgetVisible", True, bool)

    @classmethod
    def setWidgetVisible(cls, value: Optional[bool]):
        QSettings().setValue(f"{cls.PREFIX}/widgetVisible", value)


    @classmethod
    def getMaxCacheMB(cls) -> int:
        return QSettings().value(f"{cls.PREFIX}/maxCacheMB", 1000, int)

    @classmethod
    def setMaxCacheMB(cls, value: Optional[int]):
        QSettings().setValue(f"{cls.PREFIX}/maxCacheMB", value)

    @classmethod
    def getCacheDirectory(cls) -> str:
        value = QSettings().value(f"{cls.PREFIX}/cacheDirectory", cls.DEFAULT_CACHE_DIR, str)
        if not value:
            return cls.DEFAULT_CACHE_DIR
        return os.path.abspath(os.path.expanduser(value))

    @classmethod
    def setCacheDirectory(cls, value: Optional[str]):
        normalized = cls.DEFAULT_CACHE_DIR
        if isinstance(value, str) and value.strip():
            normalized = os.path.abspath(os.path.expanduser(value.strip()))
        QSettings().setValue(f"{cls.PREFIX}/cacheDirectory", normalized)

    @classmethod
    def getPrecalcExportDirectory(cls) -> str:
        value = QSettings().value(f"{cls.PREFIX}/precalcExportDirectory", cls.getCacheDirectory(), str)
        if not value:
            return cls.getCacheDirectory()
        return os.path.abspath(os.path.expanduser(value))

    @classmethod
    def setPrecalcExportDirectory(cls, value: Optional[str]):
        normalized = cls.getCacheDirectory()
        if isinstance(value, str) and value.strip():
            normalized = os.path.abspath(os.path.expanduser(value.strip()))
        QSettings().setValue(f"{cls.PREFIX}/precalcExportDirectory", normalized)

    @classmethod
    def getPrecalcExportAddToQgis(cls) -> bool:
        return cls.getExportAddToQgis()

    @classmethod
    def setPrecalcExportAddToQgis(cls, value: Optional[bool]):
        cls.setExportAddToQgis(value)

    @classmethod
    def getExportAddToQgis(cls) -> bool:
        return QSettings().value(f"{cls.PREFIX}/precalcExportAddToQgis", True, bool)

    @classmethod
    def setExportAddToQgis(cls, value: Optional[bool]):
        QSettings().setValue(f"{cls.PREFIX}/precalcExportAddToQgis", bool(value))

    @classmethod
    def getDynamicExportMode(cls) -> str:
        mode = QSettings().value(f"{cls.PREFIX}/dynamicExportMode", "directory", str)
        if mode in {"directory", "file"}:
            return mode
        return "directory"

    @classmethod
    def setDynamicExportMode(cls, value: Optional[str]):
        mode = value if value in {"directory", "file"} else "directory"
        QSettings().setValue(f"{cls.PREFIX}/dynamicExportMode", mode)

    @classmethod
    def getDynamicExportPath(cls) -> str:
        value = QSettings().value(f"{cls.PREFIX}/dynamicExportPath", cls.getPrecalcExportDirectory(), str)
        if not value:
            return cls.getPrecalcExportDirectory()
        normalized = os.path.abspath(os.path.expanduser(value))
        if os.path.isdir(normalized):
            return normalized

        # Backward compatibility: older versions persisted a file path.
        parent_dir = os.path.dirname(normalized)
        if os.path.isfile(normalized):
            return parent_dir or cls.getPrecalcExportDirectory()
        if os.path.splitext(normalized)[1] and parent_dir:
            return parent_dir
        return normalized

    @classmethod
    def setDynamicExportPath(cls, value: Optional[str]):
        normalized = cls.getPrecalcExportDirectory()
        if isinstance(value, str) and value.strip():
            candidate = os.path.abspath(os.path.expanduser(value.strip()))
            if os.path.isdir(candidate):
                normalized = candidate
            else:
                parent_dir = os.path.dirname(candidate)
                if os.path.isfile(candidate):
                    normalized = parent_dir or normalized
                elif os.path.splitext(candidate)[1] and parent_dir:
                    normalized = parent_dir
                else:
                    normalized = candidate
        QSettings().setValue(f"{cls.PREFIX}/dynamicExportPath", normalized)

    @classmethod
    def getDynamicExportAddToQgis(cls) -> bool:
        return cls.getExportAddToQgis()

    @classmethod
    def setDynamicExportAddToQgis(cls, value: Optional[bool]):
        cls.setExportAddToQgis(value)


    @classmethod
    def getNumAvgrasters(cls) -> int:
        return QSettings().value(f"{cls.PREFIX}/numAvgrasters", 2, int)

    @classmethod
    def setNumAvgrasters(cls, value: Optional[int]):
        QSettings().setValue(f"{cls.PREFIX}/numAvgrasters", value)

    @classmethod
    def getChkDistinct(cls) -> bool:
        return QSettings().value(f"{cls.PREFIX}/chk_distinct", False, bool)

    @classmethod
    def setChkDistinct(cls, value: Optional[bool]):
        QSettings().setValue(f"{cls.PREFIX}/chk_distinct", value)

    @classmethod
    def getDistinctOffset(cls) -> int:
        return QSettings().value(f"{cls.PREFIX}/distinct_offset", 0, int)

    @classmethod
    def setDistinctOffset(cls, value: Optional[int]):
        QSettings().setValue(f"{cls.PREFIX}/distinct_offset", value)

    @classmethod
    def getComposeOperation(cls) -> str:
        valid_keys = {key for key, _, _ in cls.COMPOSE_OPERATIONS}
        operation = QSettings().value(f"{cls.PREFIX}/compose_operation", "mean", str)
        return operation if operation in valid_keys else "mean"

    @classmethod
    def setComposeOperation(cls, value: Optional[str]):
        valid_keys = {key for key, _, _ in cls.COMPOSE_OPERATIONS}
        QSettings().setValue(f"{cls.PREFIX}/compose_operation", value if value in valid_keys else "mean")

    @classmethod
    def getComposeOperationShortName(cls, operation: str | None = None) -> str:
        current = operation or cls.getComposeOperation()
        for key, short_name, _ in cls.COMPOSE_OPERATIONS:
            if key == current:
                return short_name
        return "Min"

    @classmethod
    def getComposeOperationFullName(cls, operation: str | None = None) -> str:
        current = operation or cls.getComposeOperation()
        for key, _short_name, full_name in cls.COMPOSE_OPERATIONS:
            if key == current:
                return full_name
        return "Minimum"

    @classmethod
    def getComposeOutputDatatypeOptions(cls) -> list[tuple[str, str]]:
        options: list[tuple[str, str]] = []
        for key, label, gdal_const_name in cls.COMPOSE_OUTPUT_DATATYPES:
            if key in cls.DISABLED_COMPOSE_OUTPUT_DATATYPES:
                continue
            if gdal_const_name is None or hasattr(gdal, gdal_const_name):
                options.append((key, label))
        return options

    @classmethod
    def getComposeOutputDatatype(cls) -> str:
        valid_keys = {key for key, _label in cls.getComposeOutputDatatypeOptions()}
        value = QSettings().value(f"{cls.PREFIX}/compose_output_datatype", "first_layer", str)
        return value if value in valid_keys else "first_layer"

    @classmethod
    def setComposeOutputDatatype(cls, value: Optional[str]):
        valid_keys = {key for key, _label in cls.getComposeOutputDatatypeOptions()}
        QSettings().setValue(f"{cls.PREFIX}/compose_output_datatype", value if value in valid_keys else "first_layer")

    @classmethod
    def getComposeOutputDatatypeGdalType(cls, datatype: str | None = None) -> Optional[int]:
        current = datatype or cls.getComposeOutputDatatype()
        for key, _label, gdal_const_name in cls.COMPOSE_OUTPUT_DATATYPES:
            if key != current:
                continue
            if gdal_const_name is None:
                return None
            return getattr(gdal, gdal_const_name, None)
        return gdal.GDT_Byte

    @classmethod
    def getComposeContrastMode(cls) -> str:
        mode = QSettings().value(f"{cls.PREFIX}/compose_contrast_mode", "auto", str)
        if mode in {"auto", "constant"}:
            return mode
        return "auto"

    @classmethod
    def setComposeContrastMode(cls, value: Optional[str]):
        if value in {"auto", "constant"}:
            QSettings().setValue(f"{cls.PREFIX}/compose_contrast_mode", value)
        else:
            QSettings().setValue(f"{cls.PREFIX}/compose_contrast_mode", "auto")

    @classmethod
    def getComposeContrastN(cls) -> int:
        return QSettings().value(f"{cls.PREFIX}/compose_contrast_n", 5, int)

    @classmethod
    def setComposeContrastN(cls, value: Optional[int]):
        QSettings().setValue(f"{cls.PREFIX}/compose_contrast_n", value)

    @classmethod
    def getComposeContrastValue(cls) -> int:
        return QSettings().value(f"{cls.PREFIX}/compose_contrast_value", 0, int)

    @classmethod
    def setComposeContrastValue(cls, value: Optional[int]):
        QSettings().setValue(f"{cls.PREFIX}/compose_contrast_value", value)
