# Hermes profile resolution

The maintained procedure is the shipped
[Hermes profile resolution skill](../src/scitex_agent_container/_skills/scitex-agent-container/47_hermes-profile-resolution.md).
It maps spec and `to_home` inputs to generated runtime/overlay profiles, identifies
the active container HOME, and distinguishes project skills from provider settings.

Read it through `sac dev skills get 47_hermes-profile-resolution`. For fleet hosts
and skill projections, follow its references to the owning scitex-dev skills.

Documentation examples marked `# sac-doc-check` are executed by
`tests/develop/test_documented_examples.py` using scitex-dev's Markdown parser.
Run `python -m pytest tests/develop/test_documented_examples.py` from this
checkout. These offline examples also run in the existing daily CI suite.
Unmarked examples, live API behavior, and prose still require source review;
passing this check does not certify every statement in README, docs, or skills.
