"""Domain error hierarchy."""


class AiOilError(Exception):
    """Base class of all domain errors."""


class ValidationError(AiOilError):
    """Input or state violates a rule."""


class NotFoundError(AiOilError):
    """A referenced record does not exist."""


class ConflictError(AiOilError):
    """The operation conflicts with existing data."""


class TransitionError(ValidationError):
    """A workflow transition is not allowed."""


class ConfigError(AiOilError):
    """Configuration is missing or invalid."""
