import calc
def test_add():
    assert calc.add(2, 3) == 5
    assert calc.add(-1, 1) == 0
    assert calc.add(0, 0) == 0

def test_sub():
    assert calc.sub(5, 3) == 2
    assert calc.sub(1, 1) == 0
    assert calc.sub(0, 0) == 0

def test_dev():
    assert calc.dev(7, 2) == 3.5