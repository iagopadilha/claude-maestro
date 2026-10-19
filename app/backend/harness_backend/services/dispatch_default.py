"""Default dispatch (`wave`) of a new loop, computed from the approved ROADMAP when the user does not choose one.

Same rules as the engine scheduler (`loop_state.pick_wave`): features of the FIRST stage that still has open work, whose dependencies
are already verified (or are open features of the same stage that do not depend on each other), with disjoint `paths=`. The result is
capped to the loop range and to a conservative default cap; anything unclear falls back to 1.
"""
from ..repositories.loop_file_repository import loop_state

MIN_WAVE, MAX_WAVE = 1, 8
DEFAULT_WAVE_CAP = 3
CLOSED = ("verified", "blocked")


def compute_default_dispatch(roadmap_text, cap=DEFAULT_WAVE_CAP, primary_name=""):
    """{wave, auto: True, reason}. Never raises: an unreadable ROADMAP is just `wave 1`."""
    try:
        features, errors = loop_state.parse_roadmap(roadmap_text or "")
    except Exception:  # noqa: BLE001 - the default must never break the check
        return build("1", 1, "ROADMAP could not be read")
    open_features = {slug: f for slug, f in features.items() if f["status"] not in CLOSED}
    if errors or not open_features:
        return build("1", 1, "no open features in the ROADMAP" if not errors else "ROADMAP has errors")
    stage = next(iter(open_features.values())).get("stage", "")
    stage_features = {slug: f for slug, f in open_features.items() if f.get("stage", "") == stage}
    first_layer = [slug for slug in stage_features if loop_state.ready(features, slug)]  # every dependency verified
    if any(not stage_features[slug].get("paths") for slug in first_layer) and len(first_layer) > 1:
        return build(stage, 1, "some features have no paths=, running one at a time")
    # the greedy pick is the engine's own: a feature that overlaps or must run alone is left for the next turn
    candidates = [slug for slug in first_layer if stage_features[slug].get("paths")]
    chosen = loop_state.pick_wave(features, candidates, MAX_WAVE, headless=True, primary_name=primary_name)
    independent = max(1, len(chosen))
    limit = max(MIN_WAVE, min(MAX_WAVE, cap))
    wave = min(independent, limit)
    if len(first_layer) <= 1:
        return build(stage, 1, "1 feature ready in stage %s" % stage)
    if wave == 1:
        return build(stage, 1, "features in stage %s depend on each other or share paths" % stage)
    if independent > limit:
        return build(stage, wave, "%d independent features in stage %s, capped at %d" % (independent, stage, limit))
    return build(stage, wave, "%d independent features in stage %s" % (wave, stage))


def build(stage, wave, reason):
    return {"wave": wave, "auto": True, "reason": reason, "stage": stage}
