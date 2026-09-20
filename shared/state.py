from enum import StrEnum


class Stage(StrEnum):
    NEW = "NEW"
    ANALYZING_SOURCE = "ANALYZING_SOURCE"
    WAITING_FOR_TRACK_SELECTION = "WAITING_FOR_TRACK_SELECTION"
    PREPARING_TRACKS = "PREPARING_TRACKS"
    RUNNING_CRF_ANALYSIS = "RUNNING_CRF_ANALYSIS"
    WAITING_FOR_ENCODE_SELECTION = "WAITING_FOR_ENCODE_SELECTION"
    ENCODING = "ENCODING"
    VALIDATING_ENCODE = "VALIDATING_ENCODE"
    REMUXING = "REMUXING"
    SCREENSHOT_CANDIDATE_GENERATION = "SCREENSHOT_CANDIDATE_GENERATION"
    SCREENSHOT_AGENT_SELECTION = "SCREENSHOT_AGENT_SELECTION"
    WAITING_FOR_SCREENSHOT_SELECTION = "WAITING_FOR_SCREENSHOT_SELECTION"
    SCREENSHOT_RENDERING = "SCREENSHOT_RENDERING"
    WAITING_FOR_RELEASE_DETAILS = "WAITING_FOR_RELEASE_DETAILS"
    GENERATING_RELEASE = "GENERATING_RELEASE"
    COMPLETE = "COMPLETE"


STAGES = list(Stage)
TASK_TYPES = {
    Stage.ANALYZING_SOURCE: "analyze",
    Stage.PREPARING_TRACKS: "prepare_tracks",
    Stage.RUNNING_CRF_ANALYSIS: "crf_analysis",
    Stage.ENCODING: "encode",
    Stage.VALIDATING_ENCODE: "validate",
    Stage.REMUXING: "mux",
    Stage.SCREENSHOT_CANDIDATE_GENERATION: "generate_candidates",
    Stage.SCREENSHOT_AGENT_SELECTION: "select_screenshots",
    Stage.SCREENSHOT_RENDERING: "render_screenshots",
    Stage.GENERATING_RELEASE: "generate_release",
}
HUMAN_GATES = {
    Stage.WAITING_FOR_TRACK_SELECTION,
    Stage.WAITING_FOR_ENCODE_SELECTION,
    Stage.WAITING_FOR_SCREENSHOT_SELECTION,
    Stage.WAITING_FOR_RELEASE_DETAILS,
}


def next_stage(stage: str) -> Stage:
    index = STAGES.index(Stage(stage))
    if index == len(STAGES) - 1:
        raise ValueError("Job is already complete")
    return STAGES[index + 1]


def require_stage(actual: str, expected: Stage):
    if actual != expected:
        raise ValueError(f"Action requires {expected}; job is {actual}")
