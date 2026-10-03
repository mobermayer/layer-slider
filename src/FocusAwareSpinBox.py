from qgis.PyQt.QtCore import QEvent, Qt
from qgis.PyQt.QtWidgets import QSpinBox, QStyle, QStyleOptionSpinBox, QWidget


class FocusAwareSpinBox(QSpinBox):
    @classmethod
    def replace_spinbox(cls, old: QSpinBox) -> "FocusAwareSpinBox":
        """Replace a Designer spin box while preserving its configuration."""
        parent: QWidget = old.parentWidget()
        layout = parent.layout() if parent else None

        new_spin = cls(parent)
        new_spin.setRange(old.minimum(), old.maximum())
        new_spin.setValue(old.value())
        new_spin.setSingleStep(old.singleStep())
        new_spin.setEnabled(old.isEnabled())
        new_spin.setObjectName(old.objectName())
        new_spin.setToolTip(old.toolTip())
        new_spin.setPrefix(old.prefix())
        new_spin.setSuffix(old.suffix())
        new_spin.setWrapping(old.wrapping())
        new_spin.setKeyboardTracking(old.keyboardTracking())
        new_spin.setButtonSymbols(old.buttonSymbols())
        new_spin.setAccelerated(old.isAccelerated())
        new_spin.setDisplayIntegerBase(old.displayIntegerBase())
        new_spin.setSpecialValueText(old.specialValueText())
        new_spin.setSizePolicy(old.sizePolicy())
        new_spin.setMinimumSize(old.minimumSize())
        new_spin.setMaximumSize(old.maximumSize())
        new_spin.setFocusPolicy(old.focusPolicy())
        new_spin.setAlignment(old.alignment())

        if layout:
            index = layout.indexOf(old)
            layout.removeWidget(old)
            layout.insertWidget(index, new_spin)

        old.deleteLater()
        return new_spin

    def event(self, event):
        if self._is_non_numeric_shortcut_override(event):
            # Let QGIS shortcuts handle letters that the integer editor cannot accept.
            event.ignore()
            return True
        if self._is_step_button_mouse_event(event):
            self.clearFocus()
        return super().event(event)

    def stepBy(self, steps: int) -> None:
        super().stepBy(steps)
        self.lineEdit().deselect()

    def _is_non_numeric_shortcut_override(self, event) -> bool:
        if event.type() != QEvent.Type.ShortcutOverride:
            return False
        text = event.text()
        if not text:
            return False
        shortcut_modifiers = (
            Qt.KeyboardModifier.ControlModifier,
            Qt.KeyboardModifier.AltModifier,
            Qt.KeyboardModifier.MetaModifier,
        )
        if any(event.modifiers() & modifier for modifier in shortcut_modifiers):
            return False
        if any(not char.isprintable() for char in text):
            return False
        return any(not char.isdigit() and char not in "+-" for char in text)

    def _is_step_button_mouse_event(self, event) -> bool:
        if event.type() not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False

        option = QStyleOptionSpinBox()
        self.initStyleOption(option)
        position = event.pos() if hasattr(event, "pos") else event.position().toPoint()
        sub_control = self.style().hitTestComplexControl(
            QStyle.ComplexControl.CC_SpinBox,
            option,
            position,
            self,
        )
        return sub_control in (
            QStyle.SubControl.SC_SpinBoxUp,
            QStyle.SubControl.SC_SpinBoxDown,
        )
