# fathomdx — conventions

Project-specific conventions and lake tag contracts. Read when working on
the Grand Loop, routines, or agent plumbing.

> The Grand Loop has replaced chat as the primary substrate. The Grand
> Loop docs (puddle, voices, witness, recall, vampire-tap, pressure)
> will live here once the design is stable. Until then, read the module
> docstrings under `api/loop/` directly.

## Routines

Routines are scheduled prompts that fire on a local machine via the
agent's `kitty` plugin. A routine lands in the lake as a `routine-fire`
delta that the agent picks up and executes by spawning claude-code in a
kitty window. The model running in that kitty window is free to write
deltas back to the lake (tagged with whatever the routine's prompt
instructs), and the dashboard pairs the fire to its summary delta by
routine-id.

If a user wants to see what a routine produced, they look at the
routines page or search the lake by `routine-id:<id>`.

> TODO (Grand Loop): the witness should eventually be able to mint
> routines from intent deltas. The OpenAI-shape schema for routine
> creation lives in `api/_tool_schema.py` (CHAT_ONLY_TOOLS / "routines"
> entry); reuse it when wiring the witness's routine-fire route.
