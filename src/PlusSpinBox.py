import typing

from .FocusAwareSpinBox import FocusAwareSpinBox


class PlusSpinBox(FocusAwareSpinBox):
    def textFromValue(self, v: int) -> str:
        return f"{v:+d}"

    def valueFromText(self, text: typing.Optional[str]) -> int:
        return int(text) if text else 0
