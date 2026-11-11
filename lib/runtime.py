import os
import signal
import sys
import traceback

from lib.output import hide_interrupt_echo, status


class Terminated(SystemExit):
    """Raised on SIGTERM; a SystemExit so `except Exception` cannot swallow it."""

    def __init__(self):
        super().__init__(143)


def run(main):
    code = 1

    def terminate(*_):
        raise Terminated

    # Restore Ctrl-C even if the parent started us with SIGINT ignored.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, terminate)
    try:
        try:
            with hide_interrupt_echo():
                result = main()
            if result is None or isinstance(result, int):
                code = result or 0
            else:
                code = 1
                status(f"main() returned a non-integer: {result!r}")
        except KeyboardInterrupt:
            code = 130
            status("Interrupted.")
        except Terminated:
            code = 143
            status("Interrupted.")
        except BrokenPipeError:
            code = 141
        except SystemExit as exc:
            if isinstance(exc.code, int):
                code = exc.code
            elif exc.code is None:
                code = 0
            else:
                code = 1
                status(exc.code)
        except BaseException:  # noqa: BLE001 - report anything, then exit 1
            # code stays 1; os._exit below would swallow a re-raise anyway.
            traceback.print_exc()
        finally:
            signal.signal(signal.SIGINT, lambda *_: os._exit(130))
            signal.signal(signal.SIGTERM, lambda *_: os._exit(143))
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except OSError, ValueError:
                    code = code or 1
    finally:
        os._exit(code)
