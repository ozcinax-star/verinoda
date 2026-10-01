import sys


def target():
    return 1


def by_name(name):
    return getattr(sys.modules[__name__], name)()


def run_by_name():
    return by_name("target")


def through_variable():
    fn = target
    return fn()


def direct():
    return target()
