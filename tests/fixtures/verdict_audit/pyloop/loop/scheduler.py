"""Named jobs kept in a priority queue (a plain list, sorted after every insert)."""


def schedule_task(queue, name, priority=0):
    queue.append((priority, name))
    queue.sort()
    return len(queue)


def plan_nightly(queue):
    schedule_task(queue, "backup", 1)
    schedule_task(queue, "report", 2)


def plan_hourly(queue):
    return schedule_task(queue, "rotate-logs")
