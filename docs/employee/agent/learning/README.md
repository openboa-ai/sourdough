# A3 · Agent Learning and evaluation

Design draft · [Company and quant architecture](../../../README.md#system-hierarchy) · [Development lifecycle](../../../engineering/lifecycle.md)

**Responsibility:** Owns the Agent learning loop: propose a candidate, evaluate evidence, adopt for an authorized audience and observe the effective revision in a later run. A personal note does not become a company method by being shared. Resource custody remains with company resources and adoption authority with grants. This capability does not update model weights.

## Agent evaluation

### [Evaluate Agent judgment](evaluate/intent.md)

Assess grounding, authority and follow-up independently of a domain score such as profit.

- [run-trials](evaluate/run-trials/spec.md) — Use sealed cases and matched budgets to compare Agent configurations. Score grounding, authority and follow-up; keep failures and missed trials.

## Candidate and adopted methods

### [Improve a reusable method from evidence](methods/intent.md)

Convert experience into an evaluated candidate without widening authority or changing the books.

- [distill](methods/distill/spec.md) — Draft a scoped revision linked to original observations and outcomes, preserving uncertainty and source access.
- [evaluate](methods/evaluate/spec.md) — Compare candidate and incumbent on held-out cases. Record adoption or rejection and retain a working rollback target.
