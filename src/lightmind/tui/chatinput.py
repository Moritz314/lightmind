"""The chat's input line: a TextArea that wraps long text, grows from one to eight rows, and sends on Enter.

A new line is Ctrl+J, Ctrl+Space (terminals report it as "ctrl+space" or "ctrl+@") or, where the terminal can tell,
Shift+Enter.  `value` and `cursor_position` (an offset into the text) keep the names of the Input this replaced."""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.actions import SkipAction
from textual.geometry import Size
from textual.message import Message
from textual.widgets import TextArea

from .theme import WORD, word_spans

MAX_ROWS = 8
NEWLINE = ("ctrl+j", "shift+enter", "ctrl+space", "ctrl+@")


class ChatInput(TextArea):
    class Submitted(Message):
        def __init__(self, input: ChatInput, value: str):
            super().__init__()
            self.input, self.value = input, value

        @property
        def control(self) -> ChatInput:
            return self.input

    def __init__(self, placeholder: str = "", id: str | None = None):
        super().__init__("", soft_wrap=True, show_line_numbers=False, highlight_cursor_line=False,
                         tab_behavior="focus", placeholder=placeholder, id=id)

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, text: str):
        self.load_text(text)
        self.cursor_position = len(text)

    @property
    def cursor_position(self) -> int:
        return self.document.get_index_from_location(self.cursor_location)

    @cursor_position.setter
    def cursor_position(self, pos: int):
        self.cursor_location = self.document.get_location_from_index(max(0, min(pos, len(self.text))))

    def get_line(self, line_index: int) -> Text:
        """The text of one line with the word `ultracode` coloured; only styles are added, so the cursor, the selection and the
        wrapping work on the same text as before.  `line_index`: the line of the document."""
        line = super().get_line(line_index)
        for start, end in word_spans(self, line.plain):
            line.stylize(WORD, start, end)
        return line

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return max(1, min(MAX_ROWS, self.wrapped_document.height))

    def _on_text_area_changed(self, _: TextArea.Changed):
        self.refresh(layout=True)                               # the height follows the text

    async def _on_key(self, event: events.Key):
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self, self.text))
        elif event.key in NEWLINE and not self.read_only:
            event.stop()
            event.prevent_default()
            self._replace_via_keyboard("\n", *self.selection)

    def _skip_for_popup(self):
        if getattr(self.screen, "pop", None):                    # the slash popup's list takes the arrows
            raise SkipAction()

    def action_cursor_up(self, select: bool = False):
        self._skip_for_popup()
        super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False):
        self._skip_for_popup()
        super().action_cursor_down(select)
