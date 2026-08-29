"""Guards the wheel layout: BOTH the pure ``paymos`` package and the native
``paymos._core`` extension must ship and import.

Note: under pytest, ``paymos/`` also happens to be on sys.path via the source tree,
so the *definitive* check is a manual import from a neutral cwd (not ``sdk/python/``)
against the installed wheel — see task-b2-report.md. This test documents intent and
catches a regression where the native ``_core`` is missing entirely.
"""


def test_pure_package_imports():
    import paymos

    assert hasattr(paymos, "__all__")


def test_native_core_imports_nested():
    from paymos import _core

    assert callable(_core.mpc_call)
