import os

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QWidget,
)
from qgis.PyQt import uic

from .GlobalSettings import GlobalSettings

FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), "ExportDialogBase.ui")
)


class ExportDialog(QDialog, FORM_CLASS):
    DESTINATION_DIRECTORY = "directory"
    DESTINATION_FILE = "file"
    WINDOW_TITLE_PREFIX = "Layer Slider - "

    mode_row_widget: QWidget
    destination_mode_input: QComboBox
    destination_label: QLabel
    destination_input: QLineEdit
    btn_browse: QPushButton
    add_to_qgis_checkbox: QCheckBox
    button_box: QDialogButtonBox

    def __init__(
        self,
        parent=None,
        *,
        window_title: str = "Export",
        destination_label: str = "Output directory:",
        allow_file_destination: bool = False,
        initial_destination_mode: str = DESTINATION_DIRECTORY,
        fixed_destination_mode: str | None = None,
        initial_destination_path: str = "",
        default_file_name: str = "export.tif",
        initial_add_to_qgis: bool | None = None,
        persist_to_precalc_settings: bool = True,
        minimum_width: int = 200,
    ):
        super().__init__(parent)
        self.setupUi(self)

        self.setWindowTitle(self._with_window_prefix(window_title))
        self.setMinimumWidth(max(480, int(minimum_width)))

        self._default_destination_label = destination_label
        self._fixed_destination_mode = (
            fixed_destination_mode
            if fixed_destination_mode in {self.DESTINATION_DIRECTORY, self.DESTINATION_FILE}
            else None
        )
        self.allow_file_destination = bool(allow_file_destination) or (
            self._fixed_destination_mode == self.DESTINATION_FILE
        )
        self.default_file_name = default_file_name.strip() or "export.tif"
        self.persist_to_precalc_settings = bool(persist_to_precalc_settings)

        if self.allow_file_destination and self._fixed_destination_mode is None:
            self.destination_mode_input.addItem("Directory", self.DESTINATION_DIRECTORY)
            self.destination_mode_input.addItem("File", self.DESTINATION_FILE)
            self.destination_mode_input.currentIndexChanged.connect(self._on_destination_mode_changed)
        else:
            self.mode_row_widget.setVisible(False)

        self.destination_label.setText(destination_label)

        default_add_to_qgis = (
            GlobalSettings.getExportAddToQgis()
            if initial_add_to_qgis is None
            else bool(initial_add_to_qgis)
        )
        self.add_to_qgis_checkbox.setChecked(default_add_to_qgis)

        self.btn_browse.clicked.connect(self._browse_destination)
        self.button_box.accepted.connect(self._accept_if_valid)
        self.button_box.rejected.connect(self.reject)

        initial_mode = self._normalize_initial_mode(initial_destination_mode)
        if self.mode_row_widget.isVisible():
            idx = self.destination_mode_input.findData(initial_mode)
            self.destination_mode_input.setCurrentIndex(idx if idx >= 0 else 0)

        initial_path = initial_destination_path.strip() or GlobalSettings.getPrecalcExportDirectory()
        if self.selected_destination_mode() == self.DESTINATION_FILE and os.path.isdir(initial_path):
            initial_path = os.path.join(initial_path, self.default_file_name)
        self.destination_input.setText(initial_path)
        self._sync_destination_ui()

    def _with_window_prefix(self, title: str) -> str:
        normalized = (title or "").strip() or "Export"
        if normalized.startswith(self.WINDOW_TITLE_PREFIX):
            return normalized
        return f"{self.WINDOW_TITLE_PREFIX}{normalized}"

    def _normalize_initial_mode(self, mode: str) -> str:
        if self._fixed_destination_mode is not None:
            return self._fixed_destination_mode
        if mode in {self.DESTINATION_DIRECTORY, self.DESTINATION_FILE}:
            if mode == self.DESTINATION_FILE and not self.allow_file_destination:
                return self.DESTINATION_DIRECTORY
            return mode
        return self.DESTINATION_DIRECTORY

    def _sync_destination_ui(self):
        mode = self.selected_destination_mode()
        if mode == self.DESTINATION_FILE:
            self.destination_label.setText("Output file:")
            self.destination_input.setPlaceholderText("Select an output file")
        else:
            self.destination_label.setText(self._default_destination_label)
            self.destination_input.setPlaceholderText("Select an output directory")

    def _on_destination_mode_changed(self, _index: int):
        current_value = self.selected_destination()
        mode = self.selected_destination_mode()
        if mode == self.DESTINATION_FILE and current_value and os.path.isdir(current_value):
            self.destination_input.setText(os.path.join(current_value, self.default_file_name))
        elif mode == self.DESTINATION_DIRECTORY and current_value and not os.path.isdir(current_value):
            parent_dir = os.path.dirname(current_value)
            if parent_dir:
                self.destination_input.setText(parent_dir)
        self._sync_destination_ui()

    def _browse_destination(self):
        mode = self.selected_destination_mode()
        if mode == self.DESTINATION_FILE:
            selected_path, _selected_filter = QFileDialog.getSaveFileName(
                self,
                "Select output file",
                self._default_save_file_path(),
                "GeoTIFF (*.tif *.tiff);;All files (*)",
            )
            if selected_path:
                self.destination_input.setText(selected_path)
            return

        start_dir = self._default_start_directory()
        selected_dir = QFileDialog.getExistingDirectory(
            self,
            "Select output directory",
            start_dir,
            QFileDialog.ShowDirsOnly,
        )
        if selected_dir:
            self.destination_input.setText(selected_dir)

    def _default_start_directory(self) -> str:
        current_value = self.selected_destination()
        if os.path.isdir(current_value):
            return current_value
        if current_value:
            parent_dir = os.path.dirname(current_value)
            if os.path.isdir(parent_dir):
                return parent_dir
        return GlobalSettings.getPrecalcExportDirectory()

    def _default_save_file_path(self) -> str:
        current_value = self.selected_destination()
        if current_value:
            if os.path.isdir(current_value):
                return os.path.join(current_value, self.default_file_name)
            return current_value
        return os.path.join(self._default_start_directory(), self.default_file_name)

    def _accept_if_valid(self):
        destination = self.selected_destination()
        if not destination:
            QMessageBox.warning(self, "Missing destination", "Please select an output destination.")
            return

        destination = os.path.abspath(os.path.expanduser(destination))
        mode = self.selected_destination_mode()
        if mode == self.DESTINATION_DIRECTORY:
            try:
                os.makedirs(destination, exist_ok=True)
            except Exception:
                QMessageBox.warning(
                    self,
                    "Invalid directory",
                    "Selected output directory does not exist and could not be created.",
                )
                return
            if not os.path.isdir(destination):
                QMessageBox.warning(
                    self,
                    "Invalid directory",
                    "Selected output directory does not exist and could not be created.",
                )
                return
        else:
            if os.path.isdir(destination):
                QMessageBox.warning(self, "Invalid file", "Selected output file points to a directory.")
                return
            parent_dir = os.path.dirname(destination) or "."
            try:
                os.makedirs(parent_dir, exist_ok=True)
            except Exception:
                QMessageBox.warning(
                    self,
                    "Invalid directory",
                    "Output file directory does not exist and could not be created.",
                )
                return
            if not os.path.isdir(parent_dir):
                QMessageBox.warning(
                    self,
                    "Invalid directory",
                    "Output file directory does not exist and could not be created.",
                )
                return

        self.destination_input.setText(destination)
        if self.persist_to_precalc_settings:
            GlobalSettings.setPrecalcExportDirectory(self.selected_directory())
            GlobalSettings.setExportAddToQgis(self.add_to_qgis())
        self.accept()

    def selected_destination_mode(self) -> str:
        if self._fixed_destination_mode is not None:
            return self._fixed_destination_mode
        if not self.mode_row_widget.isVisible():
            return self.DESTINATION_DIRECTORY
        mode = self.destination_mode_input.currentData()
        if mode in {self.DESTINATION_DIRECTORY, self.DESTINATION_FILE}:
            return mode
        return self.DESTINATION_DIRECTORY

    def selected_destination(self) -> str:
        return self.destination_input.text().strip()

    def selected_directory(self) -> str:
        destination = self.selected_destination()
        if self.selected_destination_mode() == self.DESTINATION_FILE:
            return os.path.dirname(destination)
        return destination

    def selected_file_path(self) -> str:
        if self.selected_destination_mode() == self.DESTINATION_FILE:
            return self.selected_destination()
        return ""

    def add_to_qgis(self) -> bool:
        return self.add_to_qgis_checkbox.isChecked()
