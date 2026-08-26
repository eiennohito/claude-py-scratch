import asyncio

from py_scratch.server import SERVER_LOG, serve, _log


def main():
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    except BaseException:
        # The client discards our stderr unless it was started with --mcp-debug,
        # so make sure the reason we died is on disk somewhere.
        _log.critical("server exiting on unhandled exception", exc_info=True)
        raise
    finally:
        _log.info("server stopped (log: %s)", SERVER_LOG)


if __name__ == "__main__":
    main()
