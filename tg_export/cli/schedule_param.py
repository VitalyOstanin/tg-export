"""The `--schedule` option of `tg send`: a moment parsed and checked against the present."""

from __future__ import annotations

from datetime import datetime

import click

from tg_export.schedule import check_schedule, local_now, parse_schedule


class ScheduleTime(click.ParamType):
    """A Click type for `--schedule`: parsed and checked against the present."""

    name = "time"

    def convert(self, value, param, ctx) -> datetime:
        if isinstance(value, datetime):
            return value
        now = local_now()
        try:
            moment = parse_schedule(value, now)
            check_schedule(moment, now)
        except ValueError as e:
            self.fail(str(e), param, ctx)
        return moment


SCHEDULE_TIME = ScheduleTime()
