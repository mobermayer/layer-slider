"""
LayerSlider QGIS plugin (single-file)

- Non-blocking: uses a QDockWidget
- Auto-tracks the currently selected group in the main Layers panel
- Uses TRUE VISIBILITY changes (setItemVisibilityChecked) instead of opacity

Drop this file into your plugin folder (or adapt into the normal QGIS plugin structure).
This is a self-contained example for QGIS 3.x (PyQGIS).

Tested API usage references:
- QgsLayerTreeView.index2node / selectionModel().currentChanged
- QgsLayerTreeGroup / QgsLayerTreeLayer
- QgsLayerTreeNode.setItemVisibilityChecked

Author: assistant (example)
"""

from typing import Optional
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QAction, QIcon
from qgis.gui import QgisInterface, QgsGui

from .GlobalSettings import GlobalSettings
from .LayerSliderDockWidget import LayerSliderDockWidget
import os

PLUGIN_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# The plugin main class -----------------------------------------------------
class LayerSlider:
    iface: QgisInterface
    dock: Optional[LayerSliderDockWidget]
    icon: QIcon
    action: Optional[QAction]
    shortcut_toggle_lock: Optional[QAction]
    shortcut_left: Optional[QAction]
    shortcut_right: Optional[QAction]
    shortcut_toggle_show_all: Optional[QAction]
    shortcut_toggle_show_selected: Optional[QAction]
    shortcut_toggle_avgrasters: Optional[QAction]
    shortcut_toggle_avgdistinct: Optional[QAction]
    shortcut_precalc_all: Optional[QAction]
    shortcut_export_to_directory: Optional[QAction]
    shortcut_settings: Optional[QAction]

    def __init__(self, iface: QgisInterface):
        """iface : QgisInterface provided by QGIS when plugin is loaded."""
        self.iface = iface
        self.icon = QIcon(os.path.join(PLUGIN_DIR, "assets/icon.png"))
        self.dock = None
        self.action = None

    def initGui(self):
        self.action = QAction(self.icon, "Layer Slider - widget", self.iface.mainWindow())
        self.action.triggered.connect(self.toggle_dock)

        # add toolbar icon and menu entry
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&LayerSlider", self.action)
        self.register_shortcuts()

        if GlobalSettings.getWidgetVisible():
            self.init_dock()
            self.iface.addDockWidget(GlobalSettings.getWidgetLocation(), self.dock)

    def init_dock(self):
        if self.dock:
            self.unload_dock()
        self.dock = LayerSliderDockWidget(self.iface)
        self.dock.visible_changed.connect(self.on_dock_visible_changed)
        self.dock.dockLocationChanged.connect(self.on_dock_location_changed)
        self.set_slider_tooltip()
        self.set_btn_reset_shortcut()
        self.set_chk_lockgroups_shortcut()
        self.set_chk_avgrasters_shortcut()
        self.set_chk_avgdistinct_shortcut()
        self.set_precalc_shortcut()
        self.set_settings_shortcut()

    def unload_dock(self):
        if self.dock:
            # disconnect before removing so the stored state is unaffected
            self.dock.visible_changed.disconnect(self.on_dock_visible_changed)
            self.dock.dockLocationChanged.disconnect(self.on_dock_location_changed)

            self.dock.unload()
            self.iface.removeDockWidget(self.dock)

            self.dock.deleteLater()
            self.dock = None

    def register_shortcut(self, action: QAction, shortcut: str, on_triggered, on_keys_changed = None) -> QAction:
        self.iface.registerMainWindowAction(action, shortcut)
        self.iface.addPluginToMenu("&LayerSlider", action)
        action.triggered.connect(on_triggered)
        QgsGui.shortcutsManager().registerAction(action)
        if on_keys_changed: action.changed.connect(on_keys_changed)
        return action

    def register_shortcuts(self):
        window = self.iface.mainWindow()
        self.shortcut_settings = self.register_shortcut(QAction(self.icon, "Layer Slider - settings", window), "", self.show_settings, self.set_settings_shortcut)
        self.shortcut_left = self.register_shortcut(QAction(self.icon, "Layer Slider - previous layer", window), "D", self.shortcut_left_triggered, self.set_slider_tooltip)
        self.shortcut_right = self.register_shortcut(QAction(self.icon, "Layer Slider - next layer", window), "F", self.shortcut_right_triggered, self.set_slider_tooltip)
        self.shortcut_toggle_show_selected = self.register_shortcut(QAction(self.icon, "Layer Slider - toggle visibility of selected layer in tree", window), "C", self.toggle_show_selected, self.set_btn_reset_shortcut)
        self.shortcut_toggle_show_all = self.register_shortcut(QAction(self.icon, "Layer Slider - toggle show current layer", window), "V", self.toggle_show_all, self.set_btn_reset_shortcut)
        self.shortcut_toggle_lock = self.register_shortcut(QAction(self.icon, "Layer Slider - toggle lock layers", window), "Shift+D", self.shortcut_toggle_lock_triggered, self.set_chk_lockgroups_shortcut)
        self.shortcut_toggle_avgrasters = self.register_shortcut(QAction(self.icon, "Layer Slider - toggle compose rasters", window), "Shift+F", self.toggle_avgrasters, self.set_chk_avgrasters_shortcut)
        self.shortcut_toggle_avgdistinct = self.register_shortcut(QAction(self.icon, "Layer Slider - toggle compose distinct", window), "", self.toggle_avgdistinct, self.set_chk_avgdistinct_shortcut)
        self.shortcut_precalc_all = self.register_shortcut(QAction(self.icon, "Layer Slider - pre-compute all composed ranges", window), "", self.trigger_precalc_all, self.set_precalc_shortcut)
        self.shortcut_export_to_directory = self.register_shortcut(QAction(self.icon, "Layer Slider - export to directory", window), "", self.trigger_export_to_directory, self.set_export_shortcut)

    def unregister_shortcut(self, action: QAction | None, on_keys_changed = None) -> None:
        if action is None: return None

        if on_keys_changed: action.changed.disconnect(on_keys_changed)
        QgsGui.shortcutsManager().unregisterAction(action)
        self.iface.removePluginMenu("&LayerSlider", action)
        self.iface.unregisterMainWindowAction(action)

        return None

    def unregister_shortcuts(self):
        self.shortcut_right = self.unregister_shortcut(self.shortcut_right, self.set_slider_tooltip)
        self.shortcut_left = self.unregister_shortcut(self.shortcut_left, self.set_slider_tooltip)
        self.shortcut_toggle_lock = self.unregister_shortcut(self.shortcut_toggle_lock, self.set_chk_lockgroups_shortcut)
        self.shortcut_toggle_show_all = self.unregister_shortcut(self.shortcut_toggle_show_all, self.set_btn_reset_shortcut)
        self.shortcut_toggle_show_selected = self.unregister_shortcut(self.shortcut_toggle_show_selected, self.set_btn_reset_shortcut)
        self.shortcut_toggle_avgrasters = self.unregister_shortcut(self.shortcut_toggle_avgrasters, self.set_chk_avgrasters_shortcut)
        self.shortcut_toggle_avgdistinct = self.unregister_shortcut(self.shortcut_toggle_avgdistinct, self.set_chk_avgdistinct_shortcut)
        self.shortcut_precalc_all = self.unregister_shortcut(self.shortcut_precalc_all, self.set_precalc_shortcut)
        self.shortcut_export_to_directory = self.unregister_shortcut(self.shortcut_export_to_directory, self.set_export_shortcut)
        self.shortcut_settings = self.unregister_shortcut(self.shortcut_settings, self.set_settings_shortcut)

    def unload(self):
        self.unregister_shortcuts()

        # remove UI artifacts
        if self.dock:
            self.unload_dock()

        if self.action:
            self.iface.removePluginMenu("&LayerSlider", self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None

    def toggle_dock(self):
        if not self.dock:
            self.init_dock()
            self.iface.addDockWidget(GlobalSettings.getWidgetLocation(), self.dock)
        else:
            self.dock.setVisible(not self.dock.isVisible())

    def on_dock_visible_changed(self, visible: bool):
        GlobalSettings.setWidgetVisible(visible)

    def on_dock_location_changed(self, location: Qt.DockWidgetArea):
        GlobalSettings.setWidgetLocation(location)

    def shortcut_toggle_lock_triggered(self):
        if not self.dock or not self.dock.isVisible():
            self.toggle_dock()
        self.dock.toggle_lockgroups()

    def shortcut_left_triggered(self):
        if not self.dock or not self.dock.isVisible():
            return
        self.dock.prev_layer()

    def shortcut_right_triggered(self):
        if not self.dock or not self.dock.isVisible():
            return
        self.dock.next_layer()

    def set_slider_tooltip(self):
        if not self.dock: return

        left_keys = self.shortcut_left.shortcut()
        right_keys = self.shortcut_right.shortcut()
        left_key_text = left_keys.toString() if left_keys else ""
        right_key_text = right_keys.toString() if right_keys else ""
        left_str = f"Previous ({left_key_text})" if left_key_text else "Previous"
        right_str = f"Next ({right_key_text})" if right_key_text else "Next"
        hover_str = "Previous/Next (Hover+Mousewheel)"

        tooltip = f"Shortcuts:\n- {left_str}\n- {right_str}\n- {hover_str}"
        self.dock.set_slider_tooltip(tooltip)

    def set_btn_reset_shortcut(self):
        if not self.dock: return

        show_all_keys = self.shortcut_toggle_show_all.shortcut()
        show_all_string = f" ({show_all_keys.toString()})" if show_all_keys else ""

        selected_keys = self.shortcut_toggle_show_selected.shortcut()
        selected_key_text = f" ({selected_keys.toString().strip()})" if selected_keys else ""
        selected_toggle_string = f"\n[Shift+click] Toggle visibility of selected layer(s){selected_key_text}"

        string = f"{show_all_string}{selected_toggle_string}"
        self.dock.set_btn_reset_shortcut(string)

    def set_chk_lockgroups_shortcut(self):
        if not self.dock: return

        keys = self.shortcut_toggle_lock.shortcut()
        string = f" ({keys.toString()})" if keys else ''
        self.dock.set_chk_lockgroups_shortcut(string)

    def set_chk_avgrasters_shortcut(self):
        if not self.dock: return

        keys = self.shortcut_toggle_avgrasters.shortcut()
        string = f" ({keys.toString()})" if keys else ''
        self.dock.set_chk_avgrasters_shortcut(string)

    def set_chk_avgdistinct_shortcut(self):
        if not self.dock: return

        keys = self.shortcut_toggle_avgdistinct.shortcut()
        string = f" ({keys.toString()})" if keys else ''
        self.dock.set_chk_avgdistinct_shortcut(string)

    def set_precalc_shortcut(self):
        if not self.dock: return

        keys = self.shortcut_precalc_all.shortcut()
        string = f" ({keys.toString()})" if keys else ''
        self.dock.set_precalc_shortcut(string)

    def set_export_shortcut(self):
        if not self.dock: return

        keys = self.shortcut_export_to_directory.shortcut()
        string = f" ({keys.toString()})" if keys else ''
        self.dock.set_export_shortcut(string)

    def set_settings_shortcut(self):
        if not self.dock: return

        keys = self.shortcut_settings.shortcut()
        string = f" ({keys.toString()})" if keys else ''
        self.dock.set_settings_shortcut(string)

    def toggle_avgrasters(self):
        if not self.dock or not self.dock.isVisible():
            return
        self.dock.toggle_avgrasters()

    def toggle_avgdistinct(self):
        if not self.dock or not self.dock.isVisible():
            return
        self.dock.toggle_avgdistinct()

    def trigger_precalc_all(self):
        if not self.dock or not self.dock.isVisible():
            return
        self.dock.trigger_precalc_all()

    def trigger_export_to_directory(self):
        if not self.dock or not self.dock.isVisible():
            return
        self.dock.trigger_export_to_directory()

    def show_settings(self):
        if not self.dock:
            self.init_dock()
            self.iface.addDockWidget(GlobalSettings.getWidgetLocation(), self.dock)
        self.dock.show_settings()

    def toggle_show_all(self):
        if not self.dock: return
        self.dock.toggle_show_all()

    def toggle_show_selected(self):
        if not self.dock: return
        self.dock.toggle_show_selected()
