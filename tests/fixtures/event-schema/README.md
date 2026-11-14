# Copies of the services' schemas, for tests only

`AgentCommand.avsc` and `AgentCommandResult.avsc` are **copies** of
`veritaslock-services/event-schema/src/main/avro/protocol/events/agent-commands/v1/AgentCommand.avsc`
and `.../agent-results/v1/AgentCommandResult.avsc`, which are the source of truth.
The control plane registers them in the Schema Registry, and the agent takes them
from there at run time: no schema ships in `src/`.

The tests load these copies into the fake (and the library's mock) Schema Registry.
`tests/test_agent_schemas.py` compares them with the services repository when it is
checked out beside this one, so a stale copy fails a test. To refresh them, copy
the two files over these.
