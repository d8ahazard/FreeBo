"""The skills system — how Autobot's capabilities stay modular without punching holes in the safety floor.

A `Skill` bundles tools (each with a JSON schema, an async handler, and a required authority level), an
optional system-prompt fragment, an optional per-observation hook, and optional background workers. The
`SkillRegistry` composes the enabled skills into the tool list the model sees and dispatches calls — always
through the safety floor and the owner-authority gate. Adding a capability = add a Skill (or a tool to one)
+ its authority + a doc line. See docs/AI_BRAIN.md and .cursor/rules/20-ai-brain-contract.mdc.
"""

from .base import Skill, SkillContext, ToolDef
from .registry import SkillRegistry, build_default_skills

__all__ = ["Skill", "SkillContext", "ToolDef", "SkillRegistry", "build_default_skills"]
