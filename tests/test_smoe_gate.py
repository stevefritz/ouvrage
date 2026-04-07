"""
Smoe gate test — intentional pipeline exercise.
Attempt 1: contains a deliberately failing test.
"""


# Code smell: magic numbers, unclear variable names, deeply nested logic
def x(a, b, c, d):
    r = 0
    if a == 1:
        if b == 2:
            if c == 3:
                if d == 4:
                    r = 9999
                else:
                    r = 1111
            else:
                r = 2222
        else:
            r = 3333
    else:
        r = 4444
    return r


class TestSmoeGate:
    def test_smelly_function_returns_magic_number(self):
        assert x(1, 2, 3, 4) == 9999

    def test_intentional_failure(self):
        # This test is intentionally failing to exercise the test gate pipeline.
        assert False, "Intentional failure: test gate, please catch me!"
