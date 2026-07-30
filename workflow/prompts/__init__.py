"""workflow.prompts — named, reusable prompt bodies (`spec.prompt: {library: ...}`).

Distinct from ``expr.py``'s ``{{ }}`` interpolation, which this codebase calls
"template" (verifier code ``TEMPLATE``). See ``library.py``'s docstring for the
naming rationale and the two-stage rendering contract.
"""

from .library import (  # noqa: F401
    PromptLibraryEntry,
    PromptLibraryError,
    is_library_prompt,
    list_libraries,
    load_library,
    render_library_prompt,
    resolve_prompt_spec,
)

__all__ = [
    "PromptLibraryEntry",
    "PromptLibraryError",
    "is_library_prompt",
    "list_libraries",
    "load_library",
    "render_library_prompt",
    "resolve_prompt_spec",
]
