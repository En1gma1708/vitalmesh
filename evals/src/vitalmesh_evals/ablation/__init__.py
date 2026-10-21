"""The ablation (D39 item 2, D68): do multiple agents earn their cost?

Three arms answer the same questions for the same synthetic users:

* ``single``: ONE agent with all seven tools and a general prompt, then the same Guardian gate;
* ``t2``: the single specialist Triage picks, then the same gate;
* ``t3``: the full graph (Triage, parallel specialists, Aggregator, Synthesizer, Guardian,
  Checker for medium and high risk, Release; a run that needs a human stops there).

Every arm's output passes the SAME Guardian gate (``guardian_gate.evaluate_draft``), so the
arms differ in how the answer is produced and reviewed, not in the safety rules. Metrics are
deterministic and cost is counted at the router (calls and tokens). See ``metrics.py`` for what
each number does and does not show.
"""
