from vitalmesh_evals.case import EvalCase


def all_cases() -> tuple[EvalCase, ...]:
    from vitalmesh_evals.cases import explain, injection, t0

    cases = t0.CASES + explain.CASES + injection.CASES
    ids = [c.id for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate case ids: {sorted(dupes)}")
    return cases
