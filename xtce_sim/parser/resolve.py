"""Reference resolution for the XTCE parser: linking parsed structures
after all files merge.
"""

import logging

from xtce_sim.models import (
    XTCEDefinition,
)

logger = logging.getLogger("xtce_sim.parser")


class ResolutionMixin:
    """Cross-reference resolution and the inheritance summary."""

    def _resolve_references(self, definition: XTCEDefinition):
        """Resolve base command and container references after parsing.

        A ref that names a non-existent target is left as None and warned about
        — otherwise inherited arguments/opcode/entries are silently dropped.
        """
        # Resolve command base references
        for cmd in definition.meta_commands.values():
            if cmd.base_meta_command_ref:
                cmd.base_command = definition.meta_commands.get(cmd.base_meta_command_ref)
                if cmd.base_command is None and self._warn:
                    logger.warning(
                        "command %r references unknown base command %r; "
                        "inherited arguments/opcode will be missing",
                        cmd.name,
                        cmd.base_meta_command_ref,
                    )

        # Resolve container base references
        for container in definition.containers.values():
            if container.base_container_ref:
                container.base_container = definition.containers.get(container.base_container_ref)
                if container.base_container is None and self._warn:
                    logger.warning(
                        "container %r references unknown base container %r; "
                        "inherited entries will be missing",
                        container.name,
                        container.base_container_ref,
                    )

        self._log_inheritance_summary(definition)

    @staticmethod
    def _log_inheritance_summary(definition: XTCEDefinition) -> None:
        """Trace how much inheritance the resolution pass actually wired up."""
        cmds = definition.meta_commands.values()
        with_base = sum(1 for c in cmds if c.base_command is not None)
        with_assign = sum(1 for c in cmds if c.argument_assignments)
        with_base_cont = sum(
            1 for c in definition.containers.values() if c.base_container is not None
        )
        if with_base or with_base_cont:
            logger.info(
                "resolved inheritance: %d command(s) with a base command "
                "(%d fixing inherited args via assignments), "
                "%d container(s) with a base container",
                with_base,
                with_assign,
                with_base_cont,
            )
