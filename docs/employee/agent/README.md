# Agent

[System map](../../README.md) · [Glossary](../../glossary.md) · [Design conventions](../../conventions.md)

An Agent is the software realization of the [Employee](../README.md), not a department or job title. The company hires an Agent with its own ID and a fixed runtime/model choice. Configuration is open to different models; execution is not free to substitute one. Restarting a process on the same binding preserves the Agent. Moving a duty to a different runtime requires another Agent and a recorded handoff.

- [Agent Configuration — choose and pin a compatible runtime/model before creation](configuration/README.md)
- [Agent Harness — prepare, execute, fence and settle each run](harness/README.md)
- [Agent Desk — private working set and explicit publication](desk/README.md)
- [Agent Learning — propose, evaluate, adopt in scope and verify use in a later shift](learning/README.md)
