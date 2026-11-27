"""Read exact Modal call outcomes; never infer that an old queued call died."""

async def is_terminal(call_id):
    import modal
    from modal.exception import (NotFoundError, OutputExpiredError, FunctionTimeoutError,
                                 RemoteError)
    # Inspect the worker outcome only. Connection failures and queueing are
    # deliberately not recovery signals.
    try:
        await modal.FunctionCall.from_id(call_id).get.aio(timeout=0)
    except (NotFoundError, OutputExpiredError, FunctionTimeoutError, RemoteError):
        return True
    except Exception:
        return False
    return True
