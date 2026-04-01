from typing import List, Optional
import os
import re
import shutil
import traceback

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QApplication
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsBrightnessContrastFilter,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsProject,
    QgsRasterLayer,
    QgsTask,
)

from .DynamicLayerFactory import DynamicLayerFactory
from .ExportDialog import ExportDialog
from .GlobalSettings import GlobalSettings
from . import LayerRangeMapper

LAYER_SUFFIX_NUMBER_RE = re.compile(
    r"^(?P<prefix>.*?)(?P<start>\d+)(?:\s*[-–—]+\s*(?P<end>\d+))?\s*$"
)


class ComposeManager:
    def __init__(self, dock):
        self.dock = dock
        self.dynamic_node = None
        self._compose_request_token = 0
        self._active_single_compose_task: Optional[QgsTask] = None
        self._active_single_compose_request: Optional[dict] = None
        self._pending_single_compose_request: Optional[dict] = None
        self._desired_single_compose_cache_key: Optional[str] = None
        self._active_batch_precalc_task: Optional[QgsTask] = None

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
    # Dynamic node lifecycle
    # ------------------------------------------------------------------
    def dynamic_node_defined(self) -> bool:
        try:
            if self.dynamic_node is None:
                return False
            self.dynamic_node.name()
            return True
        except Exception:
            return False

    def remove_dynamic_node(self):
        if not self.dynamic_node_defined():
            return
        self.dock.update_lock += 1
        try:
            layer_id = None
            if hasattr(self.dynamic_node, "layer") and self.dynamic_node.layer() is not None:
                layer_id = self.dynamic_node.layer().id()
            if layer_id:
                try:
                    QgsProject.instance().removeMapLayer(layer_id)
                except Exception:
                    try:
                        parent = self.dynamic_node.parent()
                        if parent:
                            parent.removeChildNode(self.dynamic_node)
                    except Exception:
                        pass
            else:
                try:
                    parent = self.dynamic_node.parent()
                    if parent:
                        parent.removeChildNode(self.dynamic_node)
                except Exception:
                    pass
        finally:
            self.dynamic_node = None
            self.dock.update_lock -= 1

    def remove_stale_dynamic_layers(self):
        project = QgsProject.instance()
        layers_to_remove = []
        for layer in project.mapLayers().values():
            is_dynamic = layer.customProperty(DynamicLayerFactory.CUSTOM_PROPERTY, False)
            original_id = layer.customProperty(DynamicLayerFactory.ORIGINAL_ID_PROPERTY, None)
            if is_dynamic and layer.id() == original_id:
                layers_to_remove.append(layer)
        for layer in layers_to_remove:
            project.removeMapLayer(layer.id())

    def _insert_dynamic_layer_from_cached_path(self, cached_path: str, name: str, num_layers: int):
        dynamic_layer = DynamicLayerFactory.layer_from_cached_path(cached_path, name)
        self._insert_dynamic_layer(dynamic_layer, num_layers)

    def _insert_dynamic_layer(self, dynamic_layer: QgsRasterLayer, num_layers: int):
        self.remove_dynamic_node()

        contrast_filter = QgsBrightnessContrastFilter()
        contrast_filter.setContrast(self._compute_compose_contrast(num_layers))
        dynamic_layer.pipe().set(contrast_filter)

        node = QgsLayerTreeLayer(dynamic_layer)
        node.setExpanded(False)

        parent_group = QgsProject.instance().layerTreeRoot()
        index = 0
        if self.dock.current_group_node:
            parent = self.dock.current_group_node.parent()
            if isinstance(parent, QgsLayerTreeGroup):
                parent_group = parent
                try:
                    index = parent.children().index(self.dock.current_group_node)
                except Exception:
                    index = 0

        self.dock.update_lock += 1
        try:
            QgsProject.instance().addMapLayer(dynamic_layer, addToLegend=False)

            root = QgsProject.instance().layerTreeRoot()
            canonical = root.findLayer(dynamic_layer.id())
            node_to_insert = canonical if canonical is not None else node

            try:
                parent_group.insertChildNode(index, node_to_insert)
            except Exception:
                parent_group.addChildNode(node_to_insert)

            node_to_insert.setItemVisibilityChecked(True)
            self.dock.iface.layerTreeView().refreshLayerSymbology(dynamic_layer.id())
            self.dock.iface.mapCanvas().refresh()
            self.dynamic_node = node_to_insert
        finally:
            self.dock.update_lock -= 1

    def add_dynamic_layer(self, raster_layers: List[QgsRasterLayer]):
        operation = GlobalSettings.getComposeOperation()
        name = self._compose_layer_name(raster_layers, operation)
        cached_path = DynamicLayerFactory.compute_or_get_cached_path(raster_layers, operation=operation)
        self._insert_dynamic_layer_from_cached_path(cached_path, name, len(raster_layers))

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
                    return f"{prefix}_{operation_name}_{min_label}-{max_label}"
                return f"{operation_name}_{n} {min_label}-{max_label}"

        return f"{operation_name}_{n} {first_name} – {last_name}"

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

    @staticmethod
    def copy_raster_style(source_layer: QgsRasterLayer, target_layer: QgsRasterLayer):
        try:
            if not source_layer.isValid() or not target_layer.isValid():
                raise ValueError("Both layers must be valid QgsRasterLayer instances")
            target_layer.setRenderer(source_layer.renderer().clone())
            for b in range(1, source_layer.bandCount() + 1):
                try:
                    nodata = source_layer.dataProvider().sourceNoDataValue(b)
                    if nodata is not None:
                        target_layer.dataProvider().setNoDataValue(b, nodata)
                except Exception:
                    pass
            target_layer.triggerRepaint()
        except Exception:
            traceback.print_exc()

    # ------------------------------------------------------------------
    # Single compose request lifecycle
    # ------------------------------------------------------------------
    def invalidate_single_compose_request(self, cancel_task: bool = False) -> int:
        self._compose_request_token += 1
        self._pending_single_compose_request = None
        self._desired_single_compose_cache_key = None
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
        return f"{operation_name}_{n} {original_group_name}"

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
        accepted = dialog.exec_() if hasattr(dialog, "exec_") else dialog.exec()
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

        if QApplication.keyboardModifiers() & Qt.ShiftModifier:
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
