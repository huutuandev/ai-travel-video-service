class RequestConflict(Exception):
    pass


class RetryableExecutionError(Exception):
    pass


class ProcessingUnavailable(Exception):
    pass


class MediaError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

