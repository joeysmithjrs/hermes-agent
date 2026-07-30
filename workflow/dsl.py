"""Python DSL builder (design §3.1).

OPTIONAL for Phase 1 — YAML is the MVP-must. This is a stub; the DSL is a
Phase 2 deliverable. Using it raises NotImplementedError so authors are
funneled to YAML (or the IR dataclasses directly).
"""

from __future__ import annotations

__all__ = ["Workflow", "node", "gate", "expr"]


class _DSLStub:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "The Python DSL builder is a Phase 2 deliverable (design §3.1). "
            "For Phase 1, author workflows in YAML (workflow.yaml_load) or build "
            "the IR dataclasses directly (workflow.ir)."
        )

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def Workflow(*args, **kwargs):  # noqa: N802
    return _DSLStub(*args, **kwargs)


class _NodeNS:
    @staticmethod
    def agent(**kw):
        raise NotImplementedError("DSL node.agent is Phase 2; use YAML or workflow.ir.NodeSpec.")

    @staticmethod
    def script(**kw):
        raise NotImplementedError("DSL node.script is Phase 2; use YAML or workflow.ir.Node.")


node = _NodeNS


def gate(*args, **kwargs):
    raise NotImplementedError("DSL gate() is Phase 2; use YAML gates: or workflow.ir.Gate.")


class _ExprNS:
    @staticmethod
    def template(*args, **kw):
        raise NotImplementedError("DSL expr.template is Phase 2; use workflow.expr.render.")


expr = _ExprNS