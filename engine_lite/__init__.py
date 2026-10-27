"""engine_lite: a STAND-IN for Erik's Porch Engine, for the HW6 demo.

Not the Porch Engine. It does the smallest part of the Engine's job so a real
agent can reach the Device Hub the way the architecture intends:

    agent (Claude Code, via MCP) -> engine_lite: scope policy, owner approval, audit
                                 -> Device Hub: dry run, execute, read back
                                 -> result back to the agent

Erik's Engine replaces this package. Nothing in the hub depends on it.
"""

__version__ = "0.1.0"
