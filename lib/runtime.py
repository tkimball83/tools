import os
import signal
import sys
import traceback

from lib.output import hide_interrupt_echo, status


def run(main):
    code = 1

    def interrupt(*_):
        nonlocal code
        code = 130
        raise KeyboardInterrupt

    def terminate(*_):
        nonlocal code
        code = 143
        raise SystemExit(143)

    signal.signal(signal.SIGINT, interrupt)
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
        except BaseException:
            traceback.print_exc()
            raise
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
