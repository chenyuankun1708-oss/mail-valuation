import sys
import time
from contextlib import contextmanager


def format_duration(seconds):
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "%.1f秒" % seconds
    minutes = int(seconds // 60)
    return "%d分%.1f秒" % (minutes, seconds - minutes * 60)


_DEFAULT_STREAM = object()


class TimingRecorder:
    """Collect monotonic command timings while keeping business output on stdout."""

    def __init__(self, stream=_DEFAULT_STREAM, clock=None):
        self.stream = sys.stderr if stream is _DEFAULT_STREAM else stream
        self.clock = clock or time.perf_counter
        self.started_at = self.clock()
        self.steps = []
        self._summary_printed = False

    def _write(self, message):
        if self.stream is not None:
            self.stream.write(message + "\n")
            self.stream.flush()

    def event(self, event, name, group, seconds=None, status="success"):
        if event == "start":
            self._write("[计时] 开始：%s" % name)
            return
        value = round(max(0.0, float(seconds or 0.0)), 3)
        item = {"name": name, "group": group, "status": status, "seconds": value}
        self.steps.append(item)
        label = "完成" if status == "success" else "失败"
        self._write("[计时] %s：%s（%s）" % (label, name, format_duration(value)))

    @contextmanager
    def step(self, name, group):
        self.event("start", name, group)
        started = self.clock()
        status = "success"
        try:
            yield
        except BaseException:
            status = "failed"
            raise
        finally:
            self.event("finish", name, group, self.clock() - started, status)

    def callback(self, event, name, group, seconds=None, status="success"):
        self.event(event, name, group, seconds, status)

    def summary(self):
        groups = {}
        for item in self.steps:
            groups[item["group"]] = round(groups.get(item["group"], 0.0) + item["seconds"], 3)
        return {"steps": list(self.steps), "groups": groups,
                "total_seconds": round(max(0.0, self.clock() - self.started_at), 3)}

    def print_summary(self):
        summary = self.summary()
        if self._summary_printed:
            return summary
        self._summary_printed = True
        self._write("[计时] 分类汇总：" + "；".join(
            "%s %s" % (name, format_duration(seconds))
            for name, seconds in summary["groups"].items()))
        self._write("[计时] 本次命令总耗时：%s" % format_duration(summary["total_seconds"]))
        return summary


@contextmanager
def timed_phase(callback, name, group):
    """Report an optional internal phase without changing the caller's return value."""
    if callback:
        callback("start", name, group)
    started = time.perf_counter()
    status = "success"
    try:
        yield
    except BaseException:
        status = "failed"
        raise
    finally:
        if callback:
            callback("finish", name, group, time.perf_counter() - started, status)
