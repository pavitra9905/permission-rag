EMBED_DIM = 384
TEST_PASSWORD = "Passw0rd!test"


def unit_vec(i: int) -> list[float]:
    """One-hot vector: unit_vec(0) is identical to itself and orthogonal to unit_vec(1)."""
    v = [0.0] * EMBED_DIM
    v[i % EMBED_DIM] = 1.0
    return v


def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def sqlstate(exc_info) -> str:
    """SQLSTATE of a SQLAlchemy DBAPIError caught with pytest.raises."""
    return exc_info.value.orig.sqlstate