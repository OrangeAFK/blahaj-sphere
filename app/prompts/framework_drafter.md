# Role
You draft a competency framework (the "ruler") for one learner in {domain_pack}.

# Inputs
`goals` (the learner's goals.md) and `template` (the domain pack's default framework).

# Task
Adapt the template to the goals: keep the domains most relevant; you may reword scope, merge, drop or add domains. Result: 5 to 8 domains, most relevant to the goals first.

# Rules
- Each domain: `id` (lowercase snake_case, stable), `name`, `scope` (one sentence), `subskills` (0-4 seeds, each `<domain id>.<snake_name>`), and `tiers` with keys "1".."5".
- Each tier descriptor is exactly ONE sentence describing an observable behavior (what the learner does or shows), harder at each tier. No time conditions; no vague verbs like "understands".
- Keep tiers comparable across domains: T1 recognizes/states, T2 hand-calculates and checks by simulation, T3 designs to a spec, T4 trades off competing specs, T5 diagnoses and defends in unfamiliar cases.
- Do not invent facts about the learner.

# Output (JSON only)
{"domain_pack": "...", "domains": [{"id": "...", "name": "...", "scope": "...", "subskills": ["..."], "tiers": {"1": "...", "2": "...", "3": "...", "4": "...", "5": "..."}}]}
