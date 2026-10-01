def fact(n):
    return 1 if n < 2 else n * fact(n - 1)


def ping(n):
    return pong(n - 1) if n else 0


def pong(n):
    return ping(n - 1) if n else 0


def outer():
    def inner():
        return 1
    return inner()


def chain1():
    return chain2()


def chain2():
    return chain3()


def chain3():
    return leaf()


def leaf():
    return 0


def caller():
    return fact(3)


class WithMethods:
    def run(self):
        return leaf()


class Empty:
    pass


def tri1(n):
    return tri2(n - 1) if n else 0


def tri2(n):
    return tri3(n - 1) if n else 0


def tri3(n):
    return tri1(n - 1) if n else 0
