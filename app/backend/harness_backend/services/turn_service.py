"""Loop turns: short list (for `/state`), detail of one turn from its snapshot (`/turn/<n>`) and turns without progress."""
import json

from ..repositories.loop_file_repository import loop_state
from ..repositories.loop_read_formatting import ms_to_seconds, redact_credentials, to_number, to_text_list
from .claude_account_service import build_turn_claude

# same scoreboard as run_loop.RANK: counts turns without progress from the snapshots
POINTS_BY_STATUS = {"pending": 0, "speccing": 1, "specced": 2, "approved": 3, "building": 4, "verified": 6, "blocked": 0}
TURN_RESULT_LIMIT = 20000
PERMISSION_DENIAL_LIMIT = 300
TURN_STATUS_RUNNING = "running"
TURN_STATUS_ERROR = "error"


def get_turn_status(turn_record, snapshot_loop):
    """Turn status: the one from the snapshot's LOOP.json; without it, `error` when claude -p failed; else None."""
    if snapshot_loop.get("status") in loop_state.LOOP_STATUS:
        return snapshot_loop["status"]
    if turn_record.get("is_error") or turn_record.get("timeout") or turn_record.get("exit_code") not in (None, 0):
        return TURN_STATUS_ERROR
    return None


class TurnService:
    def __init__(self, loop_file_repository, roadmap_view_service):
        self.loop_file_repository = loop_file_repository
        self.roadmap_view_service = roadmap_view_service

    def list_turns(self, work_dir, running_number):
        rows = []
        for number in self.loop_file_repository.list_turn_numbers(work_dir):
            record = self.loop_file_repository.read_turn(work_dir, number)
            snapshot = self.loop_file_repository.load_snapshot(work_dir, number)
            snapshot_loop = self.loop_file_repository.read_snapshot_loop_json(snapshot)
            previous = self.loop_file_repository.load_snapshot(work_dir, number - 1) if number > 0 else None
            commits = None
            if snapshot and previous and snapshot.get("head") and previous.get("head"):
                commits = self.loop_file_repository.count_commits_between(work_dir, previous["head"], snapshot["head"])
            status = TURN_STATUS_RUNNING if number == running_number else get_turn_status(record, snapshot_loop)
            rows.append({"n": number, "cost_usd": to_number(record.get("total_cost_usd")),
                         "duration_s": ms_to_seconds(record.get("duration_ms")), "status": status,
                         "feature": to_text_list(snapshot_loop.get("feature")), "commits": commits, "has_snapshot": bool(snapshot),
                         **build_turn_claude(record)})
        return rows

    def count_turns_without_progress(self, work_dir):
        """Consecutive turns, up to the last one, in which the ROADMAP score did not go up (same count as run_loop, redone from
        the snapshots)."""
        scores = []
        for number in range(0, (self.loop_file_repository.list_turn_numbers(work_dir) or [0])[-1] + 1):
            snapshot = self.loop_file_repository.load_snapshot(work_dir, number)
            if not snapshot:
                continue
            parsed, _ = loop_state.parse_roadmap(self.loop_file_repository.read_snapshot_roadmap(snapshot))
            scores.append(sum(POINTS_BY_STATUS.get(feature["status"], 0) for feature in parsed.values()))
        without_progress = 0
        for index in range(len(scores) - 1, 0, -1):
            if scores[index] > scores[index - 1]:
                break
            without_progress += 1
        return without_progress

    def get_turn_detail(self, context, number):
        """{"n","turn_log","roadmap","features"} from the turn snapshot; None without a snapshot."""
        work_dir = context.work_dir
        snapshot = self.loop_file_repository.load_snapshot(work_dir, number)
        if not snapshot:
            return None
        turn_log = self.loop_file_repository.read_turn(work_dir, number)
        turn_log.pop("raw", None)
        if "result" in turn_log:
            turn_log["result"] = redact_credentials(turn_log["result"], TURN_RESULT_LIMIT)
        if isinstance(turn_log.get("permission_denials"), list):
            turn_log["permission_denials"] = [redact_credentials(json.dumps(item, ensure_ascii=False), PERMISSION_DENIAL_LIMIT)
                                              for item in turn_log["permission_denials"]]
        roadmap = self.loop_file_repository.read_snapshot_roadmap(snapshot)
        return {"n": number, "turn_log": turn_log, "roadmap": roadmap,
                "features": self.roadmap_view_service.build_roadmap_view(work_dir, roadmap, number)[1]}
