import os

from qgis.PyQt.QtCore import Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QCloseEvent, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStyle,
)
from qgis.PyQt import uic

from .GlobalSettings import GlobalSettings

FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), "SettingsDialogBase.ui")
)


class SettingsDialog(QDialog, FORM_CLASS):
    settings_changed = pyqtSignal()
    remove_all_composed_layers_requested = pyqtSignal()

    radio_dynamic: QRadioButton
    spin_dynamic_n: QSpinBox
    radio_constant: QRadioButton
    spin_constant: QSpinBox
    combo_output_datatype: QComboBox
    edit_cache_dir: QLineEdit
    btn_select_cache_dir: QPushButton
    btn_open_cache_dir: QPushButton
    spin_cache_mb: QSpinBox
    btn_remove_all_composed_layers: QPushButton
    button_box: QDialogButtonBox

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False
        self.setupUi(self)

        self.setWindowModality(Qt.NonModal)
        self.setAttribute(Qt.WA_DeleteOnClose, False)

        self.edit_cache_dir.setPlaceholderText(GlobalSettings.DEFAULT_CACHE_DIR)
        self.btn_open_cache_dir.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))

        for key, label in GlobalSettings.getComposeOutputDatatypeOptions():
            self.combo_output_datatype.addItem(label, key)

        self.radio_dynamic.toggled.connect(self._on_contrast_mode_toggled)
        self.radio_constant.toggled.connect(self._on_contrast_mode_toggled)
        self.btn_select_cache_dir.clicked.connect(self._on_select_cache_dir_clicked)
        self.btn_open_cache_dir.clicked.connect(self._on_open_cache_dir_clicked)
        self.button_box.accepted.connect(self._on_save_clicked)
        self.button_box.rejected.connect(self._on_cancel_clicked)
        self.btn_remove_all_composed_layers.clicked.connect(self._on_remove_all_composed_layers_clicked)

        self.refresh_from_settings()

    def refresh_from_settings(self):
        self._loading = True
        try:
            mode = GlobalSettings.getComposeContrastMode()
            self.radio_dynamic.setChecked(mode == "auto")
            self.radio_constant.setChecked(mode == "constant")
            self.spin_dynamic_n.setValue(GlobalSettings.getComposeContrastN())
            self.spin_constant.setValue(GlobalSettings.getComposeContrastValue())
            self._sync_contrast_inputs()

            idx = self.combo_output_datatype.findData(GlobalSettings.getComposeOutputDatatype())
            self.combo_output_datatype.setCurrentIndex(idx if idx >= 0 else 0)
            self.edit_cache_dir.setText(GlobalSettings.getCacheDirectory())
            self.spin_cache_mb.setValue(GlobalSettings.getMaxCacheMB())
        finally:
            self._loading = False

    def closeEvent(self, event: QCloseEvent | None):
        self.refresh_from_settings()
        self.hide()
        if event:
            event.ignore()

    def _sync_contrast_inputs(self):
        is_dynamic = self.radio_dynamic.isChecked()
        self.spin_dynamic_n.setEnabled(is_dynamic)
        self.spin_constant.setEnabled(not is_dynamic)

    def _normalize_cache_dir(self, value: str) -> str:
        normalized = value.strip() if isinstance(value, str) else ""
        if not normalized:
            return GlobalSettings.DEFAULT_CACHE_DIR
        return os.path.abspath(os.path.expanduser(normalized))

    def _collect_form_values(self) -> dict:
        return {
            "compose_contrast_mode": "auto" if self.radio_dynamic.isChecked() else "constant",
            "compose_contrast_n": int(self.spin_dynamic_n.value()),
            "compose_contrast_value": int(self.spin_constant.value()),
            "compose_output_datatype": self.combo_output_datatype.currentData(),
            "cache_directory": self._normalize_cache_dir(self.edit_cache_dir.text()),
            "max_cache_mb": int(self.spin_cache_mb.value()),
        }

    def _collect_saved_values(self) -> dict:
        return {
            "compose_contrast_mode": GlobalSettings.getComposeContrastMode(),
            "compose_contrast_n": int(GlobalSettings.getComposeContrastN()),
            "compose_contrast_value": int(GlobalSettings.getComposeContrastValue()),
            "compose_output_datatype": GlobalSettings.getComposeOutputDatatype(),
            "cache_directory": self._normalize_cache_dir(GlobalSettings.getCacheDirectory()),
            "max_cache_mb": int(GlobalSettings.getMaxCacheMB()),
        }

    def _on_contrast_mode_toggled(self, _checked: bool):
        self._sync_contrast_inputs()

    def _on_select_cache_dir_clicked(self):
        start_dir = self._normalize_cache_dir(self.edit_cache_dir.text())
        selected = QFileDialog.getExistingDirectory(self, "Select Cache Directory", start_dir)
        if not selected:
            return
        self.edit_cache_dir.setText(self._normalize_cache_dir(selected))

    def _on_open_cache_dir_clicked(self):
        cache_dir = self._normalize_cache_dir(self.edit_cache_dir.text())
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except Exception:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(cache_dir))

    def _on_save_clicked(self):
        if self._loading:
            return

        current_values = self._collect_form_values()
        saved_values = self._collect_saved_values()
        if current_values == saved_values:
            self.hide()
            return

        GlobalSettings.setComposeContrastMode(current_values["compose_contrast_mode"])
        GlobalSettings.setComposeContrastN(current_values["compose_contrast_n"])
        GlobalSettings.setComposeContrastValue(current_values["compose_contrast_value"])
        GlobalSettings.setComposeOutputDatatype(current_values["compose_output_datatype"])
        GlobalSettings.setCacheDirectory(current_values["cache_directory"])
        GlobalSettings.setMaxCacheMB(current_values["max_cache_mb"])
        self.settings_changed.emit()
        self.hide()

    def _on_cancel_clicked(self):
        self.refresh_from_settings()
        self.hide()

    def _on_remove_all_composed_layers_clicked(self):
        reply = QMessageBox.question(
            self,
            "Remove composed layers",
            "Remove all plugin-managed composed raster layers from the project?\n\n"
            "Layers duplicated in the legend (copies) are not removed.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.remove_all_composed_layers_requested.emit()
