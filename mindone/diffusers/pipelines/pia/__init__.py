from typing import TYPE_CHECKING

from ...utils import _LazyModule

_dummy_objects = {}
_import_structure = {}

_import_structure["pipeline_pia"] = ["PIAPipeline", "PIAPipelineOutput"]

if TYPE_CHECKING:
    from .pipeline_pia import PIAPipeline, PIAPipelineOutput

else:
    import sys

    sys.modules[__name__] = _LazyModule(
        __name__,
        globals()["__file__"],
        _import_structure,
        module_spec=__spec__,
    )
    for name, value in _dummy_objects.items():
        setattr(sys.modules[__name__], name, value)
