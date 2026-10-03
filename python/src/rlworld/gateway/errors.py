"""Gateway errors. Retryable upstream failures are retried; everything else surfaces at once."""


class GatewayError(Exception):
    """Base class for every gateway failure."""


class EndpointNotFoundError(GatewayError):
    """No registered endpoint matches the name or base URL a run references."""


class ModelNotPinnedError(GatewayError):
    """The request names a model the endpoint does not list as an exact pinned version."""


class DataPolicyViolationError(GatewayError):
    """The request would send screenshots to an endpoint the tenant forbids them on."""


class CircuitOpenError(GatewayError):
    """The endpoint failed repeatedly and is cooling down."""


class RateLimitTimeoutError(GatewayError):
    """Rate limit or concurrency capacity did not free up in time."""


class CacheMissError(GatewayError):
    """A read only cached run asked for a response that was never recorded."""


class TokenIdsUnavailableError(GatewayError):
    """Training mode needs exact token ids and logprobs but the endpoint returned none."""


class MissingSecretError(GatewayError):
    """An endpoint key is not in the secret store."""


class UpstreamError(GatewayError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
        counts_as_endpoint_failure: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.counts_as_endpoint_failure = counts_as_endpoint_failure
