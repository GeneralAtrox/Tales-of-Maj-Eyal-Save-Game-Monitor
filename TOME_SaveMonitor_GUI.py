from time import perf_counter

_STARTUP_TIMER_STARTED_AT = perf_counter()

if __name__ == "__main__":
    from gui.app import main

    main(startup_started_at=_STARTUP_TIMER_STARTED_AT)
