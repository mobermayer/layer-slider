import typing
from qgis.PyQt.QtWidgets import QSpinBox, QWidget


class PlusSpinBox(QSpinBox):
    def textFromValue(self, v: int) -> str:
        return f"{v:+d}"

    def valueFromText(self, text: typing.Optional[str]) -> int:
        return int(text) if text else 0

    @classmethod
    def replace_spinbox(cls, old: QSpinBox) -> "PlusSpinBox":
        """
        Replace an existing QSpinBox with a PlusSpinBox in-place,
        keeping its value, range, step, enabled state, size constraints, and parent layout.
        Returns the new PlusSpinBox instance.
        """
        parent: QWidget = old.parentWidget()
        layout = old.parentWidget().layout() if old.parentWidget() else None

        # Create the new PlusSpinBox
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
        new_spin.setSizePolicy(old.sizePolicy())
        new_spin.setMinimumSize(old.minimumSize())
        new_spin.setMaximumSize(old.maximumSize())
        new_spin.setFocusPolicy(old.focusPolicy())
        new_spin.setAlignment(old.alignment())

        # Replace widget in layout if possible
        if layout:
            index = layout.indexOf(old)
            layout.removeWidget(old)
            layout.insertWidget(index, new_spin)

        # Remove the old widget
        old.deleteLater()

        return new_spin
