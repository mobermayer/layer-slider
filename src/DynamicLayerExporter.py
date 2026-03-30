from typing import Optional
import os
import shutil
import traceback
from urllib.parse import unquote, urlparse

from qgis.PyQt.QtGui import QAction
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.core import (
    QgsLayerTreeLayer,
    QgsLayerTreeNode,
    QgsMapLayer,
    QgsProject,
    QgsRasterLayer,
)

from .ComposeManager import ComposeManager
from .DynamicLayerFactory import DynamicLayerFactory
from .ExportDialog import ExportDialog
from .GlobalSettings import GlobalSettings


class DynamicLayerExporter:
    def __init__(self, dock):
        self.dock = dock
        self._tree_context_menu_connected = False

    # ------------------------------------------------------------------
    # Context menu integration
    # ------------------------------------------------------------------
    def connect_tree_context_menu(self):
        if self._tree_context_menu_connected:
            return
        lt = self.dock.iface.layerTreeView()
        if lt is None:
            return
        if not hasattr(lt, "contextMenuAboutToShow"):
            return
        lt.contextMenuAboutToShow.connect(self._on_context_menu_about_to_show)
        self._tree_context_menu_connected = True

    def disconnect_tree_context_menu(self):
        if not self._tree_context_menu_connected:
            return
        try:
            lt = self.dock.iface.layerTreeView()
            if lt is not None and hasattr(lt, "contextMenuAboutToShow"):
                lt.contextMenuAboutToShow.disconnect(self._on_context_menu_about_to_show)
        except Exception:
            pass
        self._tree_context_menu_connected = False

    def _on_context_menu_about_to_show(self, menu):
        if self.dock._unloaded or menu is None:
            return
        dynamic_layer = self._selected_dynamic_layer()
        if dynamic_layer is None:
            return
        if menu.actions():
            menu.addSeparator()
        export_action = QAction("Export composed layer...", menu)
        export_action.triggered.connect(
            lambda _checked=False, layer=dynamic_layer: self.export_interactive(layer)
        )
        menu.addAction(export_action)

    # ------------------------------------------------------------------
    # Selection helpers
    # ------------------------------------------------------------------
    def _selected_dynamic_layer(self) -> Optional[QgsRasterLayer]:
        node = self._current_layer_tree_node()
        if isinstance(node, QgsLayerTreeLayer):
            layer = node.layer()
            if self._is_plugin_dynamic_raster_layer(layer):
                return layer

        lt = self.dock.iface.layerTreeView()
        if lt is None or not hasattr(lt, "selectedNodes"):
            return None
        for selected_node in lt.selectedNodes():
            if not isinstance(selected_node, QgsLayerTreeLayer):
                continue
            layer = selected_node.layer()
            if self._is_plugin_dynamic_raster_layer(layer):
                return layer
        return None

    def _current_layer_tree_node(self) -> Optional[QgsLayerTreeNode]:
        lt = self.dock.iface.layerTreeView()
        if lt is None:
            return None
        if hasattr(lt, "currentNode"):
            node = lt.currentNode()
            if node is not None:
                return node
        current_index = lt.currentIndex()
        if current_index.isValid():
            return lt.index2node(current_index)
        return None

    @staticmethod
    def _is_plugin_dynamic_raster_layer(layer: Optional[QgsMapLayer]) -> bool:
        if not isinstance(layer, QgsRasterLayer):
            return False
        return bool(layer.customProperty(DynamicLayerFactory.CUSTOM_PROPERTY, False))

    # ------------------------------------------------------------------
    # Source resolution
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_layer_source_file_path(layer: QgsRasterLayer) -> Optional[str]:
        source_candidates = []
        provider = layer.dataProvider()
        if provider is not None:
            source_candidates.append(provider.dataSourceUri())
        source_candidates.append(layer.source())

        for source in source_candidates:
            source_text = str(source or "").strip()
            if not source_text:
                continue
            for candidate in (source_text, source_text.split("|", 1)[0]):
                path = candidate.strip()
                if not path:
                    continue
                if path.lower().startswith("file://"):
                    parsed = urlparse(path)
                    path = unquote(parsed.path or "")
                path = os.path.abspath(os.path.expanduser(path))
                if os.path.isfile(path):
                    return path
        return None

    # ------------------------------------------------------------------
    # Export dialog & execution
    # ------------------------------------------------------------------
    def _default_export_filename(self, layer: QgsRasterLayer, source_path: str) -> str:
        extension = os.path.splitext(source_path)[1] or ".tif"
        if not extension.startswith("."):
            extension = f".{extension}"
        base_name = ComposeManager.sanitize_export_filename(layer.name())
        return f"{base_name}{extension}"

    def _show_export_dialog(self, layer: QgsRasterLayer, source_path: str):
        default_file_name = self._default_export_filename(layer, source_path)
        initial_directory = GlobalSettings.getDynamicExportPath()
        initial_file_path = os.path.join(initial_directory, default_file_name)
        dialog = ExportDialog(
            self.dock,
            window_title="Export composed layer",
            allow_file_destination=True,
            initial_destination_mode=ExportDialog.DESTINATION_FILE,
            fixed_destination_mode=ExportDialog.DESTINATION_FILE,
            initial_destination_path=initial_file_path,
            default_file_name=default_file_name,
            initial_add_to_qgis=GlobalSettings.getExportAddToQgis(),
            persist_to_precalc_settings=False,
        )
        accepted = dialog.exec_() if hasattr(dialog, "exec_") else dialog.exec()
        if not accepted:
            return None
        GlobalSettings.setDynamicExportMode(dialog.selected_destination_mode())
        GlobalSettings.setDynamicExportPath(dialog.selected_directory())
        GlobalSettings.setExportAddToQgis(dialog.add_to_qgis())
        return {
            "destination_mode": dialog.selected_destination_mode(),
            "destination": dialog.selected_destination(),
            "add_to_qgis": dialog.add_to_qgis(),
        }

    def _resolve_export_output_path(
        self,
        layer: QgsRasterLayer,
        source_path: str,
        destination_mode: str,
        destination: str,
    ) -> str:
        if destination_mode == ExportDialog.DESTINATION_FILE:
            return destination
        filename = self._default_export_filename(layer, source_path)
        return os.path.join(destination, filename)

    def export_interactive(self, layer: QgsRasterLayer):
        source_path = self._resolve_layer_source_file_path(layer)
        if not source_path:
            QMessageBox.warning(
                self.dock,
                "Export failed",
                "Could not resolve a file path for the selected composed layer.",
            )
            return

        options = self._show_export_dialog(layer, source_path)
        if not options:
            return

        destination = os.path.abspath(os.path.expanduser(options["destination"]))
        output_path = self._resolve_export_output_path(
            layer, source_path, options["destination_mode"], destination,
        )
        output_path = os.path.abspath(os.path.expanduser(output_path))
        output_dir = os.path.dirname(output_path) or "."

        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception:
            QMessageBox.warning(
                self.dock,
                "Export failed",
                f"Output directory could not be created:\n{output_dir}",
            )
            return

        if os.path.abspath(source_path) == output_path:
            QMessageBox.warning(
                self.dock,
                "Export failed",
                "Source and destination are identical. Please choose a different destination.",
            )
            return

        if os.path.exists(output_path):
            overwrite = QMessageBox.question(
                self.dock,
                "Overwrite file?",
                f"The file already exists:\n{output_path}\n\nDo you want to overwrite it?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if overwrite != QMessageBox.Yes:
                return

        try:
            shutil.copy2(source_path, output_path)
        except Exception:
            traceback.print_exc()
            QMessageBox.warning(
                self.dock,
                "Export failed",
                f"Failed to export composed layer to:\n{output_path}",
            )
            return

        added_to_qgis = 0
        if options["add_to_qgis"]:
            added_to_qgis = self.add_to_qgis(output_path, layer)

        message = f"Exported composed layer to {output_path}"
        if added_to_qgis:
            message += ", added to QGIS"
        self.dock.label_index.setText(message)

    # returns True iff added_to_qgis successfully
    def add_to_qgis(self, output_path: str, layer: QgsRasterLayer) -> bool:
        layer_name = os.path.splitext(os.path.basename(output_path))[0] or layer.name()
        exported_layer = QgsRasterLayer(output_path, layer_name, "gdal")
        added_to_qgis = False

        if not exported_layer.isValid():
            QMessageBox.warning(
                self.dock,
                "Exported file invalid",
                "Export succeeded, but the exported file could not be loaded as a QGIS raster layer.",
            )
        else:
            try:
                QgsProject.instance().addMapLayer(exported_layer)
                try:
                    root = QgsProject.instance().layerTreeRoot()
                    exported_node = root.findLayer(exported_layer.id()) if root is not None else None
                    if exported_node is not None:
                        self.dock.set_item_visible(exported_node, True)
                except Exception:
                    pass
                added_to_qgis = 1
            except Exception:
                QMessageBox.warning(
                    self.dock,
                    "Add to QGIS failed",
                    "Export succeeded, but adding the exported layer to QGIS failed.",
                )

        return added_to_qgis

