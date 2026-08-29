//! `paymos_core` — the native core of the Paymos Python SDK.
//!
//! A thin PyO3 shim over the shared `wallet-mpc` crate: one JSON-in / JSON-out call into the
//! 2-of-2 FROST-ed25519 engine (client role = id 1: DKG, commit, sign). Delegating verbatim to
//! `wallet_mpc::mpc_call_json` means the SDK runs the *identical* dispatch as the .NET server FFI
//! and the browser WASM binding, so it signs byte-for-byte the same.
//!
//! The Cargo package is published as `paymos-wallet-core`, but the lib name stays `wallet_mpc`,
//! so the Rust path is `wallet_mpc` either way.

use pyo3::prelude::*;

/// Run one MPC op. `req` is a JSON object `{"op": ..., ...}`; returns a JSON string
/// `{"ok": true, ...}` on success or `{"ok": false, "error": ...}` on failure.
#[pyfunction]
fn mpc_call(req: &str) -> String {
    wallet_mpc::mpc_call_json(req)
}

/// The Python extension module. Nested inside the pure package as `paymos._core`, so the
/// `#[pymodule]` name must be `_core` (maturin derives the init symbol `PyInit__core` from the
/// last path component of `module-name = "paymos._core"`). Import: `from paymos import _core`.
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(mpc_call, m)?)?;
    Ok(())
}
