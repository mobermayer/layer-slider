from typing import Dict, List, Optional
import os
import re
import shutil
import traceback

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import QApplication
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsDataProvider,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsProject,
    QgsRasterLayer,
    QgsTask,
)

from .DynamicLayerFactory import DynamicLayerFactory
from .DynamicLayerStyle import DynamicLayerStyle
from .ExportDialog import ExportDialog
from .GlobalSettings import GlobalSettings
from . import LayerRangeMapper

LAYER_SUFFIX_NUMBER_RE = re.compile(
    r"^(?P<prefix>.*?)(?P<start>\d+)(?:\s*[-–—]+\s*(?P<end>\d+))?\s*$"
)


class ComposeManager:
    def __init__(self, dock):
        self.dock = dock
        self._dynamic_nodes: Dict[str, QgsLayerTreeLayer] = {}
        self._compose_request_token = 0
        self._active_single_compose_task: Optional[QgsTask] = None
        self._active_single_compose_request: Optional[dict] = None
        self._pending_single_compose_request: Optional[dict] = None
        self._desired_single_compose_cache_key: Optional[str] = None
        self._active_batch_precalc_task: Optional[QgsTask] = None
        self._style_update_guard = 0
        self._dynamic_style_callbacks = {}
        self._project_removal_signal_connected = False
        self._layer_tree_root = None
        self._layer_tree_group_callbacks = []
        self._group_repair_scheduled = False
        self._origin_group_nodes = {}
        try:
            QgsProject.instance().layersWillBeRemoved.connect(self._on_project_layers_will_be_removed)
            self._project_removal_signal_connected = True
        except Exception:
            pass
        self._connect_layer_tree_root_signal()

    # ------------------------------------------------------------------
    # Layer compatibility
    # ------------------------------------------------------------------
    @staticmethod
    def is_dynamiclayer_compatible(layer) -> bool:
        if not isinstance(layer, QgsRasterLayer):
            return False
        if not layer.isValid():
            return False
        if layer.providerType() not in ("gdal"):
            return False
        provider = layer.dataProvider()
        if not provider:
            return False
        for band in range(1, provider.bandCount() + 1):
            if provider.colorInterpretation(band) != Qgis.RasterColorInterpretation.AlphaBand:
                return True
        return False

    # ------------------------------------------------------------------
    # Dynamic node lifecycle (one composed tree node per origin group UUID)
    # ------------------------------------------------------------------
    def _current_group_origin_uuid(self) -> Optional[str]:
        g = self.dock.current_group_node
        if not isinstance(g, QgsLayerTreeGroup):
            return None
        u = DynamicLayerFactory.layer_slider_group_uuid(g)
        return u if u else None

    def _find_canonical_dynamic_layer(self, origin_uuid: str) -> Optional[QgsRasterLayer]:
        key = str(origin_uuid).strip()
        if not key:
            return None

        def matches_origin(layer) -> bool:
            try:
                is_dynamic = layer.customProperty(DynamicLayerFactory.CUSTOM_PROPERTY, False)
                is_original = layer.id() == layer.customProperty(
                    DynamicLayerFactory.ORIGINAL_ID_PROPERTY, None
                )
                layer_origin = str(
                    layer.customProperty(DynamicLayerFactory.ORIGIN_GROUP_UUID_PROPERTY, "") or ""
                ).strip()
                return is_dynamic and is_original and layer_origin == key
            except Exception:
                return False

        node = self._dynamic_nodes.get(key)
        if node is not None:
            try:
                layer = node.layer()
                if matches_origin(layer):
                    return layer
            except Exception:
                pass

        for layer in QgsProject.instance().mapLayers().values():
            if matches_origin(layer):
                return layer
        return None

    def _find_and_cache_dynamic_tree_node(self, origin_uuid: str) -> Optional[QgsLayerTreeLayer]:
        key = str(origin_uuid).strip()
        if not key:
            return None
        layer = self._find_canonical_dynamic_layer(key)
        if layer is None:
            return None
        tnode = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        if isinstance(tnode, QgsLayerTreeLayer):
            self._dynamic_nodes[key] = tnode
            return tnode
        self._dynamic_nodes.pop(key, None)
        return None

    @property
    def dynamic_node(self) -> Optional[QgsLayerTreeLayer]:
        uid = self._current_group_origin_uuid()
        if not uid:
            return None
        node = self._dynamic_nodes.get(uid)
        if node is not None:
            try:
                node.name()
                if node.layer() is not None:
                    return node
            except Exception:
                pass
            self._dynamic_nodes.pop(uid, None)
        return self._find_and_cache_dynamic_tree_node(uid)

    def rebuild_dynamic_nodes_from_project(self):
        for layer, callback in list(self._dynamic_style_callbacks.values()):
            try:
                layer.styleChanged.disconnect(callback)
            except Exception:
                pass
        self._dynamic_style_callbacks.clear()
        self._dynamic_nodes.clear()
        self._origin_group_nodes.clear()
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        self._connect_layer_tree_root_signal(root)
        self._repair_duplicate_group_ids(root)
        for layer in project.mapLayers().values():
            try:
                if not layer.customProperty(DynamicLayerFactory.CUSTOM_PROPERTY, False):
                    continue
                if layer.id() != layer.customProperty(DynamicLayerFactory.ORIGINAL_ID_PROPERTY, None):
                    continue
                ou = layer.customProperty(DynamicLayerFactory.ORIGIN_GROUP_UUID_PROPERTY, "") or ""
                ou = str(ou).strip()
                if not ou:
                    continue
                tnode = root.findLayer(layer.id())
                if isinstance(tnode, QgsLayerTreeLayer):
                    self._dynamic_nodes[ou] = tnode
            except Exception:
                pass

        for origin_uuid, node in list(self._dynamic_nodes.items()):
            try:
                layer = node.layer()
                group = DynamicLayerFactory.find_group_by_layer_slider_uuid(root, origin_uuid)
                source = self._first_raster_in_group(group)
                if layer is None or group is None or source is None:
                    continue
                self._apply_dynamic_layer_style(
                    layer,
                    group,
                    source,
                    self._dynamic_layer_count(layer, group),
                    capture_current=True,
                )
            except Exception:
                traceback.print_exc()
            try:
                self._connect_dynamic_style_tracking(node.layer())
            except Exception:
                pass

    def _connect_layer_tree_root_signal(self, root=None):
        new_root = root or QgsProject.instance().layerTreeRoot()
        for group, callback in self._layer_tree_group_callbacks:
            try:
                group.addedChildren.disconnect(callback)
            except Exception:
                pass
        self._layer_tree_group_callbacks.clear()

        def connect_group(group):
            try:
                callback = self._on_layer_tree_children_added
                group.addedChildren.connect(callback)
                self._layer_tree_group_callbacks.append((group, callback))
            except Exception:
                pass
            for child in group.children():
                if isinstance(child, QgsLayerTreeGroup):
                    connect_group(child)

        try:
            connect_group(new_root)
            self._layer_tree_root = new_root
        except Exception:
            self._layer_tree_root = None

    def _repair_duplicate_group_ids(self, root):
        def visit(group):
            for child in group.children():
                if isinstance(child, QgsLayerTreeGroup):
                    group_uuid = DynamicLayerFactory.layer_slider_group_uuid(child)
                    if group_uuid:
                        DynamicLayerFactory.ensure_unique_layer_slider_group_uuid(
                            child,
                            root,
                            self._origin_group_nodes.get(group_uuid),
                        )
                    visit(child)

        visit(root)
        current_groups = {}

        def record(group):
            group_uuid = DynamicLayerFactory.layer_slider_group_uuid(group)
            if group_uuid:
                current_groups[group_uuid] = group
            for child in group.children():
                if isinstance(child, QgsLayerTreeGroup):
                    record(child)

        record(root)
        self._origin_group_nodes = current_groups

    def _on_layer_tree_children_added(self, parent, index_from: int, index_to: int):
        if self.dock.update_lock > 0 or self.dock._unloaded:
            return
        try:
            for index in range(index_from, index_to + 1):
                node = parent.children()[index]
                if isinstance(node, QgsLayerTreeGroup):
                    if not self._group_repair_scheduled:
                        self._group_repair_scheduled = True
                        QTimer.singleShot(0, self._repair_group_ids_after_tree_change)
                    break
        except Exception:
            pass

    def _repair_group_ids_after_tree_change(self):
        self._group_repair_scheduled = False
        if self.dock._unloaded:
            return
        try:
            self._repair_duplicate_group_ids(QgsProject.instance().layerTreeRoot())
            self._connect_layer_tree_root_signal(QgsProject.instance().layerTreeRoot())
        except Exception:
            pass

    @staticmethod
    def _is_source_raster(layer) -> bool:
        if not ComposeManager.is_dynamiclayer_compatible(layer):
            return False
        return not layer.customProperty(DynamicLayerFactory.CUSTOM_PROPERTY, False)

    @staticmethod
    def _first_raster_in_group(group) -> Optional[QgsRasterLayer]:
        if not isinstance(group, QgsLayerTreeGroup):
            return None
        for child in group.children():
            if isinstance(child, QgsLayerTreeLayer):
                layer = child.layer()
                if ComposeManager._is_source_raster(layer):
                    return layer
        return None

    @staticmethod
    def _dynamic_layer_count(layer: QgsRasterLayer, group=None) -> int:
        try:
            count = int(layer.customProperty(DynamicLayerFactory.LAYER_COUNT_PROPERTY, 0))
            if count > 0:
                return count
        except Exception:
            pass
        if isinstance(group, QgsLayerTreeGroup):
            count = 0
            for child in group.children():
                if isinstance(child, QgsLayerTreeLayer) and ComposeManager._is_source_raster(child.layer()):
                    count += 1
            return max(1, count)
        return 1

    def _connect_dynamic_style_tracking(self, layer):
        if layer is None:
            return
        try:
            layer_id = layer.id()
            if layer_id in self._dynamic_style_callbacks:
                return

            def callback(*_args):
                self._on_dynamic_layer_style_changed(layer_id)

            layer.styleChanged.connect(callback)
            self._dynamic_style_callbacks[layer_id] = (layer, callback)
        except Exception:
            pass

    def _disconnect_dynamic_style_tracking(self, layer):
        if layer is None:
            return
        try:
            layer_id = layer.id()
        except Exception:
            return
        record = self._dynamic_style_callbacks.pop(layer_id, None)
        if record is not None:
            try:
                record[0].styleChanged.disconnect(record[1])
            except Exception:
                pass

    def _on_project_layers_will_be_removed(self, layer_ids):
        if isinstance(layer_ids, str):
            layer_ids = [layer_ids]
        for layer_id in layer_ids or []:
            try:
                layer = QgsProject.instance().mapLayer(str(layer_id))
                self._save_dynamic_style_before_removal(layer)
                self._disconnect_dynamic_style_tracking(layer)
            except Exception:
                pass

    def disconnect_project_signals(self):
        if self._project_removal_signal_connected:
            try:
                QgsProject.instance().layersWillBeRemoved.disconnect(self._on_project_layers_will_be_removed)
            except Exception:
                pass
            self._project_removal_signal_connected = False
        for group, callback in self._layer_tree_group_callbacks:
            try:
                group.addedChildren.disconnect(callback)
            except Exception:
                pass
        self._layer_tree_group_callbacks.clear()
        self._layer_tree_root = None

    def _on_dynamic_layer_style_changed(self, layer_id: str):
        if self._style_update_guard:
            return
        try:
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer is None or not layer.customProperty(DynamicLayerFactory.CUSTOM_PROPERTY, False):
                return
            if layer.id() != layer.customProperty(DynamicLayerFactory.ORIGINAL_ID_PROPERTY, None):
                return
            root = QgsProject.instance().layerTreeRoot()
            self._repair_duplicate_group_ids(root)
            origin_uuid = str(layer.customProperty(DynamicLayerFactory.ORIGIN_GROUP_UUID_PROPERTY, "") or "").strip()
            group = DynamicLayerFactory.find_group_by_layer_slider_uuid(root, origin_uuid)
            if group is not None:
                DynamicLayerStyle.capture_manual_changes(group, layer, save_snapshot=True)
        except Exception:
            traceback.print_exc()

    def _apply_dynamic_layer_style(
        self,
        layer: QgsRasterLayer,
        group: QgsLayerTreeGroup,
        source_layer: QgsRasterLayer,
        num_layers: int,
        capture_current: bool,
        restore_snapshot: bool = False,
    ):
        self._style_update_guard += 1
        try:
            DynamicLayerStyle.apply(
                group,
                layer,
                source_layer,
                contrast_offset=self._compute_compose_contrast(num_layers),
                canvas_extent=self.dock.iface.mapCanvas().extent(),
                restore_snapshot=restore_snapshot,
                capture_current=capture_current,
            )
        finally:
            self._style_update_guard -= 1

    def reset_dynamic_layer_style(self, layer: QgsRasterLayer) -> bool:
        if not layer or not layer.customProperty(DynamicLayerFactory.CUSTOM_PROPERTY, False):
            return False
        if layer.id() != layer.customProperty(DynamicLayerFactory.ORIGINAL_ID_PROPERTY, None):
            return False
        origin_uuid = str(layer.customProperty(DynamicLayerFactory.ORIGIN_GROUP_UUID_PROPERTY, "") or "").strip()
        root = QgsProject.instance().layerTreeRoot()
        self._repair_duplicate_group_ids(root)
        group = DynamicLayerFactory.find_group_by_layer_slider_uuid(root, origin_uuid)
        source = self._first_raster_in_group(group)
        if group is None or source is None:
            return False
        DynamicLayerStyle.clear_overrides(group)
        self._apply_dynamic_layer_style(
            layer,
            group,
            source,
            self._dynamic_layer_count(layer, group),
            capture_current=False,
        )
        self.dock.iface.layerTreeView().refreshLayerSymbology(layer.id())
        self.dock.iface.mapCanvas().refresh()
        return True

    def dynamic_node_defined(self) -> bool:
        return self.dynamic_node is not None

    def _remove_dynamic_node_for_origin_uuid_impl(self, origin_uuid: str) -> bool:
        key = str(origin_uuid).strip()
        if not key:
            return False
        removed = False
        node = self._dynamic_nodes.pop(key, None)
        layer_id = None
        if node is not None:
            try:
                if hasattr(node, "layer") and node.layer() is not None:
                    layer_id = node.layer().id()
            except Exception:
                pass
        if layer_id is None:
            layer = self._find_canonical_dynamic_layer(key)
            if layer is not None:
                layer_id = layer.id()
        if layer_id:
            layer = QgsProject.instance().mapLayer(layer_id)
            self._save_dynamic_style_before_removal(layer)
            self._disconnect_dynamic_style_tracking(layer)
            try:
                QgsProject.instance().removeMapLayer(layer_id)
                removed = True
            except Exception:
                try:
                    if node is not None:
                        parent = node.parent()
                        if parent:
                            parent.removeChildNode(node)
                            removed = True
                except Exception:
                    pass
        return removed

    def _save_dynamic_style_before_removal(self, layer):
        try:
            if layer is None or not layer.customProperty(DynamicLayerFactory.CUSTOM_PROPERTY, False):
                return
            if layer.id() != layer.customProperty(DynamicLayerFactory.ORIGINAL_ID_PROPERTY, None):
                return
            origin_uuid = str(layer.customProperty(DynamicLayerFactory.ORIGIN_GROUP_UUID_PROPERTY, "") or "").strip()
            root = QgsProject.instance().layerTreeRoot()
            self._repair_duplicate_group_ids(root)
            group = DynamicLayerFactory.find_group_by_layer_slider_uuid(root, origin_uuid)
            if group is not None:
                DynamicLayerStyle.capture_manual_changes(group, layer, save_snapshot=True)
        except Exception:
            traceback.print_exc()

    def remove_dynamic_node_for_origin_uuid(self, origin_uuid: Optional[str]) -> bool:
        if not origin_uuid or not str(origin_uuid).strip():
            return False
        self.dock.update_lock += 1
        try:
            return self._remove_dynamic_node_for_origin_uuid_impl(str(origin_uuid).strip())
        finally:
            self.dock.update_lock -= 1

    def remove_dynamic_node(self):
        uid = self._current_group_origin_uuid()
        if uid:
            self.remove_dynamic_node_for_origin_uuid(uid)

    def _origin_has_canonical_dynamic_layer(self, origin_uuid: str) -> bool:
        return self._find_canonical_dynamic_layer(origin_uuid) is not None

    def remove_dynamics_for_manual_visibility_on_node(self, node) -> bool:
        """Remove composed output for the innermost ancestor group (of node) that has one."""
        if node is None:
            return False
        n = node if isinstance(node, QgsLayerTreeGroup) else node.parent()
        self.dock.update_lock += 1
        try:
            while n is not None:
                if isinstance(n, QgsLayerTreeGroup):
                    u = DynamicLayerFactory.layer_slider_group_uuid(n)
                    if u and self._origin_has_canonical_dynamic_layer(u):
                        return self._remove_dynamic_node_for_origin_uuid_impl(u)
                n = n.parent()
            return False
        finally:
            self.dock.update_lock -= 1

    def detach_dynamic_node(self):
        for layer, _callback in list(self._dynamic_style_callbacks.values()):
            self._save_dynamic_style_before_removal(layer)
            self._disconnect_dynamic_style_tracking(layer)
        self._dynamic_nodes.clear()

    def remove_canonical_dynamics_with_missing_origin(self):
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        layers_to_remove = []
        for layer in project.mapLayers().values():
            try:
                if not layer.customProperty(DynamicLayerFactory.CUSTOM_PROPERTY, False):
                    continue
                original_id = layer.customProperty(DynamicLayerFactory.ORIGINAL_ID_PROPERTY, None)
                if layer.id() != original_id:
                    continue
                ouuid = layer.customProperty(DynamicLayerFactory.ORIGIN_GROUP_UUID_PROPERTY, "") or ""
                if not str(ouuid).strip():
                    layers_to_remove.append(layer)
                    continue
                if DynamicLayerFactory.find_group_by_layer_slider_uuid(root, ouuid) is None:
                    layers_to_remove.append(layer)
            except Exception:
                continue

        remove_ids = {lyr.id() for lyr in layers_to_remove}
        for uid, nd in list(self._dynamic_nodes.items()):
            try:
                lyr = nd.layer()
                if lyr is not None and lyr.id() in remove_ids:
                    self._dynamic_nodes.pop(uid, None)
            except Exception:
                self._dynamic_nodes.pop(uid, None)

        for layer in layers_to_remove:
            try:
                self._save_dynamic_style_before_removal(layer)
                self._disconnect_dynamic_style_tracking(layer)
                project.removeMapLayer(layer.id())
            except Exception:
                pass

    def remove_stale_dynamic_layers(self):
        project = QgsProject.instance()
        layers_to_remove = []
        for layer in project.mapLayers().values():
            is_dynamic = layer.customProperty(DynamicLayerFactory.CUSTOM_PROPERTY, False)
            original_id = layer.customProperty(DynamicLayerFactory.ORIGINAL_ID_PROPERTY, None)
            if is_dynamic and layer.id() == original_id:
                layers_to_remove.append(layer)
        for layer in layers_to_remove:
            self._save_dynamic_style_before_removal(layer)
            self._disconnect_dynamic_style_tracking(layer)
            project.removeMapLayer(layer.id())

    def remove_all_canonical_dynamic_layers(self):
        """Remove every canonical plugin dynamic layer (duplicates preserved)."""
        self.detach_dynamic_node()
        self.remove_stale_dynamic_layers()

    def _insert_dynamic_layer_from_cached_path(
        self,
        cached_path: str,
        name: str,
        num_layers: int,
        source_layer: Optional[QgsRasterLayer] = None,
    ):
        group = self.dock.current_group_node
        origin_uid = None
        if isinstance(group, QgsLayerTreeGroup):
            root = QgsProject.instance().layerTreeRoot()
            current_uuid = DynamicLayerFactory.layer_slider_group_uuid(group)
            origin_uid = DynamicLayerFactory.ensure_unique_layer_slider_group_uuid(
                group,
                root,
                self._origin_group_nodes.get(current_uuid),
            )
            self._origin_group_nodes[origin_uid] = group
        source_layer = self._first_raster_in_group(group) or source_layer

        dynamic_layer = self._find_canonical_dynamic_layer(origin_uid) if origin_uid else None
        is_new_layer = dynamic_layer is None
        if is_new_layer:
            dynamic_layer = DynamicLayerFactory.layer_from_cached_path(cached_path, name)
        else:
            if isinstance(group, QgsLayerTreeGroup):
                DynamicLayerStyle.capture_manual_changes(group, dynamic_layer, save_snapshot=True)
            try:
                options = QgsDataProvider.ProviderOptions()
            except Exception:
                options = None
            self._style_update_guard += 1
            try:
                if options is not None:
                    try:
                        dynamic_layer.setDataSource(cached_path, name, "gdal", options, False)
                    except TypeError:
                        dynamic_layer.setDataSource(cached_path, name, "gdal", False)
                else:
                    dynamic_layer.setDataSource(cached_path, name, "gdal", False)
                if not dynamic_layer.isValid():
                    raise RuntimeError(f"Could not update composed raster source: {cached_path}")
            except Exception:
                traceback.print_exc()
                self._save_dynamic_style_before_removal(dynamic_layer)
                self._disconnect_dynamic_style_tracking(dynamic_layer)
                QgsProject.instance().removeMapLayer(dynamic_layer.id())
                dynamic_layer = DynamicLayerFactory.layer_from_cached_path(cached_path, name)
                is_new_layer = True
            finally:
                self._style_update_guard -= 1

        dynamic_layer.setName(name)
        dynamic_layer.setCustomProperty(DynamicLayerFactory.CUSTOM_PROPERTY, True)
        dynamic_layer.setCustomProperty(DynamicLayerFactory.ORIGINAL_ID_PROPERTY, dynamic_layer.id())
        dynamic_layer.setCustomProperty(DynamicLayerFactory.ORIGIN_GROUP_UUID_PROPERTY, origin_uid or "")
        dynamic_layer.setCustomProperty(DynamicLayerFactory.LAYER_COUNT_PROPERTY, int(num_layers))

        parent_group = QgsProject.instance().layerTreeRoot()
        index = 0
        if group:
            parent = group.parent()
            if isinstance(parent, QgsLayerTreeGroup):
                parent_group = parent
                try:
                    index = parent.children().index(group)
                except Exception:
                    index = 0

        self.dock.update_lock += 1
        try:
            if is_new_layer:
                QgsProject.instance().addMapLayer(dynamic_layer, addToLegend=False)
            root = QgsProject.instance().layerTreeRoot()
            node_to_insert = root.findLayer(dynamic_layer.id())
            if node_to_insert is None:
                node_to_insert = QgsLayerTreeLayer(dynamic_layer)
                node_to_insert.setExpanded(False)
                try:
                    parent_group.insertChildNode(index, node_to_insert)
                except Exception:
                    parent_group.addChildNode(node_to_insert)
            else:
                node_parent = node_to_insert.parent()
                node_index = node_parent.children().index(node_to_insert) if node_parent else -1
                group_index = (
                    parent_group.children().index(group)
                    if isinstance(group, QgsLayerTreeGroup) and group is not parent_group
                    else -1
                )
                needs_move = node_parent != parent_group
                if not isinstance(group, QgsLayerTreeGroup) or group is parent_group:
                    needs_move = needs_move or node_index != 0
                else:
                    needs_move = needs_move or node_index != group_index - 1
                if needs_move:
                    moved_node = node_to_insert.clone()
                    if node_parent is not None:
                        node_parent.removeChildNode(node_to_insert)
                    if isinstance(group, QgsLayerTreeGroup) and group is not parent_group:
                        index = parent_group.children().index(group)
                    else:
                        index = 0
                    try:
                        parent_group.insertChildNode(index, moved_node)
                    except Exception:
                        parent_group.addChildNode(moved_node)
                    node_to_insert = moved_node
            node_to_insert.setItemVisibilityChecked(True)
            if origin_uid and isinstance(group, QgsLayerTreeGroup) and source_layer is not None:
                self._apply_dynamic_layer_style(
                    dynamic_layer,
                    group,
                    source_layer,
                    num_layers,
                    capture_current=False,
                    restore_snapshot=is_new_layer,
                )
            dynamic_layer.setCustomProperty(DynamicLayerFactory.CUSTOM_PROPERTY, True)
            dynamic_layer.setCustomProperty(DynamicLayerFactory.ORIGINAL_ID_PROPERTY, dynamic_layer.id())
            dynamic_layer.setCustomProperty(DynamicLayerFactory.ORIGIN_GROUP_UUID_PROPERTY, origin_uid or "")
            dynamic_layer.setCustomProperty(DynamicLayerFactory.LAYER_COUNT_PROPERTY, int(num_layers))
            self._connect_dynamic_style_tracking(dynamic_layer)
            self.dock.iface.layerTreeView().refreshLayerSymbology(dynamic_layer.id())
            self.dock.iface.mapCanvas().refresh()
            if origin_uid:
                self._dynamic_nodes[origin_uid] = node_to_insert
        finally:
            self.dock.update_lock -= 1

    # ------------------------------------------------------------------
    # Compose naming helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_layer_suffix_range(layer_name: str) -> Optional[tuple]:
        match = LAYER_SUFFIX_NUMBER_RE.match(layer_name.strip())
        if not match:
            return None
        prefix = match.group("prefix")
        start_str = match.group("start")
        end_str = match.group("end") or start_str
        width = max(len(start_str), len(end_str))
        return prefix, int(start_str), int(end_str), width

    @staticmethod
    def _compose_layer_name(raster_layers: List[QgsRasterLayer], operation: str) -> str:
        n = len(raster_layers)
        first_name = raster_layers[0].name()
        last_name = raster_layers[-1].name()
        operation_name = GlobalSettings.getComposeOperationShortName(operation).upper()

        first_parsed = ComposeManager._parse_layer_suffix_range(first_name)
        last_parsed = ComposeManager._parse_layer_suffix_range(last_name)
        if first_parsed and last_parsed:
            first_prefix, first_start, first_end, first_width = first_parsed
            last_prefix, last_start, last_end, last_width = last_parsed
            if first_prefix == last_prefix:
                range_min = min(first_start, first_end, last_start, last_end)
                range_max = max(first_start, first_end, last_start, last_end)
                width = max(first_width, last_width)
                prefix = first_prefix.rstrip(" _-–—")
                min_label = str(range_min).zfill(width)
                max_label = str(range_max).zfill(width)
                if prefix:
                    return f"{prefix}_{min_label}-{max_label} {operation_name}_{n}"

        return f"{first_name} — {last_name} {operation_name}_{n}"

    @staticmethod
    def sanitize_export_filename(value: str) -> str:
        filename = re.sub(r'[\\/:*?"<>|]+', "_", value).strip()
        filename = re.sub(r"\s+", " ", filename).strip(" .")
        return filename or "composed-layer"

    @staticmethod
    def _compute_compose_contrast(num_layers: int) -> int:
        mode = GlobalSettings.getComposeContrastMode()
        if mode == "constant":
            contrast = GlobalSettings.getComposeContrastValue()
        else:
            n = GlobalSettings.getComposeContrastN()
            contrast = n * max(0, num_layers - 1)
        return max(-100, min(100, contrast))

    # ------------------------------------------------------------------
    # Single compose request lifecycle
    # ------------------------------------------------------------------
    def clear_desired_single_compose_request(self):
        self._pending_single_compose_request = None
        self._desired_single_compose_cache_key = None

    def invalidate_single_compose_request(self, cancel_task: bool = False) -> int:
        self._compose_request_token += 1
        self.clear_desired_single_compose_request()
        task = self._active_single_compose_task
        if cancel_task and task is not None and task.isActive():
            try:
                task.cancel()
            except Exception:
                pass
            self._active_single_compose_task = None
            self._active_single_compose_request = None
        return self._compose_request_token

    def _can_apply_single_compose_result(self, request: dict) -> bool:
        if self.dock._unloaded:
            return False
        if request.get("request_token") != self._compose_request_token:
            return False
        if not self.dock.chk_avgrasters.isChecked():
            return False
        if not self.dock.group_layers:
            return False
        desired_key = self._desired_single_compose_cache_key
        if not desired_key:
            return False
        return request.get("cache_key") == desired_key

    def _apply_cached_single_compose_result(self, request: dict):
        if not self._can_apply_single_compose_result(request):
            return
        try:
            self._insert_dynamic_layer_from_cached_path(
                request["cached_path"],
                request["name"],
                request["num_layers"],
                request["layers"][0] if request.get("layers") else None,
            )
            self.dock._update_label_only(request["slider_idx"])
            self.dock.update_btn_reset_text()
        except Exception:
            traceback.print_exc()
            self.dock.label_index.setText("Composition failed")

    def _start_single_compose_task(self, request: dict):
        def _run_compose(task: QgsTask):
            cached_path = DynamicLayerFactory.compute_or_get_cached_path(
                request["layers"],
                operation=request["operation"],
            )
            task.setProgress(100.0)
            return {**request, "cached_path": cached_path}

        task_holder = {"task": None}

        def _on_compose_finished(exception: Exception | None, result=None):
            if self._active_single_compose_task is task_holder["task"]:
                self._active_single_compose_task = None
                self._active_single_compose_request = None

            if exception is not None:
                traceback.print_exception(type(exception), exception, exception.__traceback__)
                if self._can_apply_single_compose_result(request):
                    self.dock.label_index.setText("Composition failed")
            elif result:
                self._apply_cached_single_compose_result(result)

            pending = self._pending_single_compose_request
            self._pending_single_compose_request = None
            if pending:
                self.queue_single_compose_request(
                    pending["layers"],
                    pending["slider_idx"],
                    operation=pending["operation"],
                    request_token=pending["request_token"],
                )

        task = QgsTask.fromFunction(
            f"Layer Slider compose ({request['name']})",
            _run_compose,
            on_finished=_on_compose_finished,
        )
        try:
            task.setDependentLayers(request["layers"])
        except Exception:
            pass
        task_holder["task"] = task
        self._active_single_compose_task = task
        self._active_single_compose_request = request
        QgsApplication.taskManager().addTask(task)

    def queue_single_compose_request(
        self,
        raster_layers: List[QgsRasterLayer],
        slider_idx: int,
        operation: Optional[str] = None,
        request_token: Optional[int] = None,
    ):
        if not raster_layers:
            return

        compose_operation = operation or GlobalSettings.getComposeOperation()
        token = request_token if request_token is not None else self._compose_request_token
        compose_name = self._compose_layer_name(raster_layers, compose_operation)
        cache_key = DynamicLayerFactory.cache_key_for_layers(raster_layers, operation=compose_operation)
        cached_path = DynamicLayerFactory.cached_path_for_key(cache_key)
        request = {
            "layers": raster_layers,
            "slider_idx": slider_idx,
            "operation": compose_operation,
            "request_token": token,
            "cache_key": cache_key,
            "cached_path": cached_path,
            "name": compose_name,
            "num_layers": len(raster_layers),
        }
        self._desired_single_compose_cache_key = cache_key

        if os.path.exists(cached_path):
            try:
                os.utime(cached_path, None)
            except Exception:
                pass
            self._apply_cached_single_compose_result(request)
            return

        active_task = self._active_single_compose_task
        if active_task is not None and active_task.isActive():
            active_request = self._active_single_compose_request
            if active_request and active_request.get("cache_key") == cache_key:
                return
            self._pending_single_compose_request = request
            return

        self._start_single_compose_task(request)

    # ------------------------------------------------------------------
    # Batch precalc pipeline
    # ------------------------------------------------------------------
    def _get_range_params(self):
        return {
            "num_avgrasters": self.dock.num_avgrasters.value(),
            "distinct": self.dock.chk_avgdistinct.isChecked(),
            "offset": self.dock.num_avgoffset.value(),
        }

    def _current_compose_candidate_nodes(self) -> List[QgsLayerTreeLayer]:
        if not isinstance(self.dock.current_group_node, QgsLayerTreeGroup):
            return []
        if not self.dock._node_belongs_to_root(self.dock.current_group_node):
            return []
        children = self.dock.current_group_node.children()

        nodes: List[QgsLayerTreeLayer] = []
        for child in children:
            if not isinstance(child, QgsLayerTreeLayer):
                continue
            if self.is_dynamiclayer_compatible(child.layer()):
                nodes.append(child)
        return nodes

    def collect_precalc_entries(self):
        candidate_nodes = self._current_compose_candidate_nodes()
        if not candidate_nodes:
            return []

        params = self._get_range_params()
        ranges = LayerRangeMapper.layer_ranges_for_count(
            len(candidate_nodes), avgrasters_enabled=True, **params,
        )
        if not ranges:
            return []

        operation = GlobalSettings.getComposeOperation()
        entries = []
        seen_cache_keys = set()

        for layer_range in ranges:
            if len(layer_range) <= 1:
                continue
            nodes = [candidate_nodes[i] for i in layer_range]
            layers = [node.layer() for node in nodes if isinstance(node, QgsLayerTreeLayer)]
            raster_layers = [layer for layer in layers if isinstance(layer, QgsRasterLayer)]
            if len(raster_layers) != len(nodes):
                continue

            cache_key = DynamicLayerFactory.cache_key_for_layers(raster_layers, operation=operation)
            if cache_key in seen_cache_keys:
                continue
            seen_cache_keys.add(cache_key)
            cached_path = DynamicLayerFactory.cached_path_for_key(cache_key)

            entries.append({
                "layers": raster_layers,
                "name": self._compose_layer_name(raster_layers, operation),
                "cache_key": cache_key,
                "cached_path": cached_path,
                "cached_exists": os.path.exists(cached_path),
            })

        return entries

    def collect_precalc_requests(self, entries=None):
        source_entries = entries if entries is not None else self.collect_precalc_entries()
        requests = []
        for entry in source_entries:
            if entry.get("cached_exists"):
                try:
                    os.utime(entry["cached_path"], None)
                except Exception:
                    pass
                continue
            requests.append(entry)
        return requests

    def build_precalc_export_entries(self, output_directory: str):
        entries = self.collect_precalc_entries()
        used_names = set()
        export_entries = []

        for entry in entries:
            base_name = self.sanitize_export_filename(entry["name"])
            candidate = base_name
            suffix = 2
            while candidate.lower() in used_names:
                candidate = f"{base_name}_{suffix}"
                suffix += 1
            used_names.add(candidate.lower())

            export_entries.append({
                **entry,
                "export_name": candidate,
                "output_path": os.path.join(output_directory, f"{candidate}.tif"),
            })
        return export_entries

    def _export_group_base_name(self, operation: str) -> str:
        operation_name = GlobalSettings.getComposeOperationShortName(operation).upper()
        n = self.dock.num_avgrasters.value()
        if isinstance(self.dock.current_group_node, QgsLayerTreeGroup):
            original_group_name = self.dock.current_group_node.name() or "Project Root"
        else:
            original_group_name = "Project Root"
        return f"{original_group_name} {operation_name}_{n}"

    def _create_precalc_export_group(self, operation: str) -> QgsLayerTreeGroup:
        parent_group = QgsProject.instance().layerTreeRoot()
        insert_index = len(parent_group.children())

        current_group = self.dock.current_group_node
        if isinstance(current_group, QgsLayerTreeGroup):
            parent = current_group.parent()
            if isinstance(parent, QgsLayerTreeGroup):
                parent_group = parent
                try:
                    insert_index = parent_group.children().index(current_group)
                except Exception:
                    insert_index = len(parent_group.children())

                if self.dynamic_node_defined():
                    try:
                        dynamic_parent = self.dynamic_node.parent()
                        if dynamic_parent == parent_group:
                            dynamic_index = parent_group.children().index(self.dynamic_node)
                            if dynamic_index <= insert_index:
                                insert_index = dynamic_index
                    except Exception:
                        pass

        insert_index = max(0, min(insert_index, len(parent_group.children())))

        base_name = self._export_group_base_name(operation)
        existing_names = {
            child.name() for child in parent_group.children()
            if isinstance(child, QgsLayerTreeGroup)
        }

        name = base_name
        suffix = 2
        while name in existing_names:
            name = f"{base_name} {suffix}"
            suffix += 1
        try:
            return parent_group.insertGroup(insert_index, name)
        except Exception:
            return parent_group.addGroup(name)

    def show_precalc_export_dialog(self):
        dialog = ExportDialog(
            self.dock,
            window_title="Export pre-calculated layers",
            allow_file_destination=False,
            initial_destination_mode=ExportDialog.DESTINATION_DIRECTORY,
            initial_destination_path=GlobalSettings.getPrecalcExportDirectory(),
            initial_add_to_qgis=GlobalSettings.getExportAddToQgis(),
            persist_to_precalc_settings=True,
        )
        accepted = dialog.exec()
        if not accepted:
            return None
        return {
            "output_directory": dialog.selected_directory(),
            "add_to_qgis": dialog.add_to_qgis(),
        }

    def start_batch_precalc_task(
        self,
        entries,
        operation: str,
        mode_label: str,
        export_directory: Optional[str] = None,
        add_to_qgis: bool = False,
    ):
        total = len(entries)
        if total == 0:
            self.update_precalc_button_state()
            return

        def _run_precalc(task: QgsTask):
            completed = 0
            failures = 0
            exported_layers = []
            if export_directory:
                try:
                    os.makedirs(export_directory, exist_ok=True)
                except Exception:
                    traceback.print_exc()
                    return {
                        "completed": 0, "failed": total, "total": total,
                        "canceled": task.isCanceled(), "exported_layers": [],
                    }
            for idx, entry in enumerate(entries):
                if task.isCanceled():
                    break
                try:
                    cached_path = entry["cached_path"]
                    if not os.path.exists(cached_path):
                        cached_path = DynamicLayerFactory.compute_or_get_cached_path(
                            entry["layers"], operation=operation,
                        )
                    else:
                        try:
                            os.utime(cached_path, None)
                        except Exception:
                            pass

                    if export_directory:
                        shutil.copy2(cached_path, entry["output_path"])
                        exported_layers.append({
                            "path": entry["output_path"],
                            "name": entry.get("export_name") or entry["name"],
                        })
                    completed += 1
                except Exception:
                    failures += 1
                    traceback.print_exc()
                task.setProgress(((idx + 1) * 100.0) / total)
            return {
                "completed": completed, "failed": failures, "total": total,
                "canceled": task.isCanceled(), "exported_layers": exported_layers,
            }

        task_holder = {"task": None}

        def _on_precalc_finished(exception: Exception | None, result=None):
            if self._active_batch_precalc_task is task_holder["task"]:
                self._active_batch_precalc_task = None

            if self.dock._unloaded:
                return

            if exception is not None:
                traceback.print_exception(type(exception), exception, exception.__traceback__)
                self.dock.label_index.setText(f"{mode_label} failed")
                self.update_precalc_button_state()
                return

            if not result:
                self.update_precalc_button_state()
                return

            if result.get("canceled"):
                self.dock.label_index.setText(f"{mode_label} canceled")
                self.update_precalc_button_state()
                return

            added_count = 0
            failed_to_add = 0
            if export_directory and add_to_qgis:
                export_group = None
                if result.get("exported_layers"):
                    try:
                        export_group = self._create_precalc_export_group(operation)
                        self.dock.set_item_visible(export_group, True)
                    except Exception:
                        export_group = None
                for exported in result.get("exported_layers", []):
                    layer = QgsRasterLayer(exported["path"], exported["name"], "gdal")
                    if not layer.isValid():
                        failed_to_add += 1
                        continue
                    try:
                        if export_group is not None:
                            QgsProject.instance().addMapLayer(layer, addToLegend=False)
                            export_group.addLayer(layer)
                        else:
                            QgsProject.instance().addMapLayer(layer)
                        added_count += 1
                    except Exception:
                        failed_to_add += 1

            if export_directory:
                message = f"Exported {result.get('completed', 0)}/{result.get('total', 0)} ranges"
                if add_to_qgis:
                    message += f", added {added_count} to QGIS"
                    if failed_to_add:
                        message += f" ({failed_to_add} failed)"
                self.dock.label_index.setText(message)
            else:
                self.dock.label_index.setText(
                    f"Pre-calculated {result.get('completed', 0)}/{result.get('total', 0)} ranges"
                )
            self.update_precalc_button_state()

        task_title = f"Layer Slider {mode_label.lower()} ({total} ranges)"
        task = QgsTask.fromFunction(task_title, _run_precalc, on_finished=_on_precalc_finished)
        try:
            dependent_layers = []
            for entry in entries:
                dependent_layers.extend(entry["layers"])
            task.setDependentLayers(dependent_layers)
        except Exception:
            pass

        def _on_precalc_progress(progress: float):
            if self._active_batch_precalc_task is task_holder["task"]:
                self.dock.btn_precalc_all.setToolTip(
                    f"{mode_label} {total} ranges ({int(progress)}%)... click to cancel"
                )

        task.progressChanged.connect(_on_precalc_progress)
        task_holder["task"] = task
        self._active_batch_precalc_task = task
        self.dock.btn_precalc_all.setToolTip(f"{mode_label} {total} ranges... click to cancel")
        QgsApplication.taskManager().addTask(task)

    def update_precalc_button_state(self):
        if not hasattr(self.dock, "btn_precalc_all"):
            return

        running_task = self._active_batch_precalc_task
        if running_task is not None and running_task.isActive():
            self.dock.btn_precalc_all.setEnabled(True)
            self.dock.btn_precalc_all.setToolTip("Cancel pre-calculation")
            return

        entries = self.collect_precalc_entries()
        request_count = len(self.collect_precalc_requests(entries))
        self.dock.btn_precalc_all.setEnabled(len(entries) > 0)

        precalc_key = getattr(self.dock, "precalc_shortcut", "")
        export_key = getattr(self.dock, "export_shortcut", "")
        export_line = f"\n[Shift+click] Export to directory{export_key}"

        if request_count > 0:
            self.dock.btn_precalc_all.setToolTip(
                f"Pre-calculate all composed ranges for current group/settings{precalc_key}{export_line}"
            )
        elif entries:
            self.dock.btn_precalc_all.setToolTip(
                f"All composed ranges are cached{export_line}"
            )
        else:
            self.dock.btn_precalc_all.setToolTip(
                "No composed ranges to pre-calculate for current group/settings"
            )

    def on_precalc_all_clicked(self):
        if self._active_batch_precalc_task is not None and self._active_batch_precalc_task.isActive():
            try:
                self._active_batch_precalc_task.cancel()
            except Exception:
                pass
            self.dock.btn_precalc_all.setToolTip("Cancelling pre-calculation...")
            return

        if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._on_precalc_all_shift_clicked()
            return

        requests = self.collect_precalc_requests()
        if not requests:
            self.update_precalc_button_state()
            return

        operation = GlobalSettings.getComposeOperation()
        self.start_batch_precalc_task(requests, operation=operation, mode_label="Pre-calculating")

    def _on_precalc_all_shift_clicked(self):
        options = self.show_precalc_export_dialog()
        if not options:
            self.update_precalc_button_state()
            return

        entries = self.build_precalc_export_entries(options["output_directory"])
        if not entries:
            self.update_precalc_button_state()
            return

        operation = GlobalSettings.getComposeOperation()
        self.start_batch_precalc_task(
            entries,
            operation=operation,
            mode_label="Exporting",
            export_directory=options["output_directory"],
            add_to_qgis=bool(options["add_to_qgis"]),
        )

    # ------------------------------------------------------------------
    # Task cancellation
    # ------------------------------------------------------------------
    def cancel_background_tasks(self):
        self.invalidate_single_compose_request(cancel_task=True)
        task = self._active_batch_precalc_task
        if task is not None and task.isActive():
            try:
                task.cancel()
            except Exception:
                pass
        self._active_batch_precalc_task = None
        self.update_precalc_button_state()
