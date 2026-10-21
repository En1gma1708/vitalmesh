"""Run cases against systems under test and summarise pass rates by category."""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from vitalmesh_evals.case import CaseResult, EvalCase
from vitalmesh_evals.graders import run_grader
from vitalmesh_evals.suts import Sut, SutOutput


class Report(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    results: tuple[CaseResult, ...]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    def by_category(self) -> dict[str, tuple[int, int]]:
        tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in self.results:
            tally[r.category][1] += 1
            tally[r.category][0] += int(r.passed)
        return {k: (v[0], v[1]) for k, v in sorted(tally.items())}

    def tokens(self) -> tuple[int, int]:
        return (
            sum(r.prompt_tokens for r in self.results),
            sum(r.completion_tokens for r in self.results),
        )

    def summary(self) -> str:
        infra = sum(1 for r in self.results if r.error)
        lines = [f"== {self.label}: {self.passed}/{len(self.results)} cases passed"]
        if infra:
            lines.append(f"   ({infra} of the failures are provider errors, not answer quality)")
        for cat, (ok, total) in self.by_category().items():
            lines.append(f"   {cat:18} {ok}/{total}")
        pt, ct = self.tokens()
        cached = sum(1 for r in self.results if r.cached)
        if pt or ct:
            lines.append(f"   tokens {pt} prompt + {ct} completion; {cached} cached")
        for r in self.results:
            if not r.passed:
                why = r.error or "; ".join(
                    f"{x.grader}: {x.detail}" for x in r.grades if not x.passed
                )
                lines.append(f"   FAIL {r.case_id}: {why}")
        return "\n".join(lines)


def fingerprint(case: EvalCase, extra: str) -> str:
    """Changes whenever the case (input or graders) or the system configuration changes, so a
    stale checkpoint entry is never reused for an edited case."""
    return hashlib.sha256((case.model_dump_json() + "|" + extra).encode()).hexdigest()[:16]


def _append_line(path: Path, line: str) -> None:
    """Append one complete line. If an interrupted run left a torn last line (no trailing
    newline), terminate it first so this entry never merges into the garbage; flush and fsync so
    a finished case survives a crash right after."""
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_break = False
    if path.exists() and path.stat().st_size > 0:
        with path.open("rb") as f:
            f.seek(-1, os.SEEK_END)
            needs_break = f.read(1) != b"\n"
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(("\n" if needs_break else "") + line + "\n")
        f.flush()
        os.fsync(f.fileno())


def code_fingerprint() -> str:
    """Hash of the grader and system-under-test code: editing either invalidates old results."""
    from vitalmesh_evals import evidence_sut, graders, guard, judge, nutrition_sut, suts

    h = hashlib.sha256()
    for mod in (graders, suts, judge, guard, evidence_sut, nutrition_sut):
        h.update(Path(str(mod.__file__)).read_bytes())
    return h.hexdigest()[:16]


def _load_checkpoint(path: Path) -> dict[str, CaseResult]:
    done: dict[str, CaseResult] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
                done[doc["fp"]] = CaseResult.model_validate(doc["result"])
            except (ValueError, KeyError):
                continue  # a half-written last line from an interrupted run is simply ignored
    return done


def run_cases(
    label: str,
    cases: Sequence[EvalCase],
    suts: Mapping[str, Sut],
    checkpoint: Path | None = None,
    config: str = "",
    progress: Callable[[str], None] | None = None,
    checkpoint_suts: frozenset[str] = frozenset({"explain", "evidence", "nutrition"}),
) -> Report:
    """Run every case. With a ``checkpoint`` file, finished cases are saved one by one and
    skipped on the next run (resume after a rate limit or crash). Provider errors are never
    checkpointed, so they are retried. Only ``checkpoint_suts`` (the model-backed ones) are
    checkpointed: deterministic cases are instant and always rerun. ``config`` should identify
    the prompt, model setup and code version."""
    done = _load_checkpoint(checkpoint) if checkpoint else {}
    results: list[CaseResult] = []
    for case in cases:
        fp = fingerprint(case, config)
        if fp in done and case.sut in checkpoint_suts:
            results.append(done[fp])
            if progress:
                progress(f"resumed {case.id}")
            continue
        try:
            out = suts[case.sut].run(case)
        except Exception as exc:  # an infrastructure crash is an error result, not a lost run
            out = SutOutput({}, error=f"sut_crashed: {type(exc).__name__}")
        grades = tuple(run_grader(s.type, out.output, case.input, s.args) for s in case.graders)
        result = CaseResult(
            case_id=case.id,
            category=case.category,
            sut=case.sut,
            passed=out.error is None and all(x.passed for x in grades),
            grades=grades,
            output=out.output,
            error=out.error,
            provider=out.provider,
            prompt_tokens=out.prompt_tokens,
            completion_tokens=out.completion_tokens,
            cached=out.cached,
            latency_ms=out.latency_ms,
        )
        results.append(result)
        if checkpoint and result.error is None and case.sut in checkpoint_suts:
            _append_line(
                checkpoint, json.dumps({"fp": fp, "result": result.model_dump(mode="json")})
            )
        if progress:
            progress(f"{'ok  ' if result.passed else 'FAIL'} {case.id}")
    return Report(label=label, results=tuple(results))


def save_report(report: Report, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{report.label}-{stamp}.json"
    path.write_text(json.dumps(report.model_dump(mode="json"), indent=1), encoding="utf-8")
    return path
