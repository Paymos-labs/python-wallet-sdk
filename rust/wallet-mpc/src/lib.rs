//! 2-of-2 FROST-ed25519 threshold signing.
//!
//! Two participants (client = id 1, server = id 2) jointly hold one ed25519 key via DKG (neither
//! ever holds the full key). Together they produce a STANDARD ed25519 signature for the group key,
//! which is an ed25519 implicit account (id = hex(group pubkey)). The client only ever sees the opaque
//! message bytes, so the client side carries no chain-specific code.
//!
//! `mpc_call` is the single C-ABI / WASM entry point: a JSON request `{op, ...}` in, a JSON
//! response `{ok, ...}` out. The HOST (the server, the WASM client) holds all per-round state
//! (secret packages, nonces, key packages) as JSON between calls — the lib is stateless.

pub use frost_ed25519 as frost;
use frost::Identifier;
use rand::rngs::OsRng;
use serde::{de::DeserializeOwned, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;

pub const CLIENT_ID: u16 = 1;
pub const SERVER_ID: u16 = 2;

// ---- helpers --------------------------------------------------------------------------------
fn e2s<E: std::fmt::Display>(e: E) -> String { e.to_string() }
fn to_v<T: Serialize>(x: &T) -> Result<Value, String> { serde_json::to_value(x).map_err(e2s) }
fn from_v<T: DeserializeOwned>(v: &Value) -> Result<T, String> { serde_json::from_value(v.clone()).map_err(e2s) }
fn id_u16(n: u16) -> Result<Identifier, String> { Identifier::try_from(n).map_err(e2s) }
fn id_to_u16(id: &Identifier) -> Result<u16, String> {
    for n in [CLIENT_ID, SERVER_ID] {
        if id_u16(n)? == *id { return Ok(n); }
    }
    Err("unknown identifier".into())
}
// Maps cross the wire as arrays of [id_u16, value] so we never depend on how Identifier serializes.
fn parse_map<T: DeserializeOwned>(v: &Value) -> Result<BTreeMap<Identifier, T>, String> {
    let arr = v.as_array().ok_or("expected array of [id, value]")?;
    let mut m = BTreeMap::new();
    for it in arr {
        let n = it.get(0).and_then(|x| x.as_u64()).ok_or("bad id")? as u16;
        m.insert(id_u16(n)?, from_v::<T>(it.get(1).ok_or("missing value")?)?);
    }
    Ok(m)
}
fn map_to_v<T: Serialize>(m: &BTreeMap<Identifier, T>) -> Result<Value, String> {
    let mut arr = Vec::new();
    for (id, val) in m { arr.push(json!([id_to_u16(id)?, to_v(val)?])); }
    Ok(Value::Array(arr))
}
fn group_hex(pk: &frost::keys::PublicKeyPackage) -> Result<String, String> {
    Ok(hex::encode(pk.verifying_key().serialize().map_err(e2s)?))
}

// ---- per-round dispatch ---------------------------------------------------------------------
fn handle(req: &Value) -> Result<Value, String> {
    let op = req.get("op").and_then(|x| x.as_str()).ok_or("missing op")?;
    let mut rng = OsRng;
    match op {
        // ---- DKG ----
        "dkg_part1" => {
            let id = id_u16(req["id"].as_u64().ok_or("id")? as u16)?;
            let max = req["max"].as_u64().ok_or("max")? as u16;
            let min = req["min"].as_u64().ok_or("min")? as u16;
            let (secret, package) = frost::keys::dkg::part1(id, max, min, &mut rng).map_err(e2s)?;
            Ok(json!({ "secret": to_v(&secret)?, "package": to_v(&package)? }))
        }
        "dkg_part2" => {
            let secret: frost::keys::dkg::round1::SecretPackage = from_v(&req["secret"])?;
            let r1 = parse_map::<frost::keys::dkg::round1::Package>(&req["round1_packages"])?;
            let (secret2, pkgs) = frost::keys::dkg::part2(secret, &r1).map_err(e2s)?;
            Ok(json!({ "secret": to_v(&secret2)?, "packages": map_to_v(&pkgs)? }))
        }
        "dkg_part3" => {
            let secret: frost::keys::dkg::round2::SecretPackage = from_v(&req["secret"])?;
            let r1 = parse_map::<frost::keys::dkg::round1::Package>(&req["round1_packages"])?;
            let r2 = parse_map::<frost::keys::dkg::round2::Package>(&req["round2_packages"])?;
            let (kp, pk) = frost::keys::dkg::part3(&secret, &r1, &r2).map_err(e2s)?;
            Ok(json!({ "key_package": to_v(&kp)?, "public_key_package": to_v(&pk)?, "group_pubkey_hex": group_hex(&pk)? }))
        }
        // ---- signing ----
        "commit" => {
            let kp: frost::keys::KeyPackage = from_v(&req["key_package"])?;
            let (nonces, commitments) = frost::round1::commit(kp.signing_share(), &mut rng);
            Ok(json!({ "nonces": to_v(&nonces)?, "commitments": to_v(&commitments)? }))
        }
        "build_signing_package" => {
            let commitments = parse_map::<frost::round1::SigningCommitments>(&req["commitments"])?;
            let msg = hex::decode(req["message_hex"].as_str().ok_or("message_hex")?).map_err(e2s)?;
            let sp = frost::SigningPackage::new(commitments, &msg);
            Ok(json!({ "signing_package": to_v(&sp)? }))
        }
        "sign" => {
            let sp: frost::SigningPackage = from_v(&req["signing_package"])?;
            let nonces: frost::round1::SigningNonces = from_v(&req["nonces"])?;
            let kp: frost::keys::KeyPackage = from_v(&req["key_package"])?;
            let share = frost::round2::sign(&sp, &nonces, &kp).map_err(e2s)?;
            Ok(json!({ "signature_share": to_v(&share)? }))
        }
        "aggregate" => {
            let sp: frost::SigningPackage = from_v(&req["signing_package"])?;
            let shares = parse_map::<frost::round2::SignatureShare>(&req["signature_shares"])?;
            let pk: frost::keys::PublicKeyPackage = from_v(&req["public_key_package"])?;
            let sig = frost::aggregate(&sp, &shares, &pk).map_err(e2s)?;
            let verified = pk.verifying_key().verify(sp.message(), &sig).is_ok();
            Ok(json!({ "signature_hex": hex::encode(sig.serialize().map_err(e2s)?), "verified": verified }))
        }
        // ---- meta ----
        // The FFI SDKs download a pinned build of this library; this op lets them prove the
        // binary they loaded is the build they pinned, fail-closed on a stale override.
        "version" => Ok(json!({ "version": env!("CARGO_PKG_VERSION") })),
        other => Err(format!("unknown op: {other}")),
    }
}

// ---- C-ABI / WASM entry points --------------------------------------------------------------
/// Run one JSON op. Returns a newly-allocated UTF-8 JSON buffer; the caller MUST free it with
/// `mpc_free(ptr, *out_len)`.
#[no_mangle]
pub extern "C" fn mpc_call(req_ptr: *const u8, req_len: usize, out_len: *mut usize) -> *mut u8 {
    let input = unsafe { std::slice::from_raw_parts(req_ptr, req_len) };
    let resp = match serde_json::from_slice::<Value>(input) {
        Ok(req) => match handle(&req) {
            Ok(mut v) => {
                if let Some(o) = v.as_object_mut() { o.insert("ok".into(), json!(true)); }
                v
            }
            Err(e) => json!({ "ok": false, "error": e }),
        },
        Err(e) => json!({ "ok": false, "error": format!("bad request json: {e}") }),
    };
    let bytes = serde_json::to_vec(&resp).unwrap_or_else(|_| br#"{"ok":false,"error":"serialize"}"#.to_vec());
    let mut boxed = bytes.into_boxed_slice();
    let ptr = boxed.as_mut_ptr();
    unsafe { *out_len = boxed.len(); }
    std::mem::forget(boxed);
    ptr
}

/// Free a buffer returned by `mpc_call`.
#[no_mangle]
pub extern "C" fn mpc_free(ptr: *mut u8, len: usize) {
    if !ptr.is_null() {
        unsafe { drop(Box::from_raw(std::slice::from_raw_parts_mut(ptr, len))); }
    }
}

/// JSON string in/out entry (all native targets): parse -> handle -> {..., ok}. The PyO3 SDK core
/// and (optionally) the WASM binding share this so every surface runs the identical dispatch.
pub fn mpc_call_json(req: &str) -> String {
    let resp = match serde_json::from_str::<Value>(req) {
        Ok(r) => match handle(&r) {
            Ok(mut v) => { if let Some(o) = v.as_object_mut() { o.insert("ok".into(), json!(true)); } v }
            Err(e) => json!({ "ok": false, "error": e }),
        },
        Err(e) => json!({ "ok": false, "error": format!("bad request json: {e}") }),
    };
    serde_json::to_string(&resp).unwrap_or_else(|_| r#"{"ok":false,"error":"serialize"}"#.to_string())
}

// ---- in-process self-test (used by the FFI smoke + the unit test) --------------------------
/// Full in-process 2-of-2 DKG + threshold signature. Returns `(group_pubkey, message, signature)`.
pub fn selftest() -> Result<([u8; 32], Vec<u8>, [u8; 64]), String> {
    let mut rng = OsRng;
    let (max, min) = (2u16, 2u16);
    let (id1, id2) = (id_u16(CLIENT_ID)?, id_u16(SERVER_ID)?);

    let (s1a, p1a) = frost::keys::dkg::part1(id1, max, min, &mut rng).map_err(e2s)?;
    let (s1b, p1b) = frost::keys::dkg::part1(id2, max, min, &mut rng).map_err(e2s)?;
    let r1a = BTreeMap::from([(id2, p1b)]);
    let r1b = BTreeMap::from([(id1, p1a)]);
    let (s2a, p2a) = frost::keys::dkg::part2(s1a, &r1a).map_err(e2s)?;
    let (s2b, p2b) = frost::keys::dkg::part2(s1b, &r1b).map_err(e2s)?;
    let r2a = BTreeMap::from([(id2, p2b[&id1].clone())]);
    let r2b = BTreeMap::from([(id1, p2a[&id2].clone())]);
    let (kpa, pka) = frost::keys::dkg::part3(&s2a, &r1a, &r2a).map_err(e2s)?;
    let (kpb, pkb) = frost::keys::dkg::part3(&s2b, &r1b, &r2b).map_err(e2s)?;
    if pka.verifying_key() != pkb.verifying_key() { return Err("group key mismatch".into()); }

    let message = b"wallet-mpc selftest".to_vec();
    let (na, ca) = frost::round1::commit(kpa.signing_share(), &mut rng);
    let (nb, cb) = frost::round1::commit(kpb.signing_share(), &mut rng);
    let commitments = BTreeMap::from([(id1, ca), (id2, cb)]);
    let sp = frost::SigningPackage::new(commitments, &message);
    let ssa = frost::round2::sign(&sp, &na, &kpa).map_err(e2s)?;
    let ssb = frost::round2::sign(&sp, &nb, &kpb).map_err(e2s)?;
    let shares = BTreeMap::from([(id1, ssa), (id2, ssb)]);
    let sig = frost::aggregate(&sp, &shares, &pka).map_err(e2s)?;
    pka.verifying_key().verify(&message, &sig).map_err(e2s)?;

    let pk: [u8; 32] = pka.verifying_key().serialize().map_err(e2s)?.as_slice().try_into().map_err(|_| "pk len")?;
    let sigb: [u8; 64] = sig.serialize().map_err(e2s)?.as_slice().try_into().map_err(|_| "sig len")?;
    Ok((pk, message, sigb))
}

/// FFI smoke test: 0 on success, 1 on failure.
#[no_mangle]
pub extern "C" fn mpc_selftest() -> i32 {
    match selftest() { Ok(_) => 0, Err(_) => 1 }
}

// ---- WASM (client) entry point: same dispatch, JS-friendly string in/out ---------------------
#[cfg(target_arch = "wasm32")]
mod wasm_api {
    use wasm_bindgen::prelude::*;

    #[wasm_bindgen]
    pub fn mpc_call_wasm(req: &str) -> String {
        let resp = match serde_json::from_str::<serde_json::Value>(req) {
            Ok(r) => match super::handle(&r) {
                Ok(mut v) => {
                    if let Some(o) = v.as_object_mut() { o.insert("ok".into(), serde_json::json!(true)); }
                    v
                }
                Err(e) => serde_json::json!({ "ok": false, "error": e }),
            },
            Err(e) => serde_json::json!({ "ok": false, "error": format!("bad request json: {e}") }),
        };
        resp.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dkg_2of2_then_sign_is_valid_ed25519() {
        let (pk, msg, sig) = selftest().expect("selftest");
        use ed25519_dalek::Verifier;
        let vk = ed25519_dalek::VerifyingKey::from_bytes(&pk).unwrap();
        vk.verify(&msg, &ed25519_dalek::Signature::from_bytes(&sig)).expect("ed25519 verify");
        println!("DKG group account (ed25519 implicit) = {}", hex::encode(pk));
    }

    // Drive the full protocol through the JSON dispatch (what the host does over FFI/WASM).
    #[test]
    fn json_dispatch_full_2of2_roundtrip() {
        let call = |v: Value| -> Value { handle(&v).unwrap_or_else(|e| json!({"error": e})) };

        let a1 = call(json!({"op":"dkg_part1","id":CLIENT_ID,"max":2,"min":2}));
        let b1 = call(json!({"op":"dkg_part1","id":SERVER_ID,"max":2,"min":2}));
        let a2 = call(json!({"op":"dkg_part2","secret":a1["secret"],"round1_packages":[[SERVER_ID,b1["package"]]]}));
        let b2 = call(json!({"op":"dkg_part2","secret":b1["secret"],"round1_packages":[[CLIENT_ID,a1["package"]]]}));
        // round2 packages addressed to each party (server's package for the client, etc.)
        let r2_for_a = b2["packages"].as_array().unwrap().iter().find(|x| x[0]==CLIENT_ID).unwrap()[1].clone();
        let r2_for_b = a2["packages"].as_array().unwrap().iter().find(|x| x[0]==SERVER_ID).unwrap()[1].clone();
        let a3 = call(json!({"op":"dkg_part3","secret":a2["secret"],"round1_packages":[[SERVER_ID,b1["package"]]],"round2_packages":[[SERVER_ID,r2_for_a]]}));
        let b3 = call(json!({"op":"dkg_part3","secret":b2["secret"],"round1_packages":[[CLIENT_ID,a1["package"]]],"round2_packages":[[CLIENT_ID,r2_for_b]]}));
        assert_eq!(a3["group_pubkey_hex"], b3["group_pubkey_hex"], "same group key");

        let ca = call(json!({"op":"commit","key_package":a3["key_package"]}));
        let cb = call(json!({"op":"commit","key_package":b3["key_package"]}));
        let sp = call(json!({"op":"build_signing_package","commitments":[[CLIENT_ID,ca["commitments"]],[SERVER_ID,cb["commitments"]]],"message_hex":"deadbeef"}));
        let sa = call(json!({"op":"sign","signing_package":sp["signing_package"],"nonces":ca["nonces"],"key_package":a3["key_package"]}));
        let sb = call(json!({"op":"sign","signing_package":sp["signing_package"],"nonces":cb["nonces"],"key_package":b3["key_package"]}));
        let agg = call(json!({"op":"aggregate","signing_package":sp["signing_package"],"signature_shares":[[CLIENT_ID,sa["signature_share"]],[SERVER_ID,sb["signature_share"]]],"public_key_package":a3["public_key_package"]}));
        assert_eq!(agg["verified"], json!(true), "aggregate verifies: {agg}");
        println!("JSON-dispatch group account = {}", a3["group_pubkey_hex"]);
    }
}
