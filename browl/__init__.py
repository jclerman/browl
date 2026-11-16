"""Host an OWL ontology as a browsable website."""

from importlib import metadata

_DISTRIBUTION_METADATA = metadata.metadata("browl")

__author__ = _DISTRIBUTION_METADATA["Author"]
__email__ = _DISTRIBUTION_METADATA["Author-email"]
__version__ = _DISTRIBUTION_METADATA["Version"]
