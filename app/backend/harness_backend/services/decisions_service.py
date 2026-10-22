"""The AI decisions of a project (`.specs/project/loop/VALIDATE.md`, grammar owned by `engine/decisions.py`); the engine file is never edited here.

What the user did with a decision (reviewed, asked for a change) lives in `internal/decisions-review.json`
(`{"items": {"<id>": {"reviewed", "reviewed_at", "change_requested"}}}`, the id is the hash of the raw line). "Ask for a change" also adds
the id to the follow-up draft (`internal/follow-up-draft.json`, key `decisions`; the `text` key belongs to the follow-up service, so this
service reads, merges and never clobbers it).
"""
import os
import threading

from ..engine import decisions as decisions_engine
from ..repositories.loop_command_file_repository import get_loop_internal_dir, now_iso, read_json_object, write_json_atomic

REVIEW_FILE_NAME = "decisions-review.json"
FOLLOW_UP_DRAFT_FILE_NAME = "follow-up-draft.json"
LEVEL_CRITICAL = "critical"
LEVEL_IMPORTANT = "important"


class DecisionNotFoundError(LookupError):
    """No decision with this id in VALIDATE.md (404)."""


class DecisionsService:
    def __init__(self):
        self.write_lock = threading.Lock()

    # ------------------------------------------------ paths
    @staticmethod
    def review_path(work_dir):
        return os.path.join(get_loop_internal_dir(work_dir), REVIEW_FILE_NAME)

    @staticmethod
    def draft_path(work_dir):
        return os.path.join(get_loop_internal_dir(work_dir), FOLLOW_UP_DRAFT_FILE_NAME)

    def read_review_items(self, work_dir):
        items = read_json_object(self.review_path(work_dir)).get("items")
        return items if isinstance(items, dict) else {}

    # ------------------------------------------------ read
    def read_decisions(self, context):
        """(items with the review state merged, unparsed lines) of the work folder, in file order."""
        parsed, unparsed = decisions_engine.parse(decisions_engine.read_file(context.work_dir))
        reviews = self.read_review_items(context.work_dir)
        items = []
        for decision in parsed:
            state = reviews.get(decision["id"])
            state = state if isinstance(state, dict) else {}
            items.append({"id": decision["id"], "slug": decision["slug"], "turn": decision["turn"], "level": decision["level"],
                          "doubt": decision["doubt"], "chose": decision["chose"], "revert": decision["revert"],
                          "reviewed": bool(state.get("reviewed")), "reviewed_at": state.get("reviewed_at"),
                          "change_requested": bool(state.get("change_requested"))})
        return items, unparsed

    @staticmethod
    def count_open(items):
        """Open = not marked as reviewed yet."""
        open_items = [item for item in items if not item["reviewed"]]
        return {"open_critical": sum(1 for item in open_items if item["level"] == LEVEL_CRITICAL),
                "open_important": sum(1 for item in open_items if item["level"] == LEVEL_IMPORTANT)}

    def list_decisions(self, context, turn=None):
        """`GET decisions[?turn=N]`. The counts always cover the whole loop, not only the filtered turn."""
        items, unparsed = self.read_decisions(context)
        counts = self.count_open(items)
        if turn is not None:
            items = [item for item in items if item["turn"] == turn]
        return {"items": items, "unparsed": unparsed, "counts": counts}

    def open_counts(self, context):
        """{open_critical, open_important}: consumed by the panel state and by the wrap-up."""
        return self.count_open(self.read_decisions(context)[0])

    def unreviewed_critical(self, context):
        """Critical decisions the user has not marked as reviewed (they block Finish)."""
        return [item for item in self.read_decisions(context)[0] if item["level"] == LEVEL_CRITICAL and not item["reviewed"]]

    # ------------------------------------------------ write
    def ensure_decision_exists(self, context, decision_id):
        items, unparsed = self.read_decisions(context)
        if not any(item["id"] == decision_id for item in items) and not any(row["id"] == decision_id for row in unparsed):
            raise DecisionNotFoundError("decision not found")

    def update_review(self, work_dir, decision_id, **fields):
        data = read_json_object(self.review_path(work_dir))
        items = data.get("items") if isinstance(data.get("items"), dict) else {}
        current = items.get(decision_id) if isinstance(items.get(decision_id), dict) else {}
        merged = {"reviewed": False, "reviewed_at": None, "change_requested": False}
        merged.update(current)
        merged.update(fields)
        items[decision_id] = merged
        data["items"] = items
        write_json_atomic(self.review_path(work_dir), data)

    def mark_reviewed(self, context, decision_id):
        self.ensure_decision_exists(context, decision_id)
        with self.write_lock:
            self.update_review(context.work_dir, decision_id, reviewed=True, reviewed_at=now_iso())
        return {"ok": True}

    def request_change(self, context, decision_id):
        """Adds the id to the follow-up draft (no duplicates, `text` kept) and flags the decision; returns the draft."""
        self.ensure_decision_exists(context, decision_id)
        with self.write_lock:
            self.update_review(context.work_dir, decision_id, change_requested=True)
            draft = read_json_object(self.draft_path(context.work_dir))
            chosen = draft.get("decisions") if isinstance(draft.get("decisions"), list) else []
            if decision_id not in chosen:
                chosen.append(decision_id)
            draft["decisions"] = chosen
            if not isinstance(draft.get("text"), str):
                draft["text"] = ""
            write_json_atomic(self.draft_path(context.work_dir), draft)
        return {"ok": True, "draft": draft}
