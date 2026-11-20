import calc
def test_add():
    assert calc.add(2, 3) == 5
    assert calc.add(-1, 1) == 0
    assert calc.add(0, 0) == 0

def test_sub():
    assert calc.sub(5, 3) == 2
    assert calc.sub(1, 1) == 0
    assert calc.sub(0, 0) == 0

def test_mul():
    assert calc.mul(2, 3) == 6
    assert calc.mul(-1, 1) == -1
    assert calc.mul(0, 5) == 0
