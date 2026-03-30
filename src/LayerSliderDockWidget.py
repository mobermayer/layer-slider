from typing import List, Optional
from qgis.PyQt.QtGui import QCloseEvent, QHideEvent, QIcon, QShowEvent
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QToolButton,
)
from qgis.gui import QgsDockWidget
from qgis.PyQt import uic
from qgis.core import (
    QgsApplication,
    QgsLayerTreeNode,
    QgsMapLayer,
    QgsProject,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsRasterLayer,
)
from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
import os

from .PlusSpinBox import PlusSpinBox
from .GlobalSettings import GlobalSettings
from .ComposeManager import ComposeManager
from .DynamicLayerExporter import DynamicLayerExporter
from . import LayerRangeMapper
from .SettingsDialog import SettingsDialog

FORM_CLASS_LAYER, _ = uic.loadUiType(os.path.join(os.path.dirname(__file__), 'LayerSliderDockWidgetBase.ui'))


class LayerSliderDockWidget(QgsDockWidget, FORM_CLASS_LAYER):
    visible_changed = pyqtSignal(bool)
    update_lock = 0

    chk_lockgroups: QToolButton
    combo_group: QComboBox
    btn_reset: QToolButton
    slider: QSlider
    chk_avgrasters: QCheckBox
    combo_operation: QComboBox
    num_avgrasters: QSpinBox
    chk_avgdistinct: QToolButton
    num_avgoffset: QSpinBox
    btn_compose_settings: QToolButton
    btn_precalc_all: QToolButton
    btn_reset_shortcut: str = ""
    lockgroups_shortcut: str = ""
    avgrasters_shortcut: str = ""
    avgdistinct_shortcut: str = ""
    precalc_shortcut: str = ""
    export_shortcut: str = ""
    settings_shortcut: str = ""
    _btn_reset_show_icon: QIcon
    _btn_reset_hide_icon: QIcon
    _btn_lockgroups_unlocked_icon: QIcon
    _btn_lockgroups_locked_icon: QIcon
    _btn_avgdistinct_active_icon: QIcon
    _btn_avgdistinct_inactive_icon: QIcon

    def __init__(self, iface, parent=None):
        super().__init__(parent)

        self.setObjectName("LayerSliderDockWidget")
        self.setupUi(self)
        self.num_avgoffset = PlusSpinBox.replace_spinbox(self.num_avgoffset)

        self.iface = iface
        self._unloaded = False

        # core state
        self.current_group_node = None
        self.group_layers = []
        self.user_has_interacted = False
        self._lt_model = None
        self._btn_avgdistinct_active_icon = QIcon()
        self._btn_avgdistinct_inactive_icon = QIcon()

        # delegates
        self.compose = ComposeManager(self)
        self.exporter = DynamicLayerExporter(self)

        # coalesce tree/model updates
        self._group_refresh_timer = QTimer(self)
        self._group_refresh_timer.setSingleShot(True)
        self._group_refresh_timer.timeout.connect(self._refresh_groups_and_current_selection)

        # init ui values
        self.num_avgrasters.setValue(GlobalSettings.getNumAvgrasters())
        self.chk_avgdistinct.setChecked(GlobalSettings.getChkDistinct())
        self.num_avgoffset.setValue(GlobalSettings.getDistinctOffset())
        self._init_operation_combo()
        self._configure_compose_row_layout()
        self._init_distinct_icon()
        self._init_settings_button()
        self._init_precalc_button()
        self._init_reset_button_icons()
        self._init_lockgroups_button()
        self.settings_dialog = SettingsDialog(self)
        self.settings_dialog.settings_changed.connect(self.on_compose_settings_changed)

        # UI signals
        self.slider.valueChanged.connect(self.on_slider_changed)
        self.slider.sliderPressed.connect(self._mark_user_interaction)

        self.chk_lockgroups.toggled.connect(self.on_chk_lockgroups_toggled)
        self.combo_group.currentIndexChanged.connect(self.on_combo_changed)
        self.btn_reset.clicked.connect(self.on_btn_reset_clicked)
        self.chk_avgrasters.stateChanged.connect(self.on_chk_avgrasters_toggled)
        self.combo_operation.currentIndexChanged.connect(self._on_operation_changed)
        self.num_avgrasters.valueChanged.connect(self.on_num_avgrasters_changed)
        self.chk_avgdistinct.toggled.connect(self._on_avgdistinct_toggled)
        self.num_avgoffset.valueChanged.connect(self.on_num_avgoffset_changed)

        self._attach_to_layer_tree_model_root()

        pj = QgsProject.instance()
        pj.layersAdded.connect(self._on_project_layers_changed)
        pj.layersRemoved.connect(self._on_project_layers_changed)

        self._connect_selection_signals()
        QgsProject.instance().aboutToBeCleared.connect(self._disconnect_selection_signals)
        QgsProject.instance().readProject.connect(self._connect_selection_signals)
        self.exporter.connect_tree_context_menu()

        self.compose.remove_stale_dynamic_layers()
        QgsProject.instance().readProject.connect(self.compose.remove_stale_dynamic_layers)

        QTimer.singleShot(200, self._initial_populate_and_bind)

    def unload(self):
        self._unloaded = True
        self.compose.cancel_background_tasks()

        pj = QgsProject.instance()
        try:
            pj.layersAdded.disconnect(self._on_project_layers_changed)
        except Exception:
            pass
        try:
            pj.layersRemoved.disconnect(self._on_project_layers_changed)
        except Exception:
            pass

        try:
            lt = self.iface.layerTreeView()
            sel = lt.selectionModel()
            if sel:
                sel.currentChanged.disconnect(self.on_tree_selection_changed)
                sel.selectionChanged.disconnect(self.on_selection_changed)
        except Exception:
            pass

        self.compose.remove_dynamic_node()
        self.compose.remove_stale_dynamic_layers()
        try:
            if self.settings_dialog:
                self.settings_dialog.settings_changed.disconnect(self.on_compose_settings_changed)
                self.settings_dialog.deleteLater()
        except Exception:
            pass
        self.exporter.disconnect_tree_context_menu()

    # ------------------------------------------------------------------
    # Selection signal management
    # ------------------------------------------------------------------
    def _connect_selection_signals(self):
        lt = self.iface.layerTreeView()
        self._lt_selection_model = lt.selectionModel()
        if self._lt_selection_model:
            self._lt_selection_model.currentChanged.connect(self.on_tree_selection_changed)
            self._lt_selection_model.selectionChanged.connect(self.on_selection_changed)

    def _disconnect_selection_signals(self):
        try:
            if hasattr(self, "_lt_selection_model") and self._lt_selection_model:
                self._lt_selection_model.currentChanged.disconnect(self.on_tree_selection_changed)
                self._lt_selection_model.selectionChanged.disconnect(self.on_selection_changed)
        except Exception:
            pass
        self._lt_selection_model = None

    # ------------------------------------------------------------------
    # Layer tree root attachment
    # ------------------------------------------------------------------
    def _attach_to_layer_tree_model_root(self):
        try:
            lt_view = self.iface.layerTreeView()
            lt_model = lt_view.layerTreeModel()
            root_group = lt_model.rootGroup()
            self._lt_model = lt_model
        except Exception:
            root_group = QgsProject.instance().layerTreeRoot()
            self._lt_model = None

        self._root_group = root_group

        if hasattr(root_group, "layerOrderChanged"):
            root_group.layerOrderChanged.connect(self.on_tree_structure_changed)

        if hasattr(root_group, "visibilityChanged"):
            root_group.visibilityChanged.connect(self.on_tree_visibility_changed)

        for sig_name in ("childrenAdded", "childrenRemoved", "willRemoveChildren", "nodeAdded", "nodeRemoved"):
            if hasattr(root_group, sig_name):
                getattr(root_group, sig_name).connect(self.on_tree_structure_changed)

        if self._lt_model is not None:
            for sig_name, handler in (
                ("rowsInserted", self.on_tree_model_structure_changed),
                ("rowsRemoved", self.on_tree_model_structure_changed),
                ("modelReset", self.on_tree_model_structure_changed),
                ("dataChanged", self.on_tree_model_data_changed),
            ):
                if hasattr(self._lt_model, sig_name):
                    getattr(self._lt_model, sig_name).connect(handler)

    # ------------------------------------------------------------------
    # Initial population
    # ------------------------------------------------------------------
    def _initial_populate_and_bind(self):
        if self._unloaded:
            return
        self.populate_group_list()

        lt_view = self.iface.layerTreeView()
        idx = lt_view.currentIndex()
        if idx.isValid():
            node = lt_view.index2node(idx)
            self._adopt_node_selection(node)
        elif self.combo_group.count() > 0:
            self.combo_group.setCurrentIndex(0)
            self.on_combo_changed(0)

    # ------------------------------------------------------------------
    # UI widget toggling handlers
    # ------------------------------------------------------------------
    def on_chk_lockgroups_toggled(self, state):
        self.update_lockgroups_layout()
        if state is False:
            preserved_slider_idx = self.slider.value()
            self.on_selection_changed(None, None)
            if self.group_layers:
                self.slider.blockSignals(True)
                self.slider.setValue(self.limit_slider_index(preserved_slider_idx))
                self.slider.blockSignals(False)
                self._update_label_only(self.slider.value())

    def on_chk_avgrasters_toggled(self, state):
        if state is True or state is False:
            self.compose.invalidate_single_compose_request(cancel_task=False)
            mode_enabled_now = bool(state)
            mode_enabled_before = not mode_enabled_now
            old_slider_idx = self.slider.value()
            anchor_layer_idx = self.layer_index_from_slider_index(
                old_slider_idx,
                avgrasters_enabled=mode_enabled_before,
                prefer_range_end=mode_enabled_before and not mode_enabled_now,
            )

            cur_index = self.combo_group.currentIndex()
            if cur_index >= 0 and self.update_lock == 0:
                self.on_combo_changed(cur_index)
                target_slider_idx = self.slider_index_for_layer_index(anchor_layer_idx)
                self.slider.blockSignals(True)
                self.slider.setValue(target_slider_idx)
                self.slider.blockSignals(False)
                self.apply_visibility_from_index(target_slider_idx)
        self.compose.update_precalc_button_state()

    def _on_avgdistinct_toggled(self, checked: bool):
        self._update_avgdistinct_button_layout()
        self.compose.invalidate_single_compose_request(cancel_task=False)
        GlobalSettings.setChkDistinct(checked)

        cur_index = self.combo_group.currentIndex()
        if cur_index >= 0 and self.update_lock == 0:
            self.on_combo_changed(cur_index)
            self.apply_visibility_from_index(self.slider.value())
        self.compose.update_precalc_button_state()

    def on_num_avgrasters_changed(self, value):
        GlobalSettings.setNumAvgrasters(self.num_avgrasters.value())
        self.compose.invalidate_single_compose_request(cancel_task=False)

        if self.chk_avgrasters.isChecked():
            cur_index = self.combo_group.currentIndex()
            if cur_index >= 0 and self.update_lock == 0:
                self.on_combo_changed(cur_index)
                self.apply_visibility_from_index(self.slider.value())
        self.set_num_avgoffset_maximum()
        self.compose.update_precalc_button_state()

    def on_num_avgoffset_changed(self, value):
        GlobalSettings.setDistinctOffset(value)
        self.compose.invalidate_single_compose_request(cancel_task=False)

        if self.chk_avgdistinct.isChecked():
            cur_index = self.combo_group.currentIndex()
            if cur_index >= 0 and self.update_lock == 0:
                self.on_combo_changed(cur_index)
                self.apply_visibility_from_index(self.slider.value())
        self.compose.update_precalc_button_state()

    def set_num_avgoffset_maximum(self):
        maximum = max(0, min(self.num_avgrasters.value() - 1, len(self.get_layer_ranges()) - 1))
        self.num_avgoffset.setMaximum(maximum)

    def _on_operation_changed(self, _index: int):
        key = self.combo_operation.currentData()
        GlobalSettings.setComposeOperation(key)
        self._update_compose_toggle_tooltip()
        self.compose.invalidate_single_compose_request(cancel_task=False)
        self.compose.update_precalc_button_state()
        if not self.chk_avgrasters.isChecked():
            return
        if self.update_lock == 0:
            current_slider_idx = self.limit_slider_index(self.slider.value())
            self.apply_visibility_from_index(current_slider_idx)

    def on_compose_settings_changed(self):
        self.compose.invalidate_single_compose_request(cancel_task=False)
        self.compose.update_precalc_button_state()
        if not self.chk_avgrasters.isChecked():
            return
        if self.update_lock == 0:
            current_slider_idx = self.limit_slider_index(self.slider.value())
            self.apply_visibility_from_index(current_slider_idx)

    # ------------------------------------------------------------------
    # UI init helpers
    # ------------------------------------------------------------------
    def _init_operation_combo(self):
        self.combo_operation.blockSignals(True)
        for key, short_name, _full in GlobalSettings.COMPOSE_OPERATIONS:
            self.combo_operation.addItem(short_name, key)
        current = GlobalSettings.getComposeOperation()
        idx = self.combo_operation.findData(current)
        self.combo_operation.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_operation.blockSignals(False)
        self._update_compose_toggle_tooltip()

    def _configure_compose_row_layout(self):
        combo_policy = self.combo_operation.sizePolicy()
        combo_policy.setHorizontalPolicy(QSizePolicy.MinimumExpanding)
        self.combo_operation.setSizePolicy(combo_policy)

        if not hasattr(self, "neighborLayout") or self.neighborLayout is None:
            return

        combo_index = self.neighborLayout.indexOf(self.combo_operation)
        spacer_index = -1
        spacer_item = None
        for index in range(self.neighborLayout.count()):
            item = self.neighborLayout.itemAt(index)
            if item is not None and item.spacerItem() is not None:
                spacer_index = index
                spacer_item = item.spacerItem()
                break

        if spacer_item is not None:
            spacer_item.changeSize(0, 1, QSizePolicy.Expanding, QSizePolicy.Minimum)

        if combo_index >= 0:
            self.neighborLayout.setStretch(combo_index, 4)
        if spacer_index >= 0:
            self.neighborLayout.setStretch(spacer_index, 1)
        self.neighborLayout.invalidate()

    def _update_compose_toggle_tooltip(self):
        full_name = GlobalSettings.getComposeOperationFullName()
        self.chk_avgrasters.setToolTip(f"Toggle layer composition ({full_name}){self.avgrasters_shortcut}")

    def _init_distinct_icon(self):
        self._btn_avgdistinct_active_icon = self._first_theme_icon([
            "mActionAlignBottom.svg",
            "/mActionAlignBottom.svg",
        ])
        self._btn_avgdistinct_inactive_icon = self._first_theme_icon([
            "mActionLowerItems.svg",
            "/mActionLowerItems.svg",
        ])
        if self._btn_avgdistinct_active_icon.isNull():
            self._btn_avgdistinct_active_icon = QIcon(":images/themes/default/mActionAlignBottom.svg")
        if self._btn_avgdistinct_inactive_icon.isNull():
            self._btn_avgdistinct_inactive_icon = QIcon(":images/themes/default/mActionLowerItems.svg")
        if self._btn_avgdistinct_active_icon.isNull():
            self._btn_avgdistinct_active_icon = self.style().standardIcon(QStyle.SP_DialogYesButton)
        if self._btn_avgdistinct_inactive_icon.isNull():
            self._btn_avgdistinct_inactive_icon = self.style().standardIcon(QStyle.SP_DialogNoButton)
        self.chk_avgdistinct.setText("")
        self.chk_avgdistinct.setAutoRaise(True)
        self._update_avgdistinct_button_layout()

    def _update_avgdistinct_button_layout(self):
        if not hasattr(self, "_btn_avgdistinct_active_icon"):
            self._btn_avgdistinct_active_icon = QIcon()
        if not hasattr(self, "_btn_avgdistinct_inactive_icon"):
            self._btn_avgdistinct_inactive_icon = QIcon()
        if self.chk_avgdistinct.isChecked():
            self.chk_avgdistinct.setIcon(self._btn_avgdistinct_active_icon)
            self.chk_avgdistinct.setToolTip(f"Compose overlapping rasters{self.avgdistinct_shortcut}")
        else:
            self.chk_avgdistinct.setIcon(self._btn_avgdistinct_inactive_icon)
            self.chk_avgdistinct.setToolTip(f"Compose non-overlapping rasters{self.avgdistinct_shortcut}")

    def set_chk_avgdistinct_shortcut(self, _avgdistinct_shortcut: str):
        self.avgdistinct_shortcut = _avgdistinct_shortcut
        self._update_avgdistinct_button_layout()

    def _init_settings_button(self):
        icon = QgsApplication.getThemeIcon("/mActionOptions.svg")
        if icon.isNull():
            icon = QIcon.fromTheme("mActionOptions")
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.SP_FileDialogDetailedView)
        self.btn_compose_settings.setIcon(icon)
        self.btn_compose_settings.setText("")
        self.btn_compose_settings.setAutoRaise(True)
        self.btn_compose_settings.setToolTip(f"Settings{self.settings_shortcut}")
        self.btn_compose_settings.clicked.connect(self.show_settings)

    def _init_precalc_button(self):
        icon = QgsApplication.getThemeIcon("/processingAlgorithm.svg")
        if icon.isNull():
            icon = QIcon.fromTheme("processingAlgorithm")
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.SP_ComputerIcon)
        self.btn_precalc_all.setIcon(icon)
        self.btn_precalc_all.setText("")
        self.btn_precalc_all.setAutoRaise(True)
        self.btn_precalc_all.clicked.connect(self.compose.on_precalc_all_clicked)
        self.compose.update_precalc_button_state()

    def _first_theme_icon(self, icon_paths: list[str]) -> QIcon:
        for icon_path in icon_paths:
            icon = QgsApplication.getThemeIcon(icon_path)
            if not icon.isNull():
                return icon
        return QIcon()

    def _init_reset_button_icons(self):
        self._btn_reset_show_icon = self._first_theme_icon([
            "/mActionShowAllLayers.svg",
            "/mActionShowAllLayersGray.svg",
        ])
        self._btn_reset_hide_icon = self._first_theme_icon([
            "/mActionHideAllLayers.svg",
        ])

        if self._btn_reset_show_icon.isNull():
            self._btn_reset_show_icon = self.style().standardIcon(QStyle.SP_DialogYesButton)
        if self._btn_reset_hide_icon.isNull():
            self._btn_reset_hide_icon = self.style().standardIcon(QStyle.SP_DialogNoButton)

        self.btn_reset.setText("")
        self.btn_reset.setAutoRaise(True)
        self.update_btn_reset_text()

    def _init_lockgroups_button(self):
        self._btn_lockgroups_unlocked_icon = self._first_theme_icon([
            "/unlockedGray.svg",
            "/unlocked.svg",
        ])
        self._btn_lockgroups_locked_icon = self._first_theme_icon([
            "/locked.svg",
            "/lockedGray.svg",
        ])

        if self._btn_lockgroups_unlocked_icon.isNull():
            self._btn_lockgroups_unlocked_icon = self.style().standardIcon(QStyle.SP_DialogOpenButton)
        if self._btn_lockgroups_locked_icon.isNull():
            self._btn_lockgroups_locked_icon = self.style().standardIcon(QStyle.SP_MessageBoxWarning)

        self.chk_lockgroups.setCheckable(True)
        self.chk_lockgroups.setText("")
        self.chk_lockgroups.setAutoRaise(True)
        self.update_lockgroups_layout()

    def show_settings(self):
        if not self.settings_dialog.isVisible():
            self.settings_dialog.refresh_from_settings()
            self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    # ------------------------------------------------------------------
    # Populate groups
    # ------------------------------------------------------------------
    def populate_group_list(self):
        self.combo_group.blockSignals(True)
        try:
            self.combo_group.clear()

            root = getattr(self, "_root_group", None)
            if root is None:
                root = QgsProject.instance().layerTreeRoot()

            self.combo_group.addItem("(Project Root)", root)

            def add_groups(group, depth=0):
                for child in group.children():
                    if isinstance(child, QgsLayerTreeGroup):
                        indent = "    " * depth
                        self.combo_group.addItem(f"{indent}{child.name()}", child)
                        add_groups(child, depth + 1)

            add_groups(root)
        finally:
            self.combo_group.blockSignals(False)

    # ------------------------------------------------------------------
    # Project-level layer changes fallback
    # ------------------------------------------------------------------
    def _on_project_layers_changed(self, *args):
        if self.update_lock > 0:
            return
        self._schedule_group_refresh(100)

    # ------------------------------------------------------------------
    # Layer-tree selection tracking
    # ------------------------------------------------------------------
    def on_tree_selection_changed(self, current, previous=None):
        if self.update_lock > 0:
            return
        if self.chk_lockgroups.isChecked():
            return
        node = self.iface.layerTreeView().index2node(current)
        if node is not None and self._node_belongs_to_root(node):
            self._adopt_node_selection(node)

    def on_selection_changed(self, selected, deselected):
        if self.update_lock > 0:
            return
        if self.chk_lockgroups.isChecked():
            return
        lt = self.iface.layerTreeView()
        node = None
        if hasattr(lt, "selectedNodes"):
            nodes = lt.selectedNodes()
            if nodes:
                node = nodes[0]
        else:
            idx = lt.currentIndex()
            if idx.isValid():
                node = lt.index2node(idx)

        if node is None:
            return
        if not self._node_belongs_to_root(node):
            return

        if isinstance(node, QgsLayerTreeLayer):
            self._adopt_node_selection(node)

            for i, n in enumerate(self.group_layers):
                if n == node:
                    slider_index = self.slider_index_for_layer_index(i)
                    self.slider.blockSignals(True)
                    self.slider.setValue(slider_index)
                    self.slider.blockSignals(False)
                    self._update_label_only(slider_index)
                    break

        elif isinstance(node, QgsLayerTreeGroup):
            self._adopt_node_selection(node)

    def _adopt_node_selection(self, node):
        root = getattr(self, "_root_group", QgsProject.instance().layerTreeRoot())

        if isinstance(node, QgsLayerTreeGroup):
            group = node
        elif isinstance(node, QgsLayerTreeLayer):
            parent = node.parent()
            group = parent if isinstance(parent, QgsLayerTreeGroup) else root
        else:
            group = root

        for i in range(self.combo_group.count()):
            if self.combo_group.itemData(i) == group:
                self.combo_group.setCurrentIndex(i)
                return

        self.combo_group.blockSignals(True)
        self.combo_group.addItem(group.name(), group)
        self.combo_group.setCurrentIndex(self.combo_group.count() - 1)
        self.combo_group.blockSignals(False)

        self.on_combo_changed(self.combo_group.currentIndex())

    # ------------------------------------------------------------------
    # External visibility changes from tree
    # ------------------------------------------------------------------
    def on_tree_visibility_changed(self, node):
        if self.update_lock > 0:
            return
        if not isinstance(node, QgsLayerTreeLayer):
            return
        if not self.current_group_node:
            return
        if node.parent() != self.current_group_node:
            return

        for i, n in enumerate(self.group_layers):
            if n.isVisible():
                slider_index = self.slider_index_for_layer_index(i)
                self.slider.blockSignals(True)
                self.slider.setValue(slider_index)
                self.slider.blockSignals(False)
                self._update_label_only(slider_index)
                self.update_btn_reset_text()
                return

        if self.group_layers:
            self._update_label_only(self.slider.value())
        else:
            self.label_index.setText("No children in group")
        self.update_btn_reset_text()

    # ------------------------------------------------------------------
    # Structural changes
    # ------------------------------------------------------------------
    def on_tree_structure_changed(self, *args):
        if self.update_lock > 0:
            return
        self._schedule_group_refresh(0)

    def on_tree_model_structure_changed(self, *args):
        if self.update_lock > 0:
            return
        self._schedule_group_refresh(0)

    def on_tree_model_data_changed(self, *args):
        if self.update_lock > 0:
            return
        roles = args[2] if len(args) >= 3 else None
        if roles is not None:
            try:
                role_ids = {int(role) for role in roles}
            except Exception:
                role_ids = set()
            if role_ids and int(Qt.DisplayRole) not in role_ids and int(Qt.EditRole) not in role_ids:
                return
        self._schedule_group_refresh(0)

    def _schedule_group_refresh(self, delay_ms: int = 0):
        if self._unloaded:
            return
        self._group_refresh_timer.start(max(0, int(delay_ms)))

    def _refresh_groups_and_current_selection(self):
        if self._unloaded or self.update_lock > 0:
            return

        selected_group = self.current_group_node
        if selected_group is None:
            cur = self.combo_group.currentIndex()
            if cur >= 0:
                selected_group = self.combo_group.itemData(cur)

        old_slider_value = self.slider.value()

        self.populate_group_list()

        target_index = -1
        if selected_group is not None and self._node_belongs_to_root(selected_group):
            for i in range(self.combo_group.count()):
                if self.combo_group.itemData(i) == selected_group:
                    target_index = i
                    break

        if target_index < 0:
            target_index = self.combo_group.currentIndex()
        if target_index < 0 and self.combo_group.count() > 0:
            target_index = 0
        if target_index < 0:
            return

        self.combo_group.blockSignals(True)
        try:
            self.combo_group.setCurrentIndex(target_index)
        finally:
            self.combo_group.blockSignals(False)

        self.on_combo_changed(target_index)

        if self.group_layers:
            self.slider.blockSignals(True)
            try:
                self.slider.setValue(self.limit_slider_index(old_slider_value))
            finally:
                self.slider.blockSignals(False)
            self._update_label_only(self.slider.value())

    # ------------------------------------------------------------------
    # Combo changed -> load group layers
    # ------------------------------------------------------------------
    def on_combo_changed(self, index):
        if self.update_lock > 0:
            return
        self.set_num_avgoffset_maximum()
        self.user_has_interacted = False

        if not hasattr(self, "_root_group"):
            self._attach_to_layer_tree_model_root()

        group = self.combo_group.itemData(index)
        if group is None or not self._node_belongs_to_root(group):
            group = getattr(self, "_root_group", QgsProject.instance().layerTreeRoot())

        if group != self.current_group_node:
            self.compose.invalidate_single_compose_request(cancel_task=False)
        self.current_group_node = group

        if self.chk_avgrasters.isChecked():
            children = [
                c for c in group.children()
                if isinstance(c, QgsLayerTreeLayer) and ComposeManager.is_dynamiclayer_compatible(c.layer())
            ]
        else:
            children = [c for c in group.children() if isinstance(c, (QgsLayerTreeLayer, QgsLayerTreeGroup))]

        self.group_layers = children

        if not self.group_layers:
            self.slider.setEnabled(False)
            self.label_index.setText("No children in group")
            self.compose.update_precalc_button_state()
            return

        self.update_btn_reset_text()

        layer_ranges = self.get_layer_ranges()
        self.slider.blockSignals(True)
        self.slider.setMaximum(len(layer_ranges) - 1)
        self.slider.blockSignals(False)

        visible = [i for i, node in enumerate(self.group_layers) if node.itemVisibilityChecked()]
        initial_layer_idx = visible[0] if visible else 0
        initial_idx = self.slider_index_for_layer_index(initial_layer_idx)

        self.slider.blockSignals(True)
        self.slider.setValue(initial_idx)
        self.slider.blockSignals(False)
        self.slider.setEnabled(True)
        self._update_label_only(initial_idx)
        self.compose.update_precalc_button_state()

    # ------------------------------------------------------------------
    # Layer range mapping (delegated to LayerRangeMapper)
    # ------------------------------------------------------------------
    def _get_range_params(self):
        return {
            "num_avgrasters": self.num_avgrasters.value(),
            "distinct": self.chk_avgdistinct.isChecked(),
            "offset": self.num_avgoffset.value(),
        }

    def get_layer_ranges(self, avgrasters_enabled: bool | None = None):
        if avgrasters_enabled is None:
            avgrasters_enabled = self.chk_avgrasters.isChecked()
        return LayerRangeMapper.layer_ranges_for_count(
            len(self.group_layers), avgrasters_enabled, **self._get_range_params(),
        )

    def limit_slider_index(self, idx: int) -> int:
        return LayerRangeMapper.limit_slider_index(idx, self.get_layer_ranges())

    def layer_index_from_slider_index(
        self,
        slider_idx: int,
        avgrasters_enabled: bool | None = None,
        prefer_range_end: bool = False,
    ) -> int:
        ranges = self.get_layer_ranges(avgrasters_enabled=avgrasters_enabled)
        return LayerRangeMapper.layer_index_from_slider_index(slider_idx, ranges, prefer_range_end)

    def slider_index_for_layer_index(self, layer_index: int) -> int:
        return LayerRangeMapper.slider_index_for_layer_index(layer_index, self.get_layer_ranges())

    # ------------------------------------------------------------------
    # Slider and visibility
    # ------------------------------------------------------------------
    def _mark_user_interaction(self):
        self.user_has_interacted = True

    def on_slider_changed(self, value):
        self.user_has_interacted = True
        if not self.current_group_node or not self._node_belongs_to_root(self.current_group_node):
            return
        self.apply_visibility_from_index(value)

    def set_item_visible(self, item: QgsLayerTreeNode | None, value: bool):
        if item is None:
            return
        item.setItemVisibilityChecked(value)
        if value:
            self.set_item_visible(item.parent(), value)

    def apply_visibility_from_index(self, idx: int):
        idx = self.limit_slider_index(idx)

        self.compose.remove_dynamic_node()
        if not self.group_layers:
            return

        g = self.current_group_node
        while g and isinstance(g, QgsLayerTreeGroup):
            g.setItemVisibilityChecked(True)
            g = g.parent()

        layers_to_average: List[QgsRasterLayer] = []
        for rangeIndex, layer_range in enumerate(self.get_layer_ranges()):
            if rangeIndex == idx:
                if len(layer_range) == 1:
                    for i in layer_range:
                        node = self.group_layers[i]
                        node.setItemVisibilityChecked(True)
                        self.repaint_safe(node)
                else:
                    nodes = [self.group_layers[i] for i in layer_range]
                    for node in nodes:
                        node.setItemVisibilityChecked(False)
                    layers = [node.layer() for node in nodes if isinstance(node, QgsLayerTreeLayer)]
                    layers_to_average = [layer for layer in layers if isinstance(layer, QgsRasterLayer)]
                    if len(nodes) != len(layers_to_average):
                        raise Exception("Received non-raster-layer to average")
            else:
                for i in layer_range:
                    self.group_layers[i].setItemVisibilityChecked(False)

        self._update_label_only(idx)
        self.update_btn_reset_text()
        if layers_to_average:
            self.compose.queue_single_compose_request(layers_to_average, idx)
        else:
            self.compose._desired_single_compose_cache_key = None
            self.compose._pending_single_compose_request = None

    def repaint_safe(self, node_or_layer):
        if isinstance(node_or_layer, QgsMapLayer):
            layer = node_or_layer
        else:
            if not hasattr(node_or_layer, "layer"):
                return
            layer = node_or_layer.layer()
        if not isinstance(layer, QgsMapLayer):
            return
        layer.triggerRepaint()

    # ------------------------------------------------------------------
    # Label / button state
    # ------------------------------------------------------------------
    def _update_label_only(self, idx: int):
        idx = self.limit_slider_index(idx)
        layer_ranges = self.get_layer_ranges()
        layer_range = layer_ranges[idx]

        if not self.chk_avgrasters.isChecked():
            for i in layer_range:
                name = self.get_name_of_node(self.group_layers[i])
                self.label_index.setText(f"{i+1}/{len(self.group_layers)}:  {name}")
        else:
            if len(layer_range) == 1:
                for i in layer_range:
                    name = self.get_name_of_node(self.group_layers[i])
                    self.label_index.setText(f"{i+1}–{i+1}/{len(self.group_layers)}:  {name}")
            else:
                idx_start = min(layer_range)
                idx_end = max(layer_range)
                name = self.compose.dynamic_node.name() if self.compose.dynamic_node_defined() else "—"
                self.label_index.setText(f"{idx_start+1}–{idx_end+1}/{len(self.group_layers)}:  {name}")

    def get_name_of_node(self, node) -> str:
        try:
            if isinstance(node, QgsLayerTreeLayer):
                return node.layer().name()
            else:
                return node.name()
        except Exception:
            return "—"

    def set_visibility_group_layers(self, visible: bool):
        self.compose.invalidate_single_compose_request(cancel_task=False)
        self.compose.remove_dynamic_node()

        if not self.group_layers:
            return
        for node in self.group_layers:
            node.setItemVisibilityChecked(visible)

        self.iface.mapCanvas().refresh()

        self.update_btn_reset_text()
        if self.group_layers:
            self._update_label_only(self.slider.value())
        else:
            self.label_index.setText("No children in group")

    def set_btn_reset_shortcut(self, _btn_reset_shortcut: str):
        self.btn_reset_shortcut = _btn_reset_shortcut
        self.update_btn_reset_text()

    def update_btn_reset_text(self):
        if self.some_layers_visible():
            self.btn_reset.setText("")
            self.btn_reset.setIcon(self._btn_reset_show_icon)
            self.btn_reset.setToolTip("Hide current slider layer(s)" + self.btn_reset_shortcut)
        else:
            self.btn_reset.setText("")
            self.btn_reset.setIcon(self._btn_reset_hide_icon)
            self.btn_reset.setToolTip("Show current layer(s)" + self.btn_reset_shortcut)

    def set_chk_lockgroups_shortcut(self, _lockgroups_shortcut: str):
        self.lockgroups_shortcut = _lockgroups_shortcut
        self.update_lockgroups_layout()

    def update_lockgroups_layout(self):
        if self.chk_lockgroups.isChecked():
            self.chk_lockgroups.setIcon(self._btn_lockgroups_locked_icon)
            self.chk_lockgroups.setToolTip("Follow group selection" + self.lockgroups_shortcut)
        else:
            self.chk_lockgroups.setIcon(self._btn_lockgroups_unlocked_icon)
            self.chk_lockgroups.setToolTip("Lock group selection" + self.lockgroups_shortcut)

    def set_chk_avgrasters_shortcut(self, _avgrasters_shortcut: str):
        self.avgrasters_shortcut = _avgrasters_shortcut
        self._update_compose_toggle_tooltip()

    def set_precalc_shortcut(self, _precalc_shortcut: str):
        self.precalc_shortcut = _precalc_shortcut
        self.compose.update_precalc_button_state()

    def set_export_shortcut(self, _export_shortcut: str):
        self.export_shortcut = _export_shortcut
        self.compose.update_precalc_button_state()

    def set_settings_shortcut(self, _settings_shortcut: str):
        self.settings_shortcut = _settings_shortcut
        self.btn_compose_settings.setToolTip(f"Settings{self.settings_shortcut}")

    def on_btn_reset_clicked(self):
        if QApplication.keyboardModifiers() & Qt.ShiftModifier:
            self.toggle_show_selected()
            return
        self.toggle_show_all()

    def toggle_show_all(self):
        if self.some_layers_visible():
            self.set_visibility_group_layers(False)
        else:
            self.apply_visibility_from_index(self.slider.value())

    def toggle_show_selected(self):
        layer_tree_view = self.iface.layerTreeView()
        if layer_tree_view is None:
            return
        selected_nodes = layer_tree_view.selectedNodes()
        if not selected_nodes:
            return

        some_visible = any(node.itemVisibilityChecked() for node in selected_nodes)
        for node in selected_nodes:
            self.set_item_visible(node, not some_visible)

    def some_layers_visible(self) -> bool:
        if self.compose.dynamic_node_defined():
            return True
        if not self.group_layers:
            return False
        return any(layer.itemVisibilityChecked() for layer in self.group_layers)

    # ------------------------------------------------------------------
    # Show / hide / close events
    # ------------------------------------------------------------------
    def showEvent(self, event: QShowEvent | None):
        is_spontaneous = bool(event and event.spontaneous())
        if not is_spontaneous:
            self.visible_changed.emit(True)
            self.compose.update_precalc_button_state()
            if self.update_lock == 0:
                QTimer.singleShot(0, lambda: (
                    None if self._unloaded else self.update_btn_reset_text()
                ))
        if event:
            event.accept()

    def closeEvent(self, event: QCloseEvent | None):
        self.visible_changed.emit(False)
        self.compose.cancel_background_tasks()
        self.compose.remove_dynamic_node()
        if event:
            event.accept()

    def hideEvent(self, event: QHideEvent):
        is_spontaneous = bool(event and event.spontaneous())
        if not is_spontaneous:
            self.visible_changed.emit(False)
            self.compose.cancel_background_tasks()
            self.compose.remove_dynamic_node()
        event.accept()

    # ------------------------------------------------------------------
    # Public API for plugin orchestrator (LayerSlider.py)
    # ------------------------------------------------------------------
    def focus_slider(self):
        if not self.slider:
            return
        self.slider.setFocus()

    def prev_layer(self):
        if not self.slider:
            return
        self.slider.setValue(self.slider.value() - 1)

    def next_layer(self):
        if not self.slider:
            return
        self.slider.setValue(self.slider.value() + 1)

    def set_slider_tooltip(self, tooltip):
        if not self.slider:
            return
        self.slider.setToolTip(tooltip)

    def toggle_lockgroups(self):
        if not self.chk_lockgroups:
            return
        self.chk_lockgroups.setChecked(not self.chk_lockgroups.isChecked())

    def toggle_avgrasters(self):
        self.chk_avgrasters.setChecked(not self.chk_avgrasters.isChecked())

    def toggle_avgdistinct(self):
        self.chk_avgdistinct.setChecked(not self.chk_avgdistinct.isChecked())

    def trigger_precalc_all(self):
        self.compose.on_precalc_all_clicked()

    def trigger_export_to_directory(self):
        self.compose._on_precalc_all_shift_clicked()

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _node_belongs_to_root(self, node):
        root = getattr(self, "_root_group", QgsProject.instance().layerTreeRoot())
        try:
            p = node
            while p is not None:
                if p == root:
                    return True
                p = p.parent()
        except RuntimeError:
            return False
        return False
