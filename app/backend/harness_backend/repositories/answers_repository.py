"""The loop's ANSWERS.md: the panel only APPENDS `[pending]` blocks (user note/instruction); the next turn applies them.

Same format as the engine's `write_answer`: `\\n## Answer <iso> [pending]\\nQuestion: ...\\nUser answer: ...\\n`.
"""
import os
from typing import Protocol

from .loop_command_file_repository import loop_state, now_iso

ANSWERS_FILE_NAME = "ANSWERS.md"
USER_NOTE_QUESTION = "(user note from the panel)"
USER_INSTRUCTION_QUESTION = "(user instruction from the panel)"
PENDING_ANSWER_BLOCK = "\n## Answer %s [pending]\nQuestion: %s\nUser answer: %s\n"


class IAnswersRepository(Protocol):
    def append_pending_answer(self, work_dir, question, text) -> None: ...


class AnswersRepository:
    def append_pending_answer(self, work_dir, question, text):
        path = loop_state.state_file(work_dir, ANSWERS_FILE_NAME)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        block = PENDING_ANSWER_BLOCK % (now_iso(), question, text.strip())
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(block)
