"""Era-aware wall-clock conversion using the selected provider's ledger history."""

from copy import deepcopy

from .domain import KernelError


class SlotClock:
    def __init__(self, profile, eras):
        self.profile, self.eras = profile, deepcopy(eras)
        if not isinstance(eras, list) or not eras:
            raise KernelError("Unverified era history")
        previous = None
        for era in eras:
            start, end = era["start"], era.get("end")
            length = era["parameters"]["slotLength"]["milliseconds"]
            if type(length) is not int or length <= 0:
                raise KernelError("Invalid era slot length")
            if previous is not None and start != previous:
                raise KernelError("Discontinuous era history")
            if end is not None:
                elapsed = (end["time"]["seconds"] - start["time"]["seconds"]) * 1000
                if elapsed <= 0 or elapsed != (end["slot"] - start["slot"]) * length:
                    raise KernelError("Inconsistent era time and slot bounds")
            previous = end

    def slot_at_ms(self, timestamp):
        relative = timestamp - self.profile.system_start * 1000
        for era in self.eras:
            start, end = era["start"], era.get("end")
            start_ms = start["time"]["seconds"] * 1000
            if relative >= start_ms and (
                end is None or relative < end["time"]["seconds"] * 1000
            ):
                return (
                    start["slot"]
                    + (relative - start_ms)
                    // era["parameters"]["slotLength"]["milliseconds"]
                )
        raise KernelError(
            "Requested validity time is outside the provider's era horizon"
        )
