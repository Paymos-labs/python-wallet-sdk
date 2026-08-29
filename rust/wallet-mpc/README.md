# paymos-wallet-core — the 2-of-2 signing core

The FROST co-signing core every Paymos wallet SDK embeds. It is the piece that makes a vault
**non-custodial**: two shares exist, one in your process and one on the server, and a signature is
produced only when both take part. Neither share alone can move a coin.

This crate is published on its own because the Rust SDK depends on it and registries do not accept
path dependencies — and because a signing core that eight languages rely on should be readable by
anyone who trusts it.

```toml
[dependencies]
paymos-wallet-core = "0"
```

Each release here carries the compiled core for **Linux (x86-64 and arm64)** and **Windows x64**,
with a SHA-256 beside every archive, so a Go, C#, Java, PHP or Ruby package does not compile Rust at
install time. macOS is not among them yet; on a Mac, build this crate and point `PAYMOS_NATIVE_LIB`
at the result.

## What it does

- **Two-of-two FROST (Ed25519).** Key generation produces two shares; signing is a two-round protocol
  between them. No dealer holds the whole key at any moment, including at creation.
- **It signs what it is handed, and nothing more.** The core takes an opaque message and produces a
  share of a signature over it. It does not know what the message means, and it does not check it —
  the binding check that compares the message against the operation you approved lives one layer up,
  in each SDK, which recomputes the digest itself before it ever asks for a signature.
- **No network, no storage.** It signs. Where shares live, how they are backed up, and who is asked
  for what belong to the SDK above it.

## What it is not

- Not a wallet. It holds no balance, knows no address, and speaks to nothing.
- Not a general FROST library. The threshold is fixed at two of two, because that is the property
  the product needs: not "some of many", but "both, always".

## Security

A vulnerability here is a vulnerability in every SDK that embeds it. Report privately to
**security@paymos.io** rather than opening an issue.

## FAQ

### Why 2-of-2 rather than a threshold like 2-of-3?

Because the promise is that nobody can move money alone, and a recovery share held by a third party
is a third party who can. Backup is solved by backing up the client share, not by adding a signer.

### What happens if the server disappears?

The share in your process is one half of a key; the other half is exportable by design, so the vault
outlives the service. That property is the SDK's to expose — the core only signs.

### Does the server ever see the client share?

No. The protocol exchanges commitments and signature shares, never key material. That is the whole
reason for a two-round protocol instead of "send me your key and I will sign".

## The SDKs that embed it

| Language | Package | Repository |
|---|---|---|
| Python | `pip install paymos-wallet` | [python-wallet-sdk](https://github.com/paymos-labs/python-wallet-sdk) |
| TypeScript / Node.js | `npm install @paymos/wallet` | [typescript-wallet-sdk](https://github.com/paymos-labs/typescript-wallet-sdk) |
| Go | `go get github.com/paymos-labs/go-wallet-sdk` | [go-wallet-sdk](https://github.com/paymos-labs/go-wallet-sdk) |
| Rust | `cargo add paymos-wallet` | [rust-wallet-sdk](https://github.com/paymos-labs/rust-wallet-sdk) |
| C# / .NET | `dotnet add package Paymos.Wallet` | [csharp-wallet-sdk](https://github.com/paymos-labs/csharp-wallet-sdk) |
| Java | `io.paymos:wallet` | [java-wallet-sdk](https://github.com/paymos-labs/java-wallet-sdk) |
| Ruby | `gem install paymos-wallet` | [ruby-wallet-sdk](https://github.com/paymos-labs/ruby-wallet-sdk) |
| PHP | `composer require paymos/wallet` | [php-wallet-sdk](https://github.com/paymos-labs/php-wallet-sdk) |

## Documentation

- [wallet.paymos.io](https://wallet.paymos.io) — the wallet these SDKs control.
