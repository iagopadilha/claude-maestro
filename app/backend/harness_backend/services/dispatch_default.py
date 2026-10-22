"""Default dispatch (`wave`) of a new loop: ALWAYS 3 features per turn unless the user chose another number.

The number is how many features (lanes) move in parallel in a turn, not a promise: each lane is one feature with its OWN agent at every
step (spec-writer, spec-reviewer, executor, verifier) and advances by itself (`loop_state.next_work`). The scheduler still fills execution
only with features whose dependencies are verified and whose `paths=` do not overlap, because running those together would break the
code. A turn with fewer than 3 ready features simply runs the ones that are ready.
"""
MIN_WAVE, MAX_WAVE = 1, 8
DEFAULT_WAVE = 3


def compute_default_dispatch(roadmap_text="", cap=DEFAULT_WAVE, primary_name=""):
    """{wave, auto: True, reason, stage}. `roadmap_text`, `cap` and `primary_name` are kept for the callers; the answer no longer depends on them."""
    wave = max(MIN_WAVE, min(MAX_WAVE, cap))
    return {"wave": wave, "auto": True, "stage": "",
            "reason": "%d features per turn, always, each one with its own agents at every step; dependent features and features sharing files wait for the next turn" % wave}
