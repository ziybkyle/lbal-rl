"""Reserved fixed evaluation seeds, shared by all training routes."""

FIXED_EVAL_START = 1_900_000_000
MAX_EVAL_GAMES = 100_000
FIXED_EVAL_STOP = FIXED_EVAL_START + MAX_EVAL_GAMES


def overlaps_fixed_evaluation(start: int, stop: int) -> bool:
    return start < FIXED_EVAL_STOP and FIXED_EVAL_START < stop
